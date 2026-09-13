"""validate() tests. Frames are built directly from fixtures so they do not depend on parse()."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from risk_engine.data.etl.contract import InstrumentSpec, JumpThresholds
from risk_engine.data.etl.validate import load_thresholds, thresholds_sha256, validate

FIXTURES = Path(__file__).parent / "fixtures" / "etl"
REPO_ROOT = Path(__file__).resolve().parents[1]


WTI = InstrumentSpec(1, "eia", "RWTC", "WTI", "price", "absolute")
EURUSD = InstrumentSpec(2, "ecb", "USD", "EURUSD", "price", "log")
UST10 = InstrumentSpec(3, "fred", "DGS10", "UST_10Y", "yield", "absolute")
TH = JumpThresholds(max_abs_return=0.30, max_abs_change_bp=100.0, max_abs_change=10.0)
TODAY = date(2026, 9, 13)


def _frame(source_id: str, values: dict[str, float]) -> pd.DataFrame:
    dates = pd.to_datetime(list(values))
    return pd.DataFrame(
        {
            "source_id": pd.Series([source_id] * len(values), dtype="string"),
            "price_date": dates,
            "close": list(values.values()),
            "adj_close": list(values.values()),
            "volume": pd.Series([pd.NA] * len(values), dtype="Int64"),
        }
    )


def _wti_april_2020() -> pd.DataFrame:
    rows = json.loads((FIXTURES / "eia_rwtc_2020-04.json").read_text())["response"]["data"]
    return _frame(
        "RWTC", {r["period"]: float(r["value"]) for r in sorted(rows, key=lambda r: r["period"])}
    )


def test_wti_2020_04_20_is_warning_not_error() -> None:
    report = validate(_wti_april_2020(), WTI, TH, today=TODAY)
    assert report.ok, [f for f in report.findings if f.level == "error"]
    codes = {(f.code, f.level) for f in report.findings}
    assert ("nonpositive_price", "warning") in codes  # -36.98 recorded, not rejected
    assert ("jump", "warning") in codes  # |ΔP| = 55.29 > 10 USD/bbl
    jump = next(
        f for f in report.findings if f.code == "jump" and f.price_date == date(2020, 4, 20)
    )
    assert jump.value == pytest.approx(-55.29) and jump.threshold == 10.0
    assert jump.prev_date == date(2020, 4, 17) and jump.gap_days == 3  # over the weekend
    assert (
        report.rows == 15 and report.first == date(2020, 4, 13) and report.last == date(2020, 5, 1)
    )


def test_absolute_series_never_uses_percentage_rule() -> None:
    # 1.0 -> 2.0 is +100 % but only +1 USD: no jump warning on an absolute series
    report = validate(_frame("RWTC", {"2020-01-02": 1.0, "2020-01-03": 2.0}), WTI, TH, today=TODAY)
    assert not any(f.code == "jump" for f in report.findings)


def test_log_series_nonpositive_is_error_and_percentage_jump_warns() -> None:
    bad = validate(_frame("USD", {"2020-01-02": 1.1, "2020-01-03": 0.0}), EURUSD, TH, today=TODAY)
    assert not bad.ok and any(
        f.code == "nonpositive_price" and f.level == "error" for f in bad.findings
    )
    jumpy = validate(_frame("USD", {"2020-01-02": 1.0, "2020-01-03": 1.4}), EURUSD, TH, today=TODAY)
    assert jumpy.ok and any(f.code == "jump" for f in jumpy.findings)


def test_yield_jump_in_basis_points() -> None:
    # 0.93 % -> 2.00 % is +107 bp
    report = validate(
        _frame("10 Yr", {"2020-01-02": 0.93, "2020-01-03": 2.00}), UST10, TH, today=TODAY
    )
    jump = next(f for f in report.findings if f.code == "jump")
    assert jump.value == pytest.approx(107.0) and jump.threshold == 100.0


def test_structural_errors() -> None:
    dup = pd.concat([_frame("USD", {"2020-01-02": 1.1})] * 2, ignore_index=True)
    assert {f.code for f in validate(dup, EURUSD, TH, today=TODAY).findings} >= {"duplicate_date"}
    future = validate(_frame("USD", {"2030-01-01": 1.1}), EURUSD, TH, today=TODAY)
    assert any(f.code == "future_date" and f.level == "error" for f in future.findings)
    empty = validate(_frame("USD", {}), EURUSD, TH, today=TODAY)
    assert any(f.code == "empty_frame" for f in empty.findings)
    missing = _frame("USD", {"2020-01-02": 1.1}).drop(columns=["adj_close"])
    assert any(
        f.code == "missing_column" for f in validate(missing, EURUSD, TH, today=TODAY).findings
    )


def test_validate_does_not_mutate_frame() -> None:
    frame = _wti_april_2020()
    before = frame.copy(deep=True)
    validate(frame, WTI, TH, today=TODAY)
    pd.testing.assert_frame_equal(frame, before)


def test_thresholds_from_config() -> None:
    path = REPO_ROOT / "config" / "validation.toml"
    wti = load_thresholds("WTI", path)
    assert (wti.max_abs_change, wti.max_abs_return, wti.max_abs_change_bp) == (10.0, 0.30, 100.0)
    assert load_thresholds("HENRYHUB", path).max_abs_change == 2.0
    assert load_thresholds("EURUSD", path).max_abs_change == 10.0  # defaults for unlisted tickers
    assert len(thresholds_sha256(path)) == 64
