"""Database writes for the ETL (design note 03 §6 and §12).

Three writers, nothing else in the package touches the database:

* :func:`upsert_instruments` — ``config/universe.csv`` -> ``instruments`` (ids never change);
* :func:`upsert_prices` — one series' frame -> ``prices`` with the IS DISTINCT FROM guard;
* :func:`record_etl_run` — one ``etl_runs`` row per series and run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from risk_engine.data.etl.contract import InstrumentSpec, LoadResult, Report

UPSERT_PRICES_SQL = """
INSERT INTO prices (instrument_id, price_date, close, adj_close, volume)
VALUES (%(instrument_id)s, %(price_date)s, %(close)s, %(adj_close)s, %(volume)s)
ON CONFLICT (instrument_id, price_date) DO UPDATE
SET close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
    volume = EXCLUDED.volume, loaded_at = now()
WHERE prices.close IS DISTINCT FROM EXCLUDED.close
   OR prices.adj_close IS DISTINCT FROM EXCLUDED.adj_close
   OR prices.volume IS DISTINCT FROM EXCLUDED.volume
RETURNING (xmax = 0) AS inserted
"""


def read_universe(path: Path) -> list[dict[str, str]]:
    """Rows of ``config/universe.csv`` as dicts (header names as keys)."""
    raise NotImplementedError


def upsert_instruments(conn: psycopg.Connection[Any], rows: Sequence[Mapping[str, str]]) -> int:
    """Insert or update ``instruments`` from universe rows; returns the number of rows sent.

    Conflict key is ``(source, ticker)``. On conflict every descriptive column and
    ``updated_at = now()`` are updated; ``instrument_id`` is never changed, so ``prices``
    and ``positions`` keep their foreign keys across re-loads.
    """
    raise NotImplementedError


def instrument_specs(conn: psycopg.Connection[Any], source: str) -> dict[str, InstrumentSpec]:
    """Active instruments of ``source`` keyed by ``source_id`` (read-only)."""
    raise NotImplementedError


def upsert_prices(
    conn: psycopg.Connection[Any], spec: InstrumentSpec, frame: pd.DataFrame
) -> LoadResult:
    """Upsert one validated series into ``prices`` inside a single transaction.

    Uses :data:`UPSERT_PRICES_SQL`: rows whose values are unchanged are not touched (their
    ``loaded_at`` keeps meaning "when this value last changed"), so ``RETURNING`` yields
    only inserted and updated rows and ``unchanged = fetched - inserted - updated``.
    ``inserted`` is counted from ``xmax = 0``.
    """
    raise NotImplementedError


def record_etl_run(
    conn: psycopg.Connection[Any],
    *,
    started_at: datetime,
    finished_at: datetime,
    source: str,
    source_id: str,
    instrument_id: int | None,
    status: str,
    report: Report | None,
    result: LoadResult | None,
    cache_sha256: str | None,
    params: Mapping[str, Any],
    code_version: str | None,
) -> int:
    """Insert one ``etl_runs`` row and return its id.

    Called only by the sync orchestrator. ``report.findings`` is serialised to the JSON shape
    documented in design note 03 §12; ``result`` supplies the row counts (zeros when the
    series was skipped or the run was a dry run).
    """
    raise NotImplementedError
