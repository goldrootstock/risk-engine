"""U.S. Treasury daily par yield curve (design note 03 §3).

One CSV per calendar year, from 1990. Columns are quoted maturity labels (``"1 Mo"``,
``"10 Yr"``) and the set of columns changes across years (9 columns in 1990, 13 in 2020).
Dates are ``MM/DD/YYYY`` in descending order; missing cells are empty strings. Values are
percent (``0.93`` = 0.93 %), stored as-is with ``quote_type = 'yield'``.

Incremental syncs re-download every year that overlaps ``[start, end]`` (the orchestrator
has already moved ``start`` back by the overlap window); a whole year file is at most ~15 KB,
so refetching the current year daily is the incremental unit.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd

from risk_engine.data.etl.contract import RawFile
from risk_engine.data.etl.http import client_for, fetch_with_retry
from risk_engine.data.etl.sources.ecb import _to_contract

URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
#: First year served by the archive endpoint [확인 2026-09-12].
FIRST_YEAR = 1990


def years_to_fetch(start: date | None, end: date | None, overlap_days: int = 0) -> list[int]:
    """Calendar years whose files must be downloaded to cover ``[start - overlap, end]``.

    ``start=None`` means every year from :data:`FIRST_YEAR`; ``end=None`` means today.
    ``overlap_days`` is kept for callers that want it; the orchestrator passes an already
    shifted ``start`` and the default is 0. Pure function; tested without network.
    """
    last = (end or date.today()).year
    first = (
        FIRST_YEAR
        if start is None
        else max(FIRST_YEAR, (start - timedelta(days=overlap_days)).year)
    )
    return list(range(first, last + 1))


class UsTreasurySource:
    """Fetch and parse Treasury par yield curve files."""

    name = "ustreasury"

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Create the source; client lifetime and retries as in ``EcbSource`` / ``etl.http``."""
        self._client = client

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """Download one CSV per year from :func:`years_to_fetch`.

        Returns one :class:`RawFile` per year with ``scope = str(year)``, in ascending year
        order. ``source_ids`` is ignored for fetching (every maturity is in every file) but
        may be used to short-circuit when empty. Non-2xx raises; a body that does not start
        with ``Date`` is rejected as not-a-CSV.
        """
        if not source_ids:
            return []
        files: list[RawFile] = []
        with client_for(self._client) as client:
            for year in years_to_fetch(start, end):
                url = URL_TEMPLATE.format(year=year)
                fetched_at = datetime.now(UTC)
                content = fetch_with_retry(client, url).content
                if not content.lstrip(b"\xef\xbb\xbf").startswith(b"Date"):
                    raise ValueError(f"Treasury response for {year} is not the yield-curve CSV")
                files.append(RawFile(self.name, str(year), url.split("?")[0], content, fetched_at))
        return files

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Return a long frame with one row per (maturity, date) present in the file.

        ``source_id`` is the column label with surrounding quotes stripped and internal
        whitespace preserved exactly as the vendor writes it (``"10 Yr"`` -> ``10 Yr``) so
        that it matches ``instruments.source_id``. Empty cells are dropped. A maturity that
        does not exist in that year simply yields no rows (1990 has no ``1 Mo``). Result is
        sorted ascending by ``(source_id, price_date)`` with a fresh ``RangeIndex``
        (``contract.Source.parse``); the vendor file is descending.
        """
        wide = pd.read_csv(io.BytesIO(raw.content), encoding="utf-8-sig")
        wide.columns = [str(c).strip().strip('"') for c in wide.columns]
        long = wide.melt(id_vars="Date", var_name="source_id", value_name="close").dropna(
            subset=["close"]
        )
        return _to_contract(long, date_format="%m/%d/%Y")
