"""FRED (Federal Reserve Bank of St. Louis) series observations (design note 03 §16).

Used for the constant-maturity Treasury yields DGS1MO … DGS30, whose source is the Board of
Governors H.15 release — the same lineage as the Treasury par yield curve, published on
FRED one business day later (T+1). Official REST API, JSON, free key.

The key travels as the ``api_key`` query parameter (the API offers no header variant
[미확인 2026-09-13: cannot be tested without a key; documented parameters list only
``api_key``]). Consequently: the request URL is never logged, ``RawFile.url`` carries no
query string, and any exception raised by the shared helper names the path only.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import httpx
import pandas as pd

from risk_engine.data.etl.contract import PRICE_COLUMNS, RawFile
from risk_engine.data.etl.http import client_for, fetch_with_retry

BASE_URL = "https://api.stlouisfed.org"
OBSERVATIONS_PATH = "/fred/series/observations"
#: Maximum observations per request [출처: FRED API docs, ``limit`` 1..100000]; the longest
#: daily series here (DGS3MO from 1981) has ~11,500 rows, so one request per series.
LIMIT = 100000
#: FRED writes a missing observation as a lone period.
MISSING = "."


class FredSource:
    """Fetch and parse FRED series observations."""

    name = "fred"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        """Create the source with the key from ``Settings.fred_api_key``; never logged."""
        self._api_key = api_key
        self._client = client

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """One request per series id, ``[start, end]`` as given (``None`` = full history).

        Returns one :class:`RawFile` per series with ``scope = series_id``. FRED does not echo
        the key in the body; the URL stored is the path only.
        """
        files: list[RawFile] = []
        url = BASE_URL + OBSERVATIONS_PATH
        with client_for(self._client) as client:
            for series in source_ids:
                params = {
                    "series_id": series,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "limit": str(LIMIT),
                    "sort_order": "asc",
                }
                if start is not None:
                    params["observation_start"] = start.isoformat()
                if end is not None:
                    params["observation_end"] = end.isoformat()
                fetched_at = datetime.now(UTC)
                response = fetch_with_retry(client, url, params=params)
                payload = response.json()
                if "observations" not in payload:
                    raise ValueError(f"FRED response for {series} has no observations")
                if int(payload.get("count", 0)) > LIMIT:
                    raise ValueError(
                        f"FRED series {series} exceeds one page ({payload['count']} rows)"
                    )
                files.append(RawFile(self.name, series, url, response.content, fetched_at))
        return files

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Observations -> long frame; ``"."`` values are dropped, never filled.

        Result is sorted ascending by ``price_date`` with a fresh ``RangeIndex``
        (``contract.Source.parse``). Values are percent (``0.63`` = 0.63 %), like the
        Treasury file they replace, so ``quote_type = 'yield'`` and bp differencing apply.
        """
        obs = [
            o
            for o in json.loads(raw.content)["observations"]
            if o.get("value") not in (None, MISSING)
        ]
        out = pd.DataFrame(
            {
                "source_id": pd.Series([raw.scope] * len(obs), dtype="string"),
                "price_date": pd.to_datetime([o["date"] for o in obs]).astype("datetime64[ns]"),
                "close": pd.Series([float(o["value"]) for o in obs], dtype="float64"),
            }
        )
        out["adj_close"] = out["close"]
        out["volume"] = pd.array([pd.NA] * len(out), dtype="Int64")
        out = out.sort_values(["source_id", "price_date"], kind="mergesort").reset_index(drop=True)
        return out[list(PRICE_COLUMNS)]
