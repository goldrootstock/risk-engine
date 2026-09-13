"""Database writes for the ETL (design note 03 §6 and §12).

Three writers, nothing else in the package touches the database:

* :func:`upsert_instruments` — ``config/universe.csv`` -> ``instruments`` (ids never change);
* :func:`upsert_prices` — one series' frame -> ``prices`` with the IS DISTINCT FROM guard;
* :func:`record_etl_run` — one ``etl_runs`` row per series and run.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

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

UPSERT_INSTRUMENTS_SQL = """
INSERT INTO instruments
    (source, ticker, source_id, name, asset_class, instrument_type, quote_type,
     return_type, currency, multiplier)
VALUES
    (%(source)s, %(ticker)s, %(source_id)s, %(name)s, %(asset_class)s, %(instrument_type)s,
     %(quote_type)s, %(return_type)s, %(currency)s, %(multiplier)s)
ON CONFLICT (source, ticker) DO UPDATE
SET source_id = EXCLUDED.source_id, name = EXCLUDED.name,
    asset_class = EXCLUDED.asset_class, instrument_type = EXCLUDED.instrument_type,
    quote_type = EXCLUDED.quote_type, return_type = EXCLUDED.return_type,
    currency = EXCLUDED.currency, multiplier = EXCLUDED.multiplier,
    is_active = TRUE, updated_at = now()
"""

INSERT_ETL_RUN_SQL = """
INSERT INTO etl_runs
    (started_at, finished_at, source, source_id, instrument_id, status,
     rows_fetched, rows_inserted, rows_updated, rows_unchanged,
     first_date, last_date, cache_sha256, findings, params, code_version)
VALUES
    (%(started_at)s, %(finished_at)s, %(source)s, %(source_id)s, %(instrument_id)s, %(status)s,
     %(rows_fetched)s, %(rows_inserted)s, %(rows_updated)s, %(rows_unchanged)s,
     %(first_date)s, %(last_date)s, %(cache_sha256)s, %(findings)s, %(params)s, %(code_version)s)
RETURNING etl_run_id
"""


def read_universe(path: Path) -> list[dict[str, str]]:
    """Rows of ``config/universe.csv`` as dicts (header names as keys)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def upsert_instruments(conn: psycopg.Connection[Any], rows: Sequence[Mapping[str, str]]) -> int:
    """Insert or update ``instruments`` from universe rows; returns the number of rows sent.

    Conflict key is ``(source, ticker)``. On conflict every descriptive column and
    ``updated_at = now()`` are updated; ``instrument_id`` is never changed, so ``prices``
    and ``positions`` keep their foreign keys across re-loads.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT_INSTRUMENTS_SQL, [dict(r) for r in rows])
    return len(rows)


def instrument_specs(conn: psycopg.Connection[Any], source: str) -> dict[str, InstrumentSpec]:
    """Active instruments of ``source`` keyed by ``source_id`` (read-only)."""
    rows = conn.execute(
        "SELECT instrument_id, source, source_id, ticker, quote_type, return_type "
        "FROM instruments WHERE source = %s AND is_active ORDER BY instrument_id",
        (source,),
    ).fetchall()
    return {r[2]: InstrumentSpec(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows}


def upsert_prices(
    conn: psycopg.Connection[Any], spec: InstrumentSpec, frame: pd.DataFrame
) -> LoadResult:
    """Upsert one validated series into ``prices`` inside a single transaction.

    Uses :data:`UPSERT_PRICES_SQL`: rows whose values are unchanged are not touched (their
    ``loaded_at`` keeps meaning "when this value last changed"), so ``RETURNING`` yields
    only inserted and updated rows and ``unchanged = fetched - inserted - updated``.
    ``inserted`` is counted from ``xmax = 0``.
    """
    dates: list[date] = frame["price_date"].dt.date.tolist()
    closes: list[float] = frame["close"].astype("float64").tolist()
    adj: list[float] = frame["adj_close"].astype("float64").tolist()
    vols: list[int | None] = [None if pd.isna(v) else int(v) for v in frame["volume"].tolist()]
    records = [
        {
            "instrument_id": spec.instrument_id,
            "price_date": d,
            "close": c,
            "adj_close": a,
            "volume": v,
        }
        for d, c, a, v in zip(dates, closes, adj, vols, strict=True)
    ]
    inserted = updated = 0
    with conn.transaction(), conn.cursor() as cur:
        for record in records:
            cur.execute(UPSERT_PRICES_SQL, record)
            hit = cur.fetchone()
            if hit is None:
                continue  # guard: nothing changed
            if hit[0]:
                inserted += 1
            else:
                updated += 1
    return LoadResult(
        spec.instrument_id, len(records), inserted, updated, len(records) - inserted - updated
    )


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
    findings = [
        {**asdict(f), "price_date": f.price_date.isoformat() if f.price_date else None}
        for f in (report.findings if report else ())
    ]
    row = {
        "started_at": started_at,
        "finished_at": finished_at,
        "source": source,
        "source_id": source_id,
        "instrument_id": instrument_id,
        "status": status,
        "rows_fetched": result.fetched if result else (report.rows if report else 0),
        "rows_inserted": result.inserted if result else 0,
        "rows_updated": result.updated if result else 0,
        "rows_unchanged": result.unchanged if result else 0,
        "first_date": report.first if report else None,
        "last_date": report.last if report else None,
        "cache_sha256": cache_sha256,
        "findings": Jsonb(findings),
        "params": Jsonb(dict(params)),
        "code_version": code_version,
    }
    with conn.transaction():
        got = conn.execute(INSERT_ETL_RUN_SQL, row).fetchone()
    assert got is not None
    return int(got[0])
