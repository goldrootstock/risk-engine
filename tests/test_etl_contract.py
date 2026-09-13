"""Pure tests of the ETL types (no skeleton bodies involved)."""

from datetime import date

from risk_engine.data.etl.contract import PRICE_COLUMNS, Finding, Report


def test_report_ok_only_without_errors() -> None:
    base = dict(
        source="eia", source_id="RWTC", rows=1, first=date(2020, 4, 20), last=date(2020, 4, 20)
    )
    assert Report(**base).ok
    assert Report(**base, findings=(Finding("warning", "jump", "big move"),)).ok
    assert not Report(**base, findings=(Finding("error", "duplicate_date", "x"),)).ok


def test_price_columns_contract() -> None:
    assert list(PRICE_COLUMNS) == ["source_id", "price_date", "close", "adj_close", "volume"]
    assert PRICE_COLUMNS["volume"] == "Int64"  # nullable integer, not int64
