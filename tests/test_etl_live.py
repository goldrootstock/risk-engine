"""Live checks against the real vendor endpoints (marker ``network``).

Skipped unless ``RUN_NETWORK_TESTS=1``; never run in CI. Purpose: detect a vendor changing
its format or blocking our client before a scheduled sync does.
"""

import os
from datetime import date

import pytest

from risk_engine.data.etl.sources.ecb import SCOPE_ALL, EcbSource
from risk_engine.data.etl.sources.ustreasury import UsTreasurySource

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


def test_live_treasury_current_year_parses() -> None:
    year = date.today().year
    files = UsTreasurySource().fetch(["10 Yr"], date(year, 1, 1), None)
    assert [f.scope for f in files] == [str(year)]
    frame = UsTreasurySource().parse(files[0])
    assert "10 Yr" in set(frame["source_id"])
