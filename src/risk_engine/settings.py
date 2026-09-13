"""Runtime configuration read from environment variables (and an optional ``.env`` file).

Only two knobs exist today; keep it that way until a third one is genuinely needed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MIGRATIONS_DIR = Path("db/migrations")


class SettingsError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable bag of runtime configuration.

    Attributes:
        database_url: PostgreSQL connection URL, e.g. ``postgresql://risk:risk@localhost:5432/risk``.
        migrations_dir: Directory containing ``NNNN_name.sql`` migration files.
        eia_api_key: EIA API v2 key (``EIA_API_KEY``); ``None`` until the EIA source is used.
            Never logged, never written to the cache.
    """

    database_url: str
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
    eia_api_key: str | None = None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: the process environment).

    When ``env`` is ``None`` a ``.env`` file in the current directory is loaded first;
    values already present in the environment are never overridden by the file.

    Args:
        env: Explicit mapping to read from, mainly for tests. Skips ``.env`` loading.

    Raises:
        SettingsError: if ``DATABASE_URL`` is missing or empty.
    """
    if env is None:
        import os

        # Explicit path: the no-argument form walks the call stack to locate the caller's
        # directory and asserts when there is none (stdin scripts, REPL, notebooks).
        load_dotenv(Path.cwd() / ".env")
        env = os.environ

    url = env.get("DATABASE_URL", "").strip()
    if not url:
        raise SettingsError("DATABASE_URL is not set (copy .env.example to .env)")

    migrations_dir = Path(env.get("MIGRATIONS_DIR", "").strip() or DEFAULT_MIGRATIONS_DIR)
    eia_api_key = env.get("EIA_API_KEY", "").strip() or None
    return Settings(database_url=url, migrations_dir=migrations_dir, eia_api_key=eia_api_key)
