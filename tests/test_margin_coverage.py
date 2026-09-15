"""Coverage backtest: pure logic on synthetic inputs, plus a DB round trip (db marker)."""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest

from risk_engine.backtest.hpl import DailyPnl, daily_pnl, horizon_pnl
from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.margin import coverage
from risk_engine.margin import engine as margin_engine
from risk_engine.margin.params import MarginParams
from risk_engine.risk import engine, record
from risk_engine.risk import universe as uni
from risk_engine.risk.positions import load_positions_csv
from risk_engine.risk.returns import build

from .conftest import MIGRATIONS_DIR, REPO_ROOT

SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
}
POS = {"EURUSD": 1_000_000.0, "WTI": 10_000.0}
CFG = coverage.CoverageConfig(target=0.99, window_days=10, mpor_days=2, sha256="0" * 64)


def _rm(n: int = 60) -> Any:
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2024-01-01", periods=n)
    lv = pd.DataFrame(
        {
            "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(n) * 0.006)),
            "WTI": 60 + np.cumsum(rng.standard_normal(n) * 1.5),
        },
        index=dates,
    )
    return build(lv, SPECS), lv


def test_horizon_pnl_is_the_cumulative_change_and_h1_is_daily_pnl() -> None:
    rm, lv = _rm()
    t = rm.changes.index[10]
    two = horizon_pnl(rm, t, 2, POS, SPECS)
    assert two.pnl_date == rm.changes.index[12].date()
    # WTI leg: exact q x (P_{t+2} - P_t); FX leg: exact q S_t (S_{t+2}/S_t - 1)
    wti = 10_000 * (lv["WTI"].iloc[13] - lv["WTI"].iloc[11])  # levels index = changes index + 1
    assert -two.loss_by_instrument["WTI"] == pytest.approx(wti)
    one = horizon_pnl(rm, t, 1, POS, SPECS)
    assert one == daily_pnl(rm, t, POS, SPECS)
    with pytest.raises(ValueError, match="observations after"):
        horizon_pnl(rm, rm.changes.index[-1], 2, POS, SPECS)


GAP_RUNS = (5, 12)  # transitions that span 3 business days (a holiday inside the MPOR)


def _runs_and_pnls(n: int = 25) -> tuple[list[coverage.MarginRunRecord], list[DailyPnl]]:
    runs, pnls = [], []
    d = np.datetime64("2024-03-04")  # a Monday
    for i in range(n):
        as_of = d.astype(date)
        nxt = np.busday_offset(d, 3 if i in GAP_RUNS else 2).astype(date)
        loss = 50.0 if i not in (3, 7) else 400.0  # two breaches of im = 100
        runs.append(coverage.MarginRunRecord(i + 1, as_of, 100.0, 80.0, 60.0))
        pnls.append(DailyPnl(as_of, nxt, -loss, -loss, {"WTI": loss}))
        d = np.busday_offset(d, 1)
    return runs, pnls


def test_backtest_counts_breaches_by_three_yardsticks_and_windows() -> None:
    runs, pnls = _runs_and_pnls()
    im_h = {runs[i].as_of: 150.0 for i in GAP_RUNS}
    rep = coverage.backtest(runs, pnls, CFG, universe="u", portfolio_code="T", im_h=im_h)
    assert len(rep.days) == 25 and [w.n_obs for w in rep.windows] == [10, 10, 10]
    total = sum(d.breach for d in rep.days)
    assert total == 2 and sum(d.breach_core for d in rep.days) == 2
    assert sum(d.breach_span for d in rep.days if d.breach_span is not None) == 2
    d3 = rep.days[3]
    assert d3.shortfall == 300.0 and d3.attribution == {"WTI": 400.0, "_total": 400.0}
    # multi-day transitions use the h-day margin as the official yardstick
    multi = [d for d in rep.days if d.h > 2]
    assert [d.h for d in multi] == [3, 3] and all(d.im_h_block == 150.0 for d in multi)
    assert all(d.h == 2 and d.im_h_block is None for d in rep.days if d.h <= 2)
    w0 = rep.windows[0]
    assert w0.breaches == 2 and w0.coverage == pytest.approx(0.8) and w0.target == 0.99
    assert w0.kupiec.p_value < 0.05  # 2/10 at 1 % is rejected
    assert w0.max_shortfall == 300.0 and w0.max_shortfall_over_im == pytest.approx(3.0)
    assert rep.windows[1].breaches == 0 and rep.windows[1].coverage == 1.0
    with pytest.raises(ValueError, match="margin for"):
        coverage.backtest(runs, pnls, CFG, universe="u", portfolio_code="T", im_h={})


