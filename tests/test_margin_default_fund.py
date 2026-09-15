"""Cover-2 default fund: pure sizing logic and a DB round trip (db marker)."""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.margin import default_fund as df
from risk_engine.margin import engine as margin_engine
from risk_engine.margin.params import MarginParams
from risk_engine.risk import engine, record, stress
from risk_engine.risk import universe as uni
from risk_engine.risk.positions import load_positions_csv

from .conftest import MIGRATIONS_DIR, REPO_ROOT


def _ims() -> dict[str, df.MemberIm]:
    return {c: df.MemberIm(c, i + 1, 100.0) for i, c in enumerate(("A", "B", "C", "D"))}


def test_cover_n_takes_the_two_largest_uncovered_losses_of_the_worst_scenario() -> None:
    losses = [
        *(
            df.ScenarioLoss("s1", "historical", c, v)
            for c, v in zip("ABCD", (150, 120, 90, 500), strict=True)
        ),
        *(
            df.ScenarioLoss("s2", "hypothetical", c, v)
            for c, v in zip("ABCD", (300, 300, 50, 50), strict=True)
        ),
    ]
    rep = df.cover_n(losses, _ims(), 2)
    # s1: uncovered 50, 20, 0, 400 -> top-2 = 450; s2: 200, 200, 0, 0 -> 400
    assert rep.default_fund == 450.0 and rep.binding_scenario == "s1"
    assert rep.binding_members == ("D", "A") and rep.by_scenario == {"s1": 450.0, "s2": 400.0}
    assert rep.n_members == 4 and len(rep.rows) == 8
    assert [r.uncovered for r in rep.rows if r.scenario == "s1"] == [50.0, 20.0, 0.0, 400.0]
    one = df.cover_n(losses, _ims(), 1)
    assert one.default_fund == 400.0 and one.binding_members == ("D",)
    with pytest.raises(ValueError, match="cover every member"):
        df.cover_n(losses[:-1], _ims(), 2)
    with pytest.raises(KeyError, match="no IM"):
        df.cover_n(losses, {k: v for k, v in _ims().items() if k != "D"}, 2)


# ---------------------------------------------------------------- database round trip

N_DAYS = 900
MEMBERS = ("E", "R", "H")


