"""Plain-SQL migration runner.

Migrations are ``db/migrations/NNNN_name.sql`` files applied in ascending version order.
Each file is executed in one transaction together with its bookkeeping row in
``schema_migrations``: PostgreSQL DDL is transactional, so a failing migration leaves
nothing behind and can simply be fixed and re-run.

Usage::

    python -m risk_engine.data.migrate            # apply pending migrations
    python -m risk_engine.data.migrate status     # show applied / pending
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from risk_engine.settings import load_settings

log = logging.getLogger(__name__)

_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_BOOTSTRAP = b"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True, slots=True, order=True)
class Migration:
    """One migration file. Ordered by ``version``."""

    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        """``0001_init`` style identifier used in logs."""
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path) -> list[Migration]:
    """Return all migrations in ``directory`` sorted by version.

    Raises:
        FileNotFoundError: if ``directory`` does not exist.
        ValueError: on a malformed file name or a duplicated version number.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")

    found: list[Migration] = []
    for path in directory.glob("*.sql"):
        match = _FILENAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file name must match NNNN_name.sql: {path.name}")
        found.append(Migration(int(match["version"]), match["name"], path))

    found.sort()
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions in {directory}: {sorted(versions)}")
    return found


def applied_versions(conn: psycopg.Connection[Any]) -> dict[int, str]:
    """Return ``{version: name}`` of applied migrations. Read-only.

    On a database that has never been migrated the bookkeeping table does not exist
    yet; that is reported as "nothing applied", not created here. Only :func:`upgrade`
    writes.
    """
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    if exists is None or not exists[0]:
        return {}
    rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    return {int(version): str(name) for version, name in rows}


def upgrade(conn: psycopg.Connection[Any], directory: Path) -> list[Migration]:
    """Apply every pending migration in order and return the ones applied.

    Each migration and its ``schema_migrations`` row are committed together.
    Running this twice is a no-op the second time.
    """
    with conn.transaction():
        conn.execute(_BOOTSTRAP)
    applied = applied_versions(conn)
    done: list[Migration] = []
    for migration in discover(directory):
        if migration.version in applied:
            continue
        with conn.transaction():
            # bytes rather than str: psycopg's typed API only accepts literal strings,
            # and a multi-statement script must be sent without parameters anyway.
            conn.execute(migration.path.read_bytes())
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
        log.info("applied %s", migration.label)
        done.append(migration)
    return done


def status(conn: psycopg.Connection[Any], directory: Path) -> list[tuple[Migration, bool]]:
    """Return ``(migration, is_applied)`` for every migration on disk. Read-only."""
    applied = applied_versions(conn)
    return [(m, m.version in applied) for m in discover(directory)]


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m risk_engine.data.migrate", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["up", "status"], default="up")
    parser.add_argument("--dir", type=Path, default=None, help="override MIGRATIONS_DIR")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    directory: Path = args.dir or settings.migrations_dir

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if args.command == "status":
            for migration, is_applied in status(conn, directory):
                print(f"{'applied' if is_applied else 'pending':8} {migration.label}")
            return 0
        applied = upgrade(conn, directory)
        print(f"applied {len(applied)} migration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