# ---------------------------------------------------------------- database round trip

N_DAYS = 703
N_RUNS = 30


@pytest.fixture
def conn(
    db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2019-01-01", periods=N_DAYS).delete([585, 586, 600])
    n = len(dates)
    series = {
        "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(n) * 0.006)),
        "WTI": 60 + np.cumsum(rng.standard_normal(n) * 1.5),
    }
    with (
        db_conn.cursor() as cur,
        cur.copy("COPY prices (instrument_id, price_date, close, adj_close) FROM STDIN") as cp,
    ):
        for t, vals in series.items():
            for d, v in zip(dates, vals, strict=True):
                cp.write_row((ids[t], d.date(), float(v), float(v)))
    csv = tmp_path / "pos.csv"
    csv.write_text(
        "portfolio_code,as_of_date,ticker,quantity\nT,2019-01-01,EURUSD,1000000\nT,2019-01-01,WTI,10000\n"
    )
    load_positions_csv(db_conn, csv)
    (tmp_path / "universes.toml").write_text(
        '[t]\nstart = 2019-01-01\ninclude = ["EURUSD", "WTI"]\n'
    )
    monkeypatch.setattr(
        "risk_engine.risk.engine.load_set",
        lambda name, path=None: uni.load_set(name, tmp_path / "universes.toml"),
    )
    rp, mp = engine.RiskParams.load(), MarginParams.load()
    rm, specs, meta = engine.prepare(db_conn, "t", date.max)
    eligible = rm.changes.index[rp.warmup_days + rp.window_days :]
    for d in eligible[:N_RUNS]:
        sliced = engine.ReturnMatrix(
            changes=rm.changes.loc[:d],
            levels=rm.levels.loc[:d],
            kind=rm.kind,
            factor_of=rm.factor_of,
            meta={**rm.meta, "last": d.date()},
        )
        res = margin_engine.run(
            db_conn,
            "T",
            d.date(),
            universe_name="t",
            risk_params=rp,
            margin_params=mp,
            tag="margin_batch",
            prepared=(sliced, specs, {**meta, "last": d.date()}),
        )
        record.write(db_conn, res, "test")
    return db_conn


@pytest.mark.db
def test_coverage_round_trip(conn: psycopg.Connection[Any]) -> None:
    cfg = coverage.CoverageConfig(target=0.99, window_days=10, mpor_days=2, sha256="0" * 64)
    conn.execute("SET default_transaction_read_only = on")  # loading and testing must not write
    runs, pnls, im_h = coverage.load_inputs(conn, "t", "T", cfg=cfg)
    rep = coverage.backtest(runs, pnls, cfg, universe="t", portfolio_code="T", im_h=im_h)
    conn.execute("SET default_transaction_read_only = off")
    assert len(runs) == N_RUNS and len(rep.days) == N_RUNS
    multi = [d for d in rep.days if d.h > 2]
    assert multi and all(d.im_h_block is not None and d.im_h_block > d.im for d in multi)
    assert all(d.pnl_date > d.as_of for d in rep.days)
    assert [w.n_obs for w in rep.windows] == [10, 10, 10]
    assert coverage.record(conn, rep, "test") == 3
    n_days, n_breach = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE breach) FROM margin_coverage_results"
    ).fetchone()
    assert n_days == N_RUNS and n_breach == sum(d.breach for d in rep.days)
    rows = conn.execute(
        "SELECT coverage, target, params->>'margin_params_sha256' FROM margin_coverage_summaries"
    ).fetchall()
    assert len(rows) == 3 and all(0 <= r[0] <= 1 and r[1] == 0.99 and len(r[2]) == 64 for r in rows)
    coverage.record(conn, rep, "test")  # re-run appends summaries, never updates
    assert conn.execute("SELECT count(*) FROM margin_coverage_summaries").fetchone() == (6,)
