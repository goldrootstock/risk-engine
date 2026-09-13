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

import io
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime

import httpx
import pandas as pd

from risk_engine.data.etl.contract import PRICE_COLUMNS, RawFile
from risk_engine.data.etl.http import client_for, fetch_with_retry

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
                in tests. It is borrowed, never closed here. ``None`` means each ``fetch``
                call opens its own client via ``etl.http.client_for`` and closes it on
                exit, so no connection outlives the call.
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

        Retries: ``etl.http.fetch_with_retry`` — 5xx/429/timeouts up to 3 attempts with
        exponential back-off; other 4xx are final. No retry loop is written here.
        """
        fetched_at = datetime.now(UTC)
        with client_for(self._client) as client:
            response = fetch_with_retry(client, HIST_URL)
        content = response.content
        if not content.startswith(ZIP_MAGIC):
            raise ValueError(f"ECB response is not a zip archive ({len(content)} bytes)")
        return [RawFile(self.name, SCOPE_ALL, HIST_URL, content, fetched_at)]

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Unzip ``CSV_MEMBER`` and return a long frame with :data:`~contract.PRICE_COLUMNS`.

        Every currency column becomes a ``source_id``; ``N/A`` cells are dropped (never
        filled); the empty trailing column is discarded; ``close == adj_close``; ``volume``
        is ``<NA>``. The vendor file is in descending date order: the result must be sorted
        ascending by ``(source_id, price_date)`` with a fresh ``RangeIndex`` (0..n-1), as
        required by ``contract.Source.parse``.
        """
        with zipfile.ZipFile(io.BytesIO(raw.content)) as zf:
            csv_bytes = zf.read(CSV_MEMBER)
        wide = pd.read_csv(io.BytesIO(csv_bytes), na_values=["N/A"])
        wide = wide.loc[:, [c for c in wide.columns if not str(c).startswith("Unnamed")]]
        long = wide.melt(id_vars="Date", var_name="source_id", value_name="close").dropna(
            subset=["close"]
        )
        return _to_contract(long, date_format="%Y-%m-%d")


def _to_contract(long: pd.DataFrame, *, date_format: str) -> pd.DataFrame:
    """Shared tail of the CSV parsers: types, ``adj_close``/``volume``, ordering, index."""
    out = pd.DataFrame(
        {
            "source_id": long["source_id"].astype("string"),
            "price_date": pd.to_datetime(long["Date"], format=date_format).astype("datetime64[ns]"),
            "close": long["close"].astype("float64"),
        }
    )
    out["adj_close"] = out["close"]
    out["volume"] = pd.array([pd.NA] * len(out), dtype="Int64")
    out = out.sort_values(["source_id", "price_date"], kind="mergesort").reset_index(drop=True)
    return out[list(PRICE_COLUMNS)]
