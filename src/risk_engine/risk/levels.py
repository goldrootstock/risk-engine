"""Read price / yield levels from the database into a wide frame (read-only)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import psycopg

from risk_engine.data.etl.contract import InstrumentSpec

LEVELS_SQL = """
SELECT i.ticker, p.price_date, p.adj_close
FROM prices p
JOIN instruments i USING (instrument_id)
WHERE i.ticker = ANY(%(tickers)s)
  AND p.price_date >= %(start)s
  AND (%(end)s::date IS NULL OR p.price_date <= %(end)s::date)
ORDER BY p.price_date, i.ticker
"""

SPECS_SQL = """
SELECT instrument_id, source, source_id, ticker, quote_type, return_type, currency, multiplier
FROM instruments WHERE ticker = ANY(%(tickers)s) AND is_active
"""


def load_levels(
    conn: psycopg.Connection[Any], tickers: list[str], start: date, end: date | None = None
) -> pd.DataFrame:
    """Wide frame of ``adj_close`` (date x ticker) for ``tickers`` from ``start`` to ``end``.

    Missing dates are left as NaN; alignment is the return builder's job. Uses
    ``adj_close`` (returns), not ``close`` (valuation) — identical for the v1 sources.
    """
    rows = conn.execute(LEVELS_SQL, {"tickers": tickers, "start": start, "end": end}).fetchall()
    long = pd.DataFrame(rows, columns=["ticker", "price_date", "level"])
    if long.empty:
        return pd.DataFrame(columns=tickers, dtype="float64")
    wide = long.pivot(index="price_date", columns="ticker", values="level").astype("float64")
    wide.index = pd.to_datetime(wide.index)
    wide.index.name = "price_date"
    wide.columns.name = None
    return wide.reindex(columns=[t for t in tickers if t in wide.columns])


def load_specs(conn: psycopg.Connection[Any], tickers: list[str]) -> dict[str, InstrumentSpec]:
    """``{ticker: InstrumentSpec}`` for the active instruments in ``tickers`` (read-only)."""
    rows = conn.execute(SPECS_SQL, {"tickers": tickers}).fetchall()
    return {
        r[3]: InstrumentSpec(int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], float(r[7]))
        for r in rows
    }
