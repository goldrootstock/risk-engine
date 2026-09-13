"""Live checks against the real vendor endpoints (marker ``network``).

Skipped unless ``RUN_NETWORK_TESTS=1``; never run in CI. Purpose: detect a vendor changing
its format or blocking our client before a scheduled sync does.
"""

import os
from datetime import date

import pytest

from risk_engine.data.etl.sources.ecb import SCOPE_ALL, EcbSource
from risk_engine.data.etl.sources.fred import FredSource

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("RUN_NETWORK_TESTS") != "1", reason="set RUN_NETWORK_TESTS=1"
    ),
]


def test_live_ecb_zip_parses() -> None:
    (raw,) = EcbSource().fetch(["USD"], None, None)
    assert raw.scope == SCOPE_ALL
    frame = EcbSource().parse(raw)
    assert {"USD", "JPY", "KRW"} <= set(frame["source_id"])
    assert frame["price_date"].min().date() == date(1999, 1, 4)


def test_live_fred_dgs10_parses() -> None:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        pytest.skip("FRED_API_KEY not set")
    (raw,) = FredSource(api_key=key).fetch(["DGS10"], date(2020, 4, 1), date(2020, 4, 30))
    frame = FredSource(api_key=key).parse(raw)
    assert set(frame["source_id"]) == {"DGS10"}
    assert frame.set_index("price_date")["close"][date(2020, 4, 20).isoformat()] == 0.63
