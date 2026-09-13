"""EIA API v2 daily spot prices (design note 03 §3).

JSON, key-authenticated, paginated at 5,000 rows. ``value`` arrives as a **string** with
trailing zeros dropped (``"12.4"``) and can be negative (``"-36.98"`` on 2020-04-20).

Secrets: the API key travels in the query string and the response **echoes it back** in
``request.params.api_key`` [확인 2026-09-12]. :meth:`EiaSource.fetch` therefore scrubs the
key from the body before returning, and :attr:`RawFile.url` never carries the query string.
The key is never logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import httpx
import pandas as pd

from risk_engine.data.etl.contract import RawFile

BASE_URL = "https://api.eia.gov"
PETROLEUM_SPOT_PATH = "/v2/petroleum/pri/spt/data/"
NATURAL_GAS_PATH = "/v2/natural-gas/pri/fut/data/"
#: Series served by the natural-gas route; everything else in the universe is petroleum.
NATURAL_GAS_SERIES: frozenset[str] = frozenset({"RNGWHHD"})
#: Maximum rows per request accepted by the API [확인 EIA docs].
PAGE_LENGTH = 5000
#: Placeholder written over the key in cached bodies.
REDACTED = "<redacted>"
#: Calendar days re-fetched behind ``start`` to pick up vendor restatements (note 03 §13).
DEFAULT_OVERLAP_DAYS = 14


def path_for(series: str) -> str:
    """API path for a series id (petroleum spot vs natural gas)."""
    raise NotImplementedError


def scrub_api_key(content: bytes, api_key: str) -> bytes:
    """Return ``content`` with every occurrence of ``api_key`` replaced by :data:`REDACTED`.

    Applied to the raw bytes, not to a parsed object, so that the cache still stores the
    vendor's exact formatting except for the secret. Pure function; tested without network.
    """
    raise NotImplementedError


class EiaSource:
    """Fetch and parse EIA spot price series."""

    name = "eia"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        """Create the source with the key from ``Settings.eia_api_key``."""
        self._api_key = api_key
        self._client = client

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """Download each series in ``source_ids`` between ``start - overlap`` and ``end``.

        One request per 5,000-row page; each page is its own :class:`RawFile` with
        ``scope = source_id``, ``page``/``pages`` set, ``url`` without the query string and
        the key scrubbed from ``content``. ``start=None`` means full history. HTTP 429 waits
        and retries; other 4xx are final; 5xx retries with back-off.
        """
        raise NotImplementedError

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Return the page's rows as a long frame.

        Validates that every row's ``series`` equals ``raw.scope`` (``series_mismatch``
        otherwise) and converts the string ``value`` to float. Facet fields (``duoarea``,
        ``area-name``, ...) are ignored. ``response.total`` is not checked here because it
        spans pages; the orchestrator compares it with the row count across pages. Result is
        sorted ascending by ``price_date`` with a fresh ``RangeIndex`` (``contract.Source.parse``);
        the API returns descending order.
        """
        raise NotImplementedError
