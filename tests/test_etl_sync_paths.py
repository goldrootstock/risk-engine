"""Transaction paths of the sync orchestrator (db marker).

Success: prices and the etl_runs row commit together. Failure: the series' prices roll back
and a 'failed' etl_runs row survives in its own transaction (design note 03 §6, JK 2026-09-13).
"""

import argparse
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
import pytest

from risk_engine.data.etl import __main__ as cli
from risk_engine.data.etl.cache import RawCache
from risk_engine.data.etl.contract import RawFile
from risk_engine.data.etl.load import instrument_specs, read_universe, upsert_instruments
from risk_engine.data.migrate import upgrade

from .conftest import MIGRATIONS_DIR, REPO_ROOT

pytestmark = pytest.mark.db


class FakeEcb:
    """Source double: no network, returns a fixed frame for USD."""

    name = "ecb"

    def fetch(
        self, source_ids: Sequence[str], start: date | None, end: date | None
    ) -> list[RawFile]:
        return [RawFile("ecb", "_all", "https://example.test/ecb", b"fake", datetime.now(UTC))]

    def parse(self, raw: RawFile) -> pd.DataFrame:
        dates = pd.to_datetime(["2020-04-29", "2020-04-30"]).astype("datetime64[ns]")
        return pd.DataFrame(
            {
                "source_id": pd.Series(["USD", "USD"], dtype="string"),
                "price_date": dates,
                "close": [1.0868, 1.0876],
                "adj_close": [1.0868, 1.0876],
                "volume": pd.array([pd.NA, pd.NA], dtype="Int64"),
            }
        )


@pytest.fixture
def conn(db_conn: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    upsert_instruments(db_conn, read_universe(REPO_ROOT / "config" / "universe.csv"))
    return db_conn


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        since=None, incremental=False, offline=False, dry_run=False, ticker=[], cache_dir=tmp_path
    )


def _run(conn: psycopg.Connection[Any], tmp_path: Path) -> int:
    specs = {"USD": instrument_specs(conn, "ecb")["USD"]}
    return cli._sync_source(
        conn,
        FakeEcb(),
        specs,
        None,
        RawCache(tmp_path),
        _args(tmp_path),
        {},
        "test",
        REPO_ROOT / "config" / "validation.toml",
    )


def _state(conn: psycopg.Connection[Any]) -> tuple[int, list[str]]:
    prices = conn.execute("SELECT count(*) FROM prices").fetchone()
    runs = conn.execute("SELECT status FROM etl_runs ORDER BY etl_run_id").fetchall()
    assert prices is not None
    return int(prices[0]), [r[0] for r in runs]


def test_success_commits_prices_and_audit_row_together(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    assert _run(conn, tmp_path) == 0
    assert _state(conn) == (2, ["loaded"])


def test_series_failure_leaves_no_prices_but_a_failed_run(
    conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.upsert_prices

    def upsert_then_crash(*a: Any, **k: Any) -> Any:
        real(*a, **k)  # rows are written inside the open transaction ...
        raise RuntimeError("disk on fire")  # ... then the series fails mid-way

    monkeypatch.setattr(cli, "upsert_prices", upsert_then_crash)
    assert _run(conn, tmp_path) == 1
    prices, runs = _state(conn)
    assert prices == 0  # rolled back with the failing transaction
    assert runs == ["failed"]  # written afterwards, in its own transaction
    detail = conn.execute("SELECT findings->-1->>'detail' FROM etl_runs").fetchone()
    assert detail is not None and "disk on fire" in detail[0]


def test_audit_failure_rolls_back_prices(
    conn: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.record_etl_run

    def record_or_crash(*a: Any, **k: Any) -> Any:
        if k.get("status") == "loaded":
            raise RuntimeError("audit table unavailable")
        return real(*a, **k)

    monkeypatch.setattr(cli, "record_etl_run", record_or_crash)
    assert _run(conn, tmp_path) == 1
    assert _state(conn) == (0, ["failed"])  # no half state: prices gone with the audit row
