"""EIA API v2 daily spot prices (design note 03 §3).

JSON, key-authenticated, paginated at 5,000 rows. ``value`` arrives as a **string** with
trailing zeros dropped (``"12.4"``) and can be negative (``"-36.98"`` on 2020-04-20).

Secrets: the API key travels in the query string and the response **echoes it back** in
``request.params.api_key`` [확인 2026-09-12]. :meth:`EiaSource.fetch` therefore scrubs the
key from the body before returning, and :attr:`RawFile.url` never carries the query string.
The key is never logged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import httpx
import pandas as pd

from risk_engine.data.etl.contract import PRICE_COLUMNS, RawFile
from risk_engine.data.etl.http import client_for, fetch_with_retry

BASE_URL = "https://api.eia.gov"
PETROLEUM_SPOT_PATH = "/v2/petroleum/pri/spt/data/"
NATURAL_GAS_PATH = "/v2/natural-gas/pri/fut/data/"
#: Series served by the natural-gas route; everything else in the universe is petroleum.
NATURAL_GAS_SERIES: frozenset[str] = frozenset({"RNGWHHD"})
#: Maximum rows per request accepted by the API [확인 EIA docs].
PAGE_LENGTH = 5000
#: Placeholder written over the key in cached bodies.
REDACTED = "<redacted>"


def path_for(series: str) -> str:
    """API path for a series id (petroleum spot vs natural gas)."""
    return NATURAL_GAS_PATH if series in NATURAL_GAS_SERIES else PETROLEUM_SPOT_PATH


def scrub_api_key(content: bytes, api_key: str) -> bytes:
    """Return ``content`` with every occurrence of ``api_key`` replaced by :data:`REDACTED`.

    Applied to the raw bytes, not to a parsed object, so that the cache still stores the
    vendor's exact formatting except for the secret. Pure function; tested without network.
    """
    return content.replace(api_key.encode("utf-8"), REDACTED.encode("utf-8"))


class EiaSource:
    """Fetch and parse EIA spot price series."""

    name = "eia"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        """Create the source with the key from ``Settings.eia_api_key``.

        Client lifetime and retries as in ``EcbSource`` / ``etl.http``; the key is never logged.
        """
        self._api_key = api_key
        self._client = client

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """Download each series in ``source_ids`` between ``start`` and ``end`` (as given).

        One request per 5,000-row page; each page is its own :class:`RawFile` with
        ``scope = source_id``, ``page``/``pages`` set, ``url`` without the query string and
        the key scrubbed from ``content``. ``start=None`` means full history. HTTP 429 waits
        and retries; other 4xx are final; 5xx retries with back-off.
        """
        files: list[RawFile] = []
        with client_for(self._client) as client:
            for series in source_ids:
                url = BASE_URL + path_for(series)
                params: dict[str, str] = {
                    "api_key": self._api_key,
                    "frequency": "daily",
                    "data[0]": "value",
                    "facets[series][]": series,
                    "sort[0][column]": "period",
                    "sort[0][direction]": "desc",
                    "length": str(PAGE_LENGTH),
                }
                if start is not None:
                    # overlap is applied once, by the orchestrator
                    params["start"] = start.isoformat()
                if end is not None:
                    params["end"] = end.isoformat()
                pages: list[tuple[bytes, datetime]] = []
                offset = 0
                while True:
                    fetched_at = datetime.now(UTC)
                    response = fetch_with_retry(
                        client, url, params={**params, "offset": str(offset)}
                    )
                    payload = response.json()
                    data = payload.get("response", {}).get("data")
                    if data is None:
                        raise ValueError(f"EIA response for {series} has no response.data")
                    pages.append((scrub_api_key(response.content, self._api_key), fetched_at))
                    total = int(payload["response"].get("total", len(data)))
                    offset += len(data)
                    if not data or offset >= total:
                        break
                files.extend(
                    RawFile(self.name, series, url, content, at, page=i, pages=len(pages))
                    for i, (content, at) in enumerate(pages, start=1)
                )
        return files

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Return the page's rows as a long frame.

        Validates that every row's ``series`` equals ``raw.scope`` (``series_mismatch``
        otherwise) and converts the string ``value`` to float. Rows whose ``value`` is
        ``null`` (the API emits them for some holidays [확인 2026-09-13: first live sync])
        are dropped, never filled. Facet fields (``duoarea``,
        ``area-name``, ...) are ignored. ``response.total`` is not checked here because it
        spans pages; the orchestrator compares it with the row count across pages. Result is
        sorted ascending by ``price_date`` with a fresh ``RangeIndex``
        (``contract.Source.parse``); the API returns descending order.
        """
        rows = [
            r for r in json.loads(raw.content)["response"]["data"] if r.get("value") is not None
        ]
        bad = {r.get("series") for r in rows} - {raw.scope}
        if bad:
            got = sorted(map(str, bad))
            raise ValueError(f"series_mismatch: expected {raw.scope}, got {got}")
        out = pd.DataFrame(
            {
                "source_id": pd.Series([raw.scope] * len(rows), dtype="string"),
                "price_date": pd.to_datetime([r["period"] for r in rows]).astype("datetime64[ns]"),
                "close": pd.Series([float(r["value"]) for r in rows], dtype="float64"),
            }
        )
        out["adj_close"] = out["close"]
        out["volume"] = pd.array([pd.NA] * len(out), dtype="Int64")
        out = out.sort_values(["source_id", "price_date"], kind="mergesort").reset_index(drop=True)
        return out[list(PRICE_COLUMNS)]
