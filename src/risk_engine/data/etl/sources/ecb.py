"""ECB euro foreign exchange reference rates (design note 03 §3).

One file, ``eurofxref-hist.zip``, holds the full history for every currency. A 90-day
incremental file exists in XML form (``eurofxref-hist-90d.xml``; the ``.zip``/``.csv``
spellings are 404) but is deliberately not used so that a single CSV parser covers the
source; every sync therefore downloads the whole ~640 KB archive (design note 03 §3, §13).
Inside the zip is ``eurofxref-hist.csv``:

* header ``Date,USD,JPY,...,`` — note the **trailing comma**, which yields an empty last
  column that must be dropped;
* rows in **descending** date order, ``YYYY-MM-DD``;
* missing values written as ``N/A`` (currencies added later, or discontinued ones);
* values are ``1 EUR = x CCY``, so ``source_id`` is the ISO code column name.

Bodies are written by the author (CLAUDE.md §5); this module ships signatures, docstrings
and constants only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import httpx
import pandas as pd

from risk_engine.data.etl.contract import RawFile

HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
#: Scope used in the cache: one file covers every currency.
SCOPE_ALL = "_all"
#: Name of the CSV member inside the zip archive.
CSV_MEMBER = "eurofxref-hist.csv"
#: First byte pair of every zip file; a 404 HTML page does not start with this.
ZIP_MAGIC = b"PK"


class EcbSource:
    """Fetch and parse the ECB reference-rate history."""

    name = "ecb"

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Create the source.

        Args:
            client: Injected HTTP client, mainly ``httpx.Client(transport=MockTransport(...))``
                in tests. ``None`` means a default client with a 30 s timeout is created per
                ``fetch`` call.
        """
        self._client = client

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """Download ``eurofxref-hist.zip`` once, regardless of ``source_ids``/``start``/``end``.

        Returns exactly one :class:`RawFile` with ``scope = SCOPE_ALL`` and the zip bytes as
        received. Raises ``httpx.HTTPStatusError`` on a non-2xx response and ``ValueError``
        when the body does not start with :data:`ZIP_MAGIC` (the ECB serves an HTML error
        page with status 404 for unknown paths; it must never be cached as data).

        Retries: 5xx and timeouts up to 3 times with exponential back-off; 4xx is final.
        """
        raise NotImplementedError

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Unzip ``CSV_MEMBER`` and return a long frame with :data:`~contract.PRICE_COLUMNS`.

        Every currency column becomes a ``source_id``; ``N/A`` cells are dropped (never
        filled); the empty trailing column is discarded; ``close == adj_close``; ``volume``
        is ``<NA>``. The vendor file is in descending date order: the result must be sorted
        ascending by ``(source_id, price_date)`` with a fresh ``RangeIndex`` (0..n-1), as
        required by ``contract.Source.parse``.
        """
        raise NotImplementedError
