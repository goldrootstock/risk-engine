"""Fixture-based parse tests (no network)."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from risk_engine.data.etl.contract import PRICE_COLUMNS, RawFile
from risk_engine.data.etl.sources.ecb import CSV_MEMBER, HIST_URL, SCOPE_ALL, EcbSource
from risk_engine.data.etl.sources.eia import EiaSource
from risk_engine.data.etl.sources.fred import FredSource

FIXTURES = Path(__file__).parent / "fixtures" / "etl"
FETCHED_AT = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def _assert_contract(frame: pd.DataFrame) -> None:
    assert list(frame.columns) == list(PRICE_COLUMNS)
    assert str(frame["volume"].dtype) == "Int64"
    assert frame["volume"].isna().all()
    assert (frame["close"] == frame["adj_close"]).all()
    assert frame.equals(frame.sort_values(["source_id", "price_date"]).reset_index(drop=True))


def _ecb_raw() -> RawFile:
    csv_bytes = (FIXTURES / "ecb_eurofxref-hist_2020-03_04.csv").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(CSV_MEMBER, csv_bytes)
    return RawFile("ecb", SCOPE_ALL, HIST_URL, buf.getvalue(), FETCHED_AT)


def test_parse_ecb_long_frame() -> None:
    frame = EcbSource().parse(_ecb_raw())
    _assert_contract(frame)
    assert "" not in set(frame["source_id"])  # trailing-comma column dropped
    jpy = frame[frame["source_id"] == "JPY"]
    assert len(jpy) == 42  # every business day of Mar-Apr 2020 has a JPY rate
    assert jpy["price_date"].iloc[0] == pd.Timestamp("2020-03-02")  # ascending
    assert jpy["price_date"].iloc[-1] == pd.Timestamp("2020-04-30")
    # currencies with N/A in the fixture (e.g. CYP, EEK) contribute no rows, never zeros
    assert "CYP" not in set(frame["source_id"])
    assert not (frame["close"] == 0).any()


def test_parse_fred_drops_missing_observations() -> None:
    body = (FIXTURES / "fred_dgs10_2020-03_04.json").read_bytes()
    raw = RawFile(
        "fred", "DGS10", "https://api.stlouisfed.org/fred/series/observations", body, FETCHED_AT
    )
    frame = FredSource(api_key="x").parse(raw)
    _assert_contract(frame)
    assert set(frame["source_id"]) == {"DGS10"}
    assert len(frame) == 43  # 44 observations, 2020-04-10 (Good Friday) is "."
    by_date = frame.set_index("price_date")["close"]
    assert by_date[pd.Timestamp("2020-03-02")] == pytest.approx(1.10)
    assert by_date[pd.Timestamp("2020-04-20")] == pytest.approx(0.63)
    assert pd.Timestamp("2020-04-10") not in by_date.index


def test_parse_eia_strings_and_negative_value() -> None:
    body = (FIXTURES / "eia_rwtc_2020-04.json").read_bytes()
    raw = RawFile("eia", "RWTC", "https://api.eia.gov/v2/petroleum/pri/spt/data/", body, FETCHED_AT)
    frame = EiaSource(api_key="x").parse(raw)
    _assert_contract(frame)
    assert len(frame) == int(json.loads(body)["response"]["total"]) == 15  # total is a string
    by_date = frame.set_index("price_date")["close"]
    assert by_date[pd.Timestamp("2020-04-20")] == pytest.approx(-36.98)
    assert by_date[pd.Timestamp("2020-04-28")] == pytest.approx(12.4)  # "12.4" string
    assert set(frame["source_id"]) == {"RWTC"}


def test_parse_eia_drops_null_values() -> None:
    body = json.loads((FIXTURES / "eia_rwtc_2020-04.json").read_bytes())
    body["response"]["data"].append({"period": "2020-05-04", "series": "RWTC", "value": None})
    raw = RawFile("eia", "RWTC", "https://api.eia.gov/x", json.dumps(body).encode(), FETCHED_AT)
    frame = EiaSource(api_key="x").parse(raw)
    assert len(frame) == 15  # the null row contributes nothing
    assert frame["close"].notna().all()
