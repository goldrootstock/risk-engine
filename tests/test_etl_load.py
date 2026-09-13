"""Loader tests against PostgreSQL (db marker). xfail (strict) until the bodies are written."""

import datetime as dt
from typing import Any

import pandas as pd
import psycopg
import pytest

from risk_engine.data.etl.contract import LoadResult
from risk_engine.data.etl.load import (
    instrument_specs,
    read_universe,
    record_etl_run,
    upsert_instruments,
    upsert_prices,
)
from risk_engine.data.migrate import upgrade

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = [
    pytest.mark.db,
    pytest.mark.xfail(
        raises=NotImplementedError, strict=True, reason="skeleton: load bodies not written yet"
    ),
]


@pytest.fixture
def conn(db_conn: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    return db_conn


def _frame(values: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source_id": pd.Series(["USD"] * len(values), dtype="string"),
            "price_date": pd.to_datetime(list(values)),
            "close": list(values.values()),
            "adj_close": list(values.values()),
            "volume": pd.Series([pd.NA] * len(values), dtype="Int64"),
        }
    )


def test_universe_upsert_keeps_instrument_ids(conn: psycopg.Connection[Any]) -> None:
    before = {r[0]: r[1] for r in conn.execute("SELECT ticker, instrument_id FROM instruments")}
    assert len(before) == 31
    rows = read_universe(REPO_ROOT / "config" / "universe.csv")
    rows[0] = {**rows[0], "name": rows[0]["name"] + " (renamed)"}
    upsert_instruments(conn, rows)
    after = {r[0]: r[1] for r in conn.execute("SELECT ticker, instrument_id FROM instruments")}
    assert after == before
    assert conn.execute(
        "SELECT count(*) FROM instruments WHERE name LIKE '%(renamed)'"
    ).fetchone() == (1,)


def test_upsert_prices_counts_and_guard(conn: psycopg.Connection[Any]) -> None:
    spec = instrument_specs(conn, "ecb")["USD"]
    assert spec.ticker == "EURUSD" and spec.return_type == "log"

    first = upsert_prices(conn, spec, _frame({"2020-04-29": 1.0868, "2020-04-30": 1.0876}))
    assert first == LoadResult(spec.instrument_id, 2, 2, 0, 0)
    loaded_at = dict(conn.execute("SELECT price_date, loaded_at FROM prices").fetchall())

    second = upsert_prices(conn, spec, _frame({"2020-04-29": 1.0868, "2020-04-30": 1.0876}))
    assert second == LoadResult(spec.instrument_id, 2, 0, 0, 2)  # nothing touched
    assert dict(conn.execute("SELECT price_date, loaded_at FROM prices").fetchall()) == loaded_at

    third = upsert_prices(conn, spec, _frame({"2020-04-29": 1.0868, "2020-04-30": 1.0900}))
    assert third == LoadResult(spec.instrument_id, 2, 0, 1, 1)  # one vendor restatement
    now = dict(conn.execute("SELECT price_date, loaded_at FROM prices").fetchall())
    assert (
        now[dt.date(2020, 4, 29)] == loaded_at[dt.date(2020, 4, 29)]
    )  # untouched row keeps loaded_at
    assert now[dt.date(2020, 4, 30)] > loaded_at[dt.date(2020, 4, 30)]


def test_record_etl_run_round_trip(conn: psycopg.Connection[Any]) -> None:
    spec = instrument_specs(conn, "ecb")["USD"]
    started = dt.datetime(2026, 9, 13, 10, 0, tzinfo=dt.UTC)
    run_id = record_etl_run(
        conn,
        started_at=started,
        finished_at=started + dt.timedelta(seconds=3),
        source="ecb",
        source_id="USD",
        instrument_id=spec.instrument_id,
        status="loaded",
        report=None,
        result=LoadResult(spec.instrument_id, 10, 8, 2, 0),
        cache_sha256="0" * 64,
        params={"since": None, "offline": False, "thresholds_sha256": "1" * 64},
        code_version="abc123",
    )
    row = conn.execute(
        "SELECT status, rows_inserted, rows_updated, params->>'code', findings "
        "FROM etl_runs WHERE etl_run_id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("loaded", 8, 2, None, [])
