"""load_levels / load_specs against PostgreSQL in a read-only session (db marker)."""

from datetime import date
from typing import Any

import psycopg
import pytest

from risk_engine.data.etl.load import read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade
from risk_engine.risk.levels import load_levels, load_specs

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = pytest.mark.db


@pytest.fixture
def conn(db_conn: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    ids = dict(db_conn.execute("SELECT ticker, instrument_id FROM instruments").fetchall())
    rows = [
        (ids["EURUSD"], "2020-04-20", 1.09),
        (ids["EURUSD"], "2020-04-21", 1.10),
        (ids["UST_10Y"], "2020-04-20", 0.63),  # no 04-21: FRED T+1 hole
        (ids["WTI"], "2020-04-20", -36.98),
        (ids["WTI"], "2020-04-21", 8.91),
    ]
    for iid, d, v in rows:
        db_conn.execute(
            "INSERT INTO prices (instrument_id, price_date, close, adj_close) "
            "VALUES (%s, %s, %s, %s)",
            (iid, d, v, v),
        )
    return db_conn


def test_load_levels_is_wide_with_nan_holes_and_read_only(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SET default_transaction_read_only = on")  # loaders must not write (note 00)
    wide = load_levels(conn, ["EURUSD", "UST_10Y", "WTI"], date(2020, 4, 1))
    assert list(wide.columns) == ["EURUSD", "UST_10Y", "WTI"]
    assert wide.shape == (2, 3)
    assert wide.loc["2020-04-21", "UST_10Y"] != wide.loc["2020-04-21", "UST_10Y"]  # NaN
    assert wide.loc["2020-04-20", "WTI"] == -36.98
    specs = load_specs(conn, ["EURJPY", "WTI"])
    assert specs["EURJPY"].currency == "JPY" and specs["WTI"].return_type == "absolute"
    assert load_levels(conn, ["EURUSD"], date(2030, 1, 1)).empty
    conn.execute("SET default_transaction_read_only = off")
