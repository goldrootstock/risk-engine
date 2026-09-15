"""Margin runs recorded in risk_runs on synthetic prices (db marker; design note 09 §8)."""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest

from risk_engine.app import queries
from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.margin import engine as margin_engine
from risk_engine.margin.params import MarginParams
from risk_engine.risk import engine, record
from risk_engine.risk import universe as uni
from risk_engine.risk.positions import load_positions_csv

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = pytest.mark.db

N_DAYS = 900


@pytest.fixture
def conn(
    db_conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rng = np.random.default_rng(3)
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
        "H,2019-01-01,WTI,10000\nH,2019-01-01,BRENT,-10000\nH,2019-01-01,EURUSD,1000000\n"
        "H,2019-01-01,UST_10Y,10000000\n"
    )
    assert load_positions_csv(db_conn, csv) == 4
    (tmp_path / "universes.toml").write_text(
        '[t]\nstart = 2019-01-01\ninclude = ["EURUSD", "WTI", "BRENT", "UST_10Y"]\n'
    )
    monkeypatch.setattr(
        "risk_engine.risk.engine.load_set",
        lambda name, path=None: uni.load_set(name, tmp_path / "universes.toml"),
    )
    return db_conn


def test_margin_run_records_in_risk_runs_and_stays_out_of_the_1day_series(
    conn: psycopg.Connection[Any],
) -> None:
    rp, mp = engine.RiskParams.load(), MarginParams.load()
    as_of = date(2022, 6, 10)
    risk = engine.run(conn, "H", as_of, universe_name="t", params=rp, tag="daily_batch")
    risk_id = record.write(conn, risk, "test")
    res = margin_engine.run(
        conn, "H", as_of, universe_name="t", risk_params=rp, margin_params=mp, tag="margin_batch"
    )
    assert res.method == "fhs" and res.horizon_days == 2 and res.n_scenarios == 499
    run_id = record.write(conn, res, "test")
    assert run_id > risk_id
    rows = dict(
        conn.execute(
            "SELECT measure, value FROM risk_measures"
            " WHERE run_id = %s AND scope_type = 'portfolio' AND confidence IS NULL",
            (run_id,),
        ).fetchall()
    )
    parts = ("im_core", "im_floor", "im_stress_blend", "im_liquidity_addon")
    assert rows["im"] == pytest.approx(
        sum(rows[p] for p in parts) + rows["im_concentration_addon"], rel=1e-9
    )
    assert rows["im_span_legacy"] > 0
    hdr = conn.execute(
        "SELECT tag, horizon_days, universe, params->>'margin_params_sha256',"
        " params->'span'->'scan_risk' FROM risk_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    assert hdr is not None and hdr[:3] == ("margin_batch", 2, "t") and hdr[3] == mp.sha256
    assert set(hdr[4]) == {"WTI", "BRENT", "EURUSD", "UST_10Y"}
    # per-instrument rows: core Euler split sums to im_core; add-ons sum to their totals
    comp = conn.execute(
        "SELECT sum(value) FROM risk_measures WHERE run_id = %s AND measure = 'component_es'",
        (run_id,),
    ).fetchone()
    assert comp is not None and float(comp[0]) == pytest.approx(rows["im_core"], rel=1e-9)
    # the 2-day margin ES exceeds the 1-day risk ES 97.5 on the same book and date
    es1 = queries.portfolio_measure(
        queries.latest_run(conn, "t", "H", tag="daily_batch") or {}, "es", 0.975
    )
    assert es1 is not None and rows["im_core"] > es1["value"]
    # the 1-day readers still see only the risk run (positive selection, note 08 §3)
    latest = queries.latest_run(conn, "t", "H", tag=None)
    assert latest is not None and latest["run_id"] == risk_id
    margin = queries.latest_run(conn, "t", "H", tag="margin_batch", horizon_days=2)
    assert margin is not None and margin["run_id"] == run_id
    cat = {(c["tag"], c["horizon_days"]): c["n_runs"] for c in queries.catalog(conn)}
    assert cat == {("daily_batch", 1): 1, ("margin_batch", 2): 1}


def test_members_csv_loads_and_each_member_margins(conn: psycopg.Connection[Any]) -> None:
    """The four clearing members load; the ones inside the test universe run end to end."""
    n = load_positions_csv(conn, REPO_ROOT / "config" / "positions_members.csv")
    assert n == 27  # 5 + 5 + 9 + 8 rows
    codes = [
        r[0]
        for r in conn.execute("SELECT DISTINCT portfolio_code FROM positions ORDER BY 1").fetchall()
    ]
    assert codes == ["CM_DIVERSIFIED", "CM_ENERGY", "CM_HEDGED", "CM_RATES", "H"]
    with pytest.raises(KeyError, match="outside the universe"):
        margin_engine.run(conn, "CM_RATES", date(2022, 6, 10), universe_name="t")


def test_0009_tables_exist_and_are_empty(conn: psycopg.Connection[Any]) -> None:
    for table in (
        "margin_coverage_results",
        "margin_coverage_summaries",
        "default_fund_runs",
        "default_fund_results",
    ):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    comment = conn.execute(
        "SELECT col_description('risk_runs'::regclass, attnum) FROM pg_attribute"
        " WHERE attrelid = 'risk_runs'::regclass AND attname = 'tag'"
    ).fetchone()
    assert comment is not None and "margin_batch" in comment[0]
