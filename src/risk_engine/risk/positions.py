"""Position snapshots (design note 01 §2-3): read the latest snapshot, load a CSV."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

SNAPSHOT_SQL = """
WITH latest AS (
    SELECT max(as_of_date) AS as_of
    FROM positions WHERE portfolio_code = %(code)s AND as_of_date <= %(as_of)s
)
SELECT i.ticker, p.quantity, p.as_of_date
FROM positions p JOIN instruments i USING (instrument_id), latest
WHERE p.portfolio_code = %(code)s AND p.as_of_date = latest.as_of
ORDER BY i.instrument_id
"""


def load_snapshot(
    conn: psycopg.Connection[Any], portfolio_code: str, as_of: date
) -> tuple[dict[str, float], date]:
    """``({ticker: quantity}, positions_as_of)`` — the latest snapshot on or before ``as_of``."""
    rows = conn.execute(SNAPSHOT_SQL, {"code": portfolio_code, "as_of": as_of}).fetchall()
    if not rows:
        raise LookupError(f"no positions for {portfolio_code} on or before {as_of}")
    return {r[0]: float(r[1]) for r in rows}, rows[0][2]


def load_positions_csv(conn: psycopg.Connection[Any], path: Path) -> int:
    """Upsert ``portfolio_code, as_of_date, ticker, quantity`` rows into ``positions``.

    A separate write command (not part of any check). Returns rows written.
    """
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with conn.transaction():
        for r in rows:
            conn.execute(
                "INSERT INTO positions (portfolio_code, as_of_date, instrument_id, quantity) "
                "SELECT %(portfolio_code)s, %(as_of_date)s, instrument_id, %(quantity)s "
                "FROM instruments WHERE ticker = %(ticker)s "
                "ON CONFLICT (portfolio_code, as_of_date, instrument_id) "
                "DO UPDATE SET quantity = EXCLUDED.quantity, loaded_at = now()",
                r,
            )
    return len(rows)
