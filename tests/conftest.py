"""Shared pytest fixtures and marker handling."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``db`` when no DATABASE_URL is configured.

    Locally you can run only the pure-Python tests without starting PostgreSQL;
    in CI the service container always sets DATABASE_URL so nothing is skipped.
    """
    if os.environ.get("DATABASE_URL"):
        return
    reason = "DATABASE_URL not set; start `docker compose up -d` and export it"
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db_conn() -> Iterator[psycopg.Connection[Any]]:
    """Autocommit connection whose search_path points at a throwaway schema.

    Every test gets a fresh, empty schema (so migrations apply from scratch) and the
    schema is dropped afterwards, leaving the shared database untouched.
    """
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        try:
            yield conn
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
