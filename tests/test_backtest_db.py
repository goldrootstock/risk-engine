"""Backtest end to end on synthetic prices (db marker).

load_inputs runs in a read-only session; record.write persists.
"""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest

from risk_engine.backtest import record as bt_record
from risk_engine.backtest.runner import BacktestConfig, backtest, load_inputs
from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.risk import engine, record
from risk_engine.risk.positions import load_positions_csv

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = pytest.mark.db

N_DAYS = 703
N_RUNS = 40


@pytest.fixture
def conn(
    db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2019-01-01", periods=N_DAYS)
    # two mid-week holes inside the run range (runs start at warmup + window = 575) so that
    # some backtested transitions span >= 2 business days (horizon test)
    dates = dates.delete([585, 586, 600])
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
    from risk_engine.risk import universe as uni

    monkeypatch.setattr(
        "risk_engine.risk.engine.load_set",
        lambda name, path=None: uni.load_set(name, tmp_path / "universes.toml"),
    )
    # a short daily series of FHS runs
    params = engine.RiskParams.load()
    rm, specs, meta = engine.prepare(db_conn, "t", date.max)
    eligible = rm.changes.index[params.warmup_days + params.window_days :]
    for d in eligible[:N_RUNS]:
        sliced = engine.ReturnMatrix(
            changes=rm.changes.loc[:d],
            levels=rm.levels.loc[:d],
            kind=rm.kind,
            factor_of=rm.factor_of,
            meta={**rm.meta, "last": d.date()},
        )
        res = engine.run(
            db_conn,
            "T",
            d.date(),
            method="fhs",
            universe_name="t",
            params=params,
            tag="daily_batch",
            prepared=(sliced, specs, {**meta, "last": d.date()}),
        )
        record.write(db_conn, res, "test")
    return db_conn


def test_backtest_round_trip(conn: psycopg.Connection[Any]) -> None:
    cfg = BacktestConfig(
        window_days=20,
        confidence=0.99,
        significance=0.05,
        yellow_from=5,
        red_from=10,
        spearman_green=0.80,
        spearman_amber=0.70,
        ks_green=0.09,
        ks_amber=0.12,
        sha256="0" * 64,
    )
    assert conn.execute("SELECT count(*) FROM risk_runs WHERE universe = 't'").fetchone() == (
        N_RUNS,
    )
    conn.execute("SET default_transaction_read_only = on")  # loading and testing must not write
    runs, pnls, hv = load_inputs(conn, "t", "T", cfg=cfg)
    report = backtest(runs, pnls, cfg, universe="t", portfolio_code="T", horizon_var=hv)
    conn.execute("SET default_transaction_read_only = off")
    assert len(runs) == N_RUNS and len(report.days) == N_RUNS
    multi = [d for d in report.days if d.h >= 2]
    assert multi and all(d.var_block is not None and d.var_sqrt is not None for d in multi)
    assert all(d.var_block > d.var for d in multi)  # h-day VaR exceeds the 1-day VaR
    assert [w.n_obs for w in report.windows] == [20, 20]
    assert all(d.pnl_date > d.as_of for d in report.days)
    assert bt_record.write(conn, report, "test") == 2
    n_days, n_exc = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE exception) FROM backtest_results"
    ).fetchone()
    assert n_days == N_RUNS and n_exc == sum(d.exception for d in report.days)
    rows = conn.execute(
        "SELECT traffic_light, pla_zone, params->>'backtest_params_sha256' FROM backtest_summaries"
    ).fetchall()
    assert len(rows) == 2 and all(
        r[0] in ("green", "yellow", "red") and len(r[2]) == 64 for r in rows
    )
    # re-running writes new summary rows, never updates (note 00 §3)
    bt_record.write(conn, report, "test")
    assert conn.execute("SELECT count(*) FROM backtest_summaries").fetchone() == (4,)
