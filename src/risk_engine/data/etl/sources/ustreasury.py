"""U.S. Treasury daily par yield curve (design note 03 §3).

One CSV per calendar year, from 1990. Columns are quoted maturity labels (``"1 Mo"``,
``"10 Yr"``) and the set of columns changes across years (9 columns in 1990, 13 in 2020).
Dates are ``MM/DD/YYYY`` in descending order; missing cells are empty strings. Values are
percent (``0.93`` = 0.93 %), stored as-is with ``quote_type = 'yield'``.

Incremental syncs re-download every year that overlaps ``start - overlap_days``; a whole
year file is at most ~15 KB, so refetching the current year daily is the incremental unit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import httpx
import pandas as pd

from risk_engine.data.etl.contract import RawFile

URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
#: First year served by the archive endpoint [확인 2026-09-12].
FIRST_YEAR = 1990
#: Calendar days re-fetched behind ``start`` to pick up vendor restatements (note 03 §13).
DEFAULT_OVERLAP_DAYS = 14


def years_to_fetch(
    start: date | None, end: date | None, overlap_days: int = DEFAULT_OVERLAP_DAYS
) -> list[int]:
    """Calendar years whose files must be downloaded to cover ``[start - overlap, end]``.

    ``start=None`` means every year from :data:`FIRST_YEAR`; ``end=None`` means today.
    Pure function; tested without network.
    """
    raise NotImplementedError


class UsTreasurySource:
    """Fetch and parse Treasury par yield curve files."""

    name = "ustreasury"

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Create the source; see :class:`~risk_engine.data.etl.sources.ecb.EcbSource`."""
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
        raise NotImplementedError

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Return a long frame with one row per (maturity, date) present in the file.

        ``source_id`` is the column label with surrounding quotes stripped and internal
        whitespace preserved exactly as the vendor writes it (``"10 Yr"`` -> ``10 Yr``) so
        that it matches ``instruments.source_id``. Empty cells are dropped. A maturity that
        does not exist in that year simply yields no rows (1990 has no ``1 Mo``). Result is
        sorted ascending by ``(source_id, price_date)`` with a fresh ``RangeIndex``
        (``contract.Source.parse``); the vendor file is descending.
        """
        raise NotImplementedError
