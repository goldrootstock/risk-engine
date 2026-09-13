"""Schema tests against a real PostgreSQL (marked ``db``)."""

import datetime as dt
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from risk_engine.data.migrate import status, upgrade

from .conftest import MIGRATIONS_DIR

pytestmark = pytest.mark.db


@pytest.fixture
def migrated(db_conn: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    return db_conn


def _table_names(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {r[0] for r in rows}


INSERT_INSTRUMENT = (
    "INSERT INTO instruments "
    "(source, ticker, source_id, name, asset_class, instrument_type, currency) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING instrument_id"
)


def _insert_spy(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(
        INSERT_INSTRUMENT, ("yfinance", "SPY", "SPY", "SPDR S&P 500 ETF", "equity", "etf", "USD")
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_status_is_read_only_on_fresh_database(db_conn: psycopg.Connection[Any]) -> None:
    pending = status(db_conn, MIGRATIONS_DIR)
    assert pending and not any(applied for _, applied in pending)
    assert _table_names(db_conn) == set()  # status must not create schema_migrations


def test_upgrade_creates_tables_and_is_idempotent(db_conn: psycopg.Connection[Any]) -> None:
    first = upgrade(db_conn, MIGRATIONS_DIR)
    assert [m.label for m in first] == [
        "0001_init",
        "0002_risk_runs",
        "0003_instrument_vocab",
        "0004_etl_runs",
        "0005_rates_to_fred",
        "0006_backtests",
        "0007_stress",
    ]
    expected = {
        "schema_migrations",
        "instruments",
        "prices",
        "positions",
        "risk_measure_types",
        "risk_runs",
        "risk_measures",
        "etl_runs",
        "backtest_results",
        "backtest_summaries",
        "stress_runs",
        "stress_results",
    }
    assert expected <= _table_names(db_conn)

    assert upgrade(db_conn, MIGRATIONS_DIR) == []
    assert all(applied for _, applied in status(db_conn, MIGRATIONS_DIR))


def test_instrument_constraints(migrated: psycopg.Connection[Any]) -> None:
    _insert_spy(migrated)
    with pytest.raises(UniqueViolation):
        migrated.execute(
            INSERT_INSTRUMENT, ("yfinance", "SPY", "SPY", "dup", "equity", "etf", "USD")
        )
    with pytest.raises(CheckViolation):
        migrated.execute(
            INSERT_INSTRUMENT, ("yfinance", "X", "X", "bad class", "crypto", "etf", "USD")
        )


def test_prices_upsert_updates_existing_row(migrated: psycopg.Connection[Any]) -> None:
    spy = _insert_spy(migrated)
    upsert = (
        "INSERT INTO prices (instrument_id, price_date, close, adj_close, volume) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (instrument_id, price_date) DO UPDATE SET "
        "close = EXCLUDED.close, adj_close = EXCLUDED.adj_close, "
        "volume = EXCLUDED.volume, loaded_at = now()"
    )
    day = dt.date(2024, 1, 2)
    migrated.execute(upsert, (spy, day, 470.0, 465.5, 80_000_000))
    migrated.execute(upsert, (spy, day, 471.0, 466.5, None))

    row = migrated.execute(
        "SELECT close, adj_close, volume FROM prices WHERE instrument_id = %s AND price_date = %s",
        (spy, day),
    ).fetchone()
    assert row == (471.0, 466.5, None)
    assert isinstance(row[0], float)  # DOUBLE PRECISION -> float, not Decimal


def test_positions_reject_zero_and_return_decimal(migrated: psycopg.Connection[Any]) -> None:
    spy = _insert_spy(migrated)
    with pytest.raises(CheckViolation):
        migrated.execute(
            "INSERT INTO positions (portfolio_code, as_of_date, instrument_id, quantity) "
            "VALUES ('MAIN', '2024-01-02', %s, 0)",
            (spy,),
        )
    migrated.execute(
        "INSERT INTO positions (portfolio_code, as_of_date, instrument_id, quantity) "
        "VALUES ('MAIN', '2024-01-02', %s, -150.5)",
        (spy,),
    )
    row = migrated.execute("SELECT quantity FROM positions").fetchone()
    assert row == (Decimal("-150.500000"),)  # NUMERIC -> Decimal (ledger value)


# ----------------------------------------------------------------- 0002: risk_runs / risk_measures


def _insert_run(conn: psycopg.Connection[Any], **overrides: Any) -> int:
    row = {
        "portfolio_code": "MAIN",
        "as_of_date": dt.date(2024, 1, 5),
        "positions_as_of": dt.date(2024, 1, 2),
        "method": "fhs",
        "window_days": 500,
        "n_scenarios": 499,
        "portfolio_value": 1_000_000.0,
        "tag": "daily_batch",
    } | overrides
    cur = conn.execute(
        "INSERT INTO risk_runs (portfolio_code, as_of_date, positions_as_of, method, "
        "window_days, n_scenarios, portfolio_value, tag) "
        "VALUES (%(portfolio_code)s, %(as_of_date)s, %(positions_as_of)s, %(method)s, "
        "%(window_days)s, %(n_scenarios)s, %(portfolio_value)s, %(tag)s) RETURNING run_id",
        row,
    )
    out = cur.fetchone()
    assert out is not None
    return int(out[0])


INSERT_MEASURE = (
    "INSERT INTO risk_measures (run_id, measure, confidence, scope_type, scope_key, value) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


def test_measure_catalogue_matches_python_vocab(migrated: psycopg.Connection[Any]) -> None:
    from risk_engine.data.vocab import MEASURE_UNITS, Measure

    rows = migrated.execute("SELECT measure, unit FROM risk_measure_types").fetchall()
    assert dict(rows) == {m.value: u.value for m, u in MEASURE_UNITS.items()}
    assert set(MEASURE_UNITS) == set(Measure)


def test_method_and_scope_vocab_accepted_by_checks(migrated: psycopg.Connection[Any]) -> None:
    from risk_engine.data.vocab import Method, ScopeType

    for method in Method:
        run = _insert_run(migrated, method=method.value)
        for scope in ScopeType:
            migrated.execute(INSERT_MEASURE, (run, "es", 0.975, scope.value, "k", 1.0))
    with pytest.raises(CheckViolation):
        _insert_run(migrated, method="historical")


def test_unknown_measure_rejected_by_fk(migrated: psycopg.Connection[Any]) -> None:
    from psycopg.errors import ForeignKeyViolation

    run = _insert_run(migrated)
    with pytest.raises(ForeignKeyViolation):
        migrated.execute(INSERT_MEASURE, (run, "cvar", 0.975, "portfolio", "", 1.0))


def test_null_confidence_is_still_unique(migrated: psycopg.Connection[Any]) -> None:
    run = _insert_run(migrated)
    migrated.execute(INSERT_MEASURE, (run, "factor_dv01", None, "factor", "rates", 120.0))
    with pytest.raises(UniqueViolation):
        migrated.execute(INSERT_MEASURE, (run, "factor_dv01", None, "factor", "rates", 121.0))


def test_headline_view_returns_currency_and_fraction(migrated: psycopg.Connection[Any]) -> None:
    run = _insert_run(migrated, portfolio_value=2_000_000.0)
    migrated.execute(INSERT_MEASURE, (run, "var", 0.99, "portfolio", "", 50_000.0))
    migrated.execute(INSERT_MEASURE, (run, "es", 0.975, "portfolio", "", 60_000.0))
    migrated.execute(
        INSERT_MEASURE, (run, "component_es", 0.975, "asset_class", "equity", 40_000.0)
    )

    row = migrated.execute(
        "SELECT var_99, es_975, var_99_frac, es_975_frac FROM v_risk_headline WHERE run_id = %s",
        (run,),
    ).fetchone()
    assert row == (50_000.0, 60_000.0, 0.025, 0.03)


def test_deleting_run_cascades_to_measures(migrated: psycopg.Connection[Any]) -> None:
    run = _insert_run(migrated)
    migrated.execute(INSERT_MEASURE, (run, "var", 0.99, "portfolio", "", 1.0))
    migrated.execute("DELETE FROM risk_runs WHERE run_id = %s", (run,))
    assert migrated.execute("SELECT count(*) FROM risk_measures").fetchone() == (0,)


# ----------------------------------------------------------------- 0003 + config/universe.csv


def test_universe_csv_loads_into_instruments(migrated: psycopg.Connection[Any]) -> None:
    """Every row of config/universe.csv satisfies the instruments constraints."""
    import csv

    from .conftest import REPO_ROOT

    with (REPO_ROOT / "config" / "universe.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 31
    assert len({(r["source"], r["ticker"]) for r in rows}) == 31

    migrated.cursor().executemany(
        "INSERT INTO instruments (source, ticker, source_id, name, asset_class, "
        "instrument_type, quote_type, return_type, currency, multiplier) "
        "VALUES (%(source)s, %(ticker)s, %(source_id)s, %(name)s, %(asset_class)s, "
        "%(instrument_type)s, %(quote_type)s, %(return_type)s, %(currency)s, %(multiplier)s)",
        rows,
    )
    counts = dict(
        migrated.execute(
            "SELECT asset_class, count(*) FROM instruments GROUP BY asset_class"
        ).fetchall()
    )
    assert counts == {"rates": 11, "fx": 12, "commodity": 8}
    assert migrated.execute(
        "SELECT count(*) FROM instruments WHERE quote_type = 'yield' AND return_type <> 'absolute'"
    ).fetchone() == (0,)


def test_return_type_and_instrument_type_vocab(migrated: psycopg.Connection[Any]) -> None:
    with pytest.raises(CheckViolation):
        migrated.execute(
            INSERT_INSTRUMENT, ("eia", "X", "X", "bad type", "commodity", "spot", "USD")
        )
    with pytest.raises(CheckViolation):
        migrated.execute(
            "INSERT INTO instruments (source, ticker, source_id, name, asset_class, "
            "instrument_type, currency, return_type) "
            "VALUES ('eia', 'Y', 'Y', 'bad return', 'commodity', 'commodity_spot', 'USD', 'simple')"
        )


def test_headline_view_guards_zero_portfolio_value(migrated: psycopg.Connection[Any]) -> None:
    run = _insert_run(migrated, portfolio_value=0.0)
    migrated.execute(INSERT_MEASURE, (run, "var", 0.99, "portfolio", "", 50_000.0))
    row = migrated.execute(
        "SELECT var_99, var_99_frac FROM v_risk_headline WHERE run_id = %s", (run,)
    ).fetchone()
    assert row == (50_000.0, None)


def test_0005_moves_rates_to_fred_keeping_instrument_ids(migrated: psycopg.Connection[Any]) -> None:
    """Replaying 0005 on a legacy row: source/source_id change, instrument_id and prices stay."""
    row = migrated.execute(
        "INSERT INTO instruments (source, ticker, source_id, name, asset_class, instrument_type, "
        "quote_type, return_type, currency) VALUES ('ustreasury', 'UST_10Y', '10 Yr', "
        "'U.S. Treasury par yield 10-year', 'rates', 'yield_curve', 'yield', 'absolute', 'USD') "
        "RETURNING instrument_id"
    ).fetchone()
    assert row is not None
    iid = int(row[0])
    migrated.execute(
        "INSERT INTO prices (instrument_id, price_date, close, adj_close) "
        "VALUES (%s, '2020-04-20', 0.63, 0.63)",
        (iid,),
    )
    migrated.execute((MIGRATIONS_DIR / "0005_rates_to_fred.sql").read_bytes())
    got = migrated.execute(
        "SELECT source, source_id, name FROM instruments WHERE instrument_id = %s", (iid,)
    ).fetchone()
    assert got == ("fred", "DGS10", "U.S. Treasury constant-maturity yield (H.15 via FRED) 10-year")
    assert migrated.execute(
        "SELECT count(*) FROM prices WHERE instrument_id = %s", (iid,)
    ).fetchone() == (1,)