@pytest.fixture
def conn(
    db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2019-01-01", periods=N_DAYS)
    common = rng.standard_normal(N_DAYS)
    series = {
        "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(N_DAYS) * 0.006)),
        "WTI": 60 + np.cumsum((0.9 * common + 0.44 * rng.standard_normal(N_DAYS)) * 1.5),
        "BRENT": 63 + np.cumsum((0.9 * common + 0.44 * rng.standard_normal(N_DAYS)) * 1.5),
        "UST_10Y": 2.0 + np.cumsum(rng.standard_normal(N_DAYS) * 0.05),
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
        "portfolio_code,as_of_date,ticker,quantity\n"
        "E,2019-01-01,WTI,30000\nE,2019-01-01,EURUSD,500000\n"
        "R,2019-01-01,UST_10Y,60000000\n"
        "H,2019-01-01,WTI,40000\nH,2019-01-01,BRENT,-40000\nH,2019-01-01,EURUSD,2000000\n"
    )
    load_positions_csv(db_conn, csv)
    (tmp_path / "universes.toml").write_text(
        '[t]\nstart = 2019-01-01\ninclude = ["EURUSD", "WTI", "BRENT", "UST_10Y"]\n'
    )
    monkeypatch.setattr(
        "risk_engine.risk.engine.load_set",
        lambda name, path=None: uni.load_set(name, tmp_path / "universes.toml"),
    )
    return db_conn


@pytest.mark.db
def test_default_fund_round_trip(conn: psycopg.Connection[Any]) -> None:
    rp, mp = engine.RiskParams.load(), MarginParams.load()
    as_of = date(2022, 6, 10)
    for code in MEMBERS:
        res = margin_engine.run(
            conn,
            code,
            as_of,
            universe_name="t",
            risk_params=rp,
            margin_params=mp,
            tag="margin_batch",
        )
        record.write(conn, res, "test")
    with pytest.raises(LookupError, match="no positions"):  # unknown member fails first
        df.load_inputs(conn, "t", as_of, ["E", "X"], mpor_days=2)
    with pytest.raises(LookupError, match="no margin run"):  # a book without a recorded IM
        df.load_inputs(conn, "t", as_of, ["E"], mpor_days=2, tag="margin_adhoc")
    conn.execute("SET default_transaction_read_only = on")
    rm, specs, books, ims = df.load_inputs(conn, "t", as_of, MEMBERS, mpor_days=2)
    idx = rm.changes.index
    scen = stress.ScenarioSet(
        historical={"win": {"start": idx[-40].date(), "end": idx[-20].date(), "why": "t"}},
        hypothetical={"oil_down_30": {"energy_pct": -30, "why": "t"}},
        permutations=1,
        seed=1,
        sha256="s" * 64,
    )
    losses = df.stress_losses(rm, books, specs, scen, horizon=mp.mpor_days)  # official basis
    path = df.stress_losses(rm, books, specs, scen)  # path basis
    rep = df.cover_n(losses, ims, mp.cover)
    conn.execute("SET default_transaction_read_only = off")
    assert len(losses) == 6 and rep.n_members == 3 and rep.cover == 2
    # the worst 2-day block inside a window never loses less than ... nothing in general, but
    # it is bounded by the largest 2-day move: windows are recorded and lie inside the scenario
    for sl in losses:
        if sl.kind == "historical":
            assert sl.window_start is not None and sl.window_end is not None
            assert idx[-40].date() <= sl.window_start <= sl.window_end <= idx[-20].date()
            assert (sl.window_end - sl.window_start).days <= 4  # two aligned business days
    assert all(p.window_start == idx[-40].date() for p in path if p.kind == "historical")
    # oil -30 %: the long-only energy book is uncovered by far more than the WTI-Brent hedge
    oil = {r.portfolio_code: r for r in rep.rows if r.scenario == "oil_down_30"}
    assert oil["E"].uncovered > oil["H"].uncovered and oil["R"].uncovered == 0.0
    assert all(r.im == ims[r.portfolio_code].im and r.im_run_id > 0 for r in rep.rows)
    run_id = df.record(
        conn,
        rep,
        universe="t",
        as_of=as_of,
        scenario_sha256=scen.sha256,
        margin_params_sha256=mp.sha256,
        params={"basis": "mpor", "horizon": 2, "members": list(MEMBERS)},
        code_version="test",
        losses=losses,
    )
    with pytest.raises(ValueError, match="basis"):
        df.record(
            conn,
            rep,
            universe="t",
            as_of=as_of,
            scenario_sha256="s" * 64,
            margin_params_sha256=mp.sha256,
            params={},
            code_version="test",
        )
    hdr = conn.execute(
        "SELECT default_fund, binding_scenario, binding_members, n_members, cover,"
        " params->'by_scenario', params->>'basis', params->'windows'"
        " FROM default_fund_runs WHERE default_fund_run_id = %s",
        (run_id,),
    ).fetchone()
    assert hdr is not None and hdr[0] == rep.default_fund and hdr[1] == rep.binding_scenario
    assert tuple(hdr[2]) == rep.binding_members and (hdr[3], hdr[4]) == (3, 2)
    assert set(hdr[5]) == {"win", "oil_down_30"} and hdr[6] == "mpor"
    assert set(hdr[7]) == {f"win/{m}" for m in MEMBERS}
    rows = conn.execute(
        "SELECT count(*), sum(uncovered) FROM default_fund_results WHERE default_fund_run_id = %s",
        (run_id,),
    ).fetchone()
    assert rows is not None and rows[0] == 6
    assert float(rows[1]) == pytest.approx(sum(r.uncovered for r in rep.rows))
    fk = conn.execute(
        "SELECT count(DISTINCT im_run_id) FROM default_fund_results d JOIN risk_runs r"
        " ON r.run_id = d.im_run_id WHERE r.tag = 'margin_batch'"
    ).fetchone()
    assert fk == (3,)


def test_worst_window_loss_picks_the_largest_two_day_move_not_the_path() -> None:
    """A window whose path nets to ~0 still has a large worst 2-day block."""
    from risk_engine.risk.returns import build

    specs = {"WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0)}
    dates = pd.bdate_range("2024-01-01", periods=30)
    lv = pd.Series(60.0, index=dates)
    lv.iloc[10] = 50.0  # -10 on day 10, then flat: a one-day crash that never recovers
    lv.iloc[11] = 45.0  # -5 more on day 11
    lv.iloc[12:] = 60.0  # full recovery on day 12: the whole-window path is 0
    rm = build(pd.DataFrame({"WTI": lv}), specs)
    idx = rm.changes.index
    book = {"WTI": 1_000.0}
    w = df.worst_window_loss(rm, "w", idx[2].date(), idx[20].date(), 2, book, specs)
    assert w.stress_loss == pytest.approx(15_000.0)  # -10 then -5 over two consecutive days
    assert (w.window_start, w.window_end) == (dates[10].date(), dates[11].date())
    path = df.stress_losses(
        rm,
        {"M": book},
        specs,
        stress.ScenarioSet(
            {"w": {"start": idx[2].date(), "end": idx[20].date()}}, {}, 1, 1, "s" * 64
        ),
    )
    assert path[0].stress_loss == pytest.approx(0.0, abs=1e-9)
    # changes.index drops the first level date (note 04 §7): idx[8]..idx[9] = dates[9]..dates[10]
    short = df.worst_window_loss(rm, "one", idx[8].date(), idx[9].date(), 2, book, specs)
    assert short.stress_loss == pytest.approx(10_000.0)  # window shorter than h: cumulative
