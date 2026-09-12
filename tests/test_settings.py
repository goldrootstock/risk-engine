from pathlib import Path

import pytest

from risk_engine.settings import DEFAULT_MIGRATIONS_DIR, SettingsError, load_settings


def test_reads_database_url_and_defaults() -> None:
    s = load_settings({"DATABASE_URL": "postgresql://u:p@h:5432/d"})
    assert s.database_url == "postgresql://u:p@h:5432/d"
    assert s.migrations_dir == DEFAULT_MIGRATIONS_DIR


def test_migrations_dir_override() -> None:
    s = load_settings({"DATABASE_URL": "x", "MIGRATIONS_DIR": "/tmp/m"})
    assert s.migrations_dir == Path("/tmp/m")


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}])
def test_missing_database_url_raises(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        load_settings(env)
