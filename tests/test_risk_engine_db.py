"""End-to-end run on synthetic prices in a throwaway schema (db marker)."""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest

from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.risk import engine, record
from risk_engine.risk.positions import load_positions_csv, load_snapshot

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = pytest.mark.db


@pytest.fixture
def conn(db_conn: psycopg.Connection[Any], tmp_path: Path) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2019-01-01", periods=900)
    series = {
        "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(900) * 0.006)),
        "WTI": 60 + np.cumsum(rng.standard_normal(900) * 1.5),
        "UST_10Y": 2.0 + np.cumsum(rng.standard_normal(900) * 0.05),
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
        "portfolio_code,as_of_date,ticker,quantity\nT,2019-01-01,EURUSD,1000000\nT,2019-01-01,WTI,10000\nT,2019-01-01,UST_10Y,10000000\n"
    )
    assert load_positions_csv(db_conn, csv) == 3
    (tmp_path / "universes.toml").write_text(
        '[t]\nstart = 2019-01-01\ninclude = ["EURUSD", "WTI", "UST_10Y"]\n'
    )
    return db_conn


def test_run_all_methods_and_record(
    conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "risk_engine.risk.engine.load_set",
        lambda name, path=None: __import__(
            "risk_engine.risk.universe", fromlist=["load_set"]
        ).load_set(name, tmp_path / "universes.toml"),
    )
    as_of = date(2022, 6, 1)
    pos, pos_as_of = load_snapshot(conn, "T", as_of)
    assert pos["WTI"] == 10_000.0 and pos_as_of == date(2019, 1, 1)
    ids: dict[str, int] = {}
    for method in ("fhs", "parametric", "mc"):
        res = engine.run(conn, "T", as_of, method=method, universe_name="t", tag="test")
        assert res.as_of_date == as_of and res.portfolio_value > 0
        ids[method] = record.write(conn, res, "test")
    rows = conn.execute(
        "SELECT run_id, method, var_99, es_975, var_99_frac FROM v_risk_headline ORDER BY run_id"
    ).fetchall()
    assert [r[1] for r in rows] == ["fhs", "parametric", "mc"]
    assert all(r[2] > 0 and r[3] > 0 and 0 < r[4] < 1 for r in rows)
    comp = conn.execute(
        "SELECT sum(value) FROM risk_measures WHERE run_id = %s AND measure = 'component_es'",
        (ids["fhs"],),
    ).fetchone()
    es = conn.execute(
        "SELECT value FROM risk_measures WHERE run_id = %s AND measure = 'es'", (ids["fhs"],)
    ).fetchone()
    assert (
        comp is not None
        and es is not None
        and float(comp[0]) == pytest.approx(float(es[0]), rel=1e-9)
    )
    params = conn.execute(
        "SELECT params FROM risk_runs WHERE run_id = %s", (ids["fhs"],)
    ).fetchone()
    assert (
        params is not None
        and params[0]["lambda"] == 0.94
        and len(params[0]["risk_params_sha256"]) == 64
    )
    assert params[0]["stressed_window"][0] < params[0]["stressed_window"][1]


def test_load_positions_rejects_unknown_ticker(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """An unknown ticker must fail loudly, not report rows written (silent-success audit)."""
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    csv = tmp_path / "bad.csv"
    csv.write_text("portfolio_code,as_of_date,ticker,quantity\nT,2019-01-01,NOPE,1\n")
    with pytest.raises(LookupError, match="NOPE"):
        load_positions_csv(db_conn, csv)
    assert db_conn.execute("SELECT count(*) FROM positions").fetchone() == (0,)
