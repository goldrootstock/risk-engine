"""Command line: ``python -m risk_engine.data.etl sync|status`` (design note 03 §7).

Exit codes (also in README):
    0  every requested series loaded (or, for ``status``, printed)
    1  configuration or infrastructure error: missing DATABASE_URL / EIA_API_KEY, database
       unreachable, unknown source
    2  the run finished but at least one series was skipped or failed — never a silent
       success; details are in ``etl_runs`` and the log
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from risk_engine.settings import Settings, SettingsError, load_settings

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_SKIPPED = 2

log = logging.getLogger("risk_engine.etl")


def build_parser() -> argparse.ArgumentParser:
    """Argument parser; ``--help`` documents the exit codes."""
    parser = argparse.ArgumentParser(
        prog="python -m risk_engine.data.etl",
        description="Load vendor price series into PostgreSQL.",
        epilog=(
            "exit codes: 0 all series loaded; 1 configuration/infrastructure error; "
            "2 at least one series skipped or failed (see etl_runs)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="fetch, validate and load series")
    sync.add_argument("--source", choices=["ecb", "ustreasury", "eia"], help="one source only")
    sync.add_argument("--ticker", action="append", default=[], help="restrict to ticker(s)")
    sync.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="first date to fetch (YYYY-MM-DD); default: full history",
    )
    sync.add_argument(
        "--incremental",
        action="store_true",
        help="fetch from the last loaded date minus the overlap window instead of --since",
    )
    sync.add_argument("--offline", action="store_true", help="parse the latest cached payloads")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, cache and validate; write nothing to the database",
    )
    sync.add_argument("--cache-dir", type=Path, default=Path("data/raw"))
    sync.add_argument("--universe", type=Path, default=Path("config/universe.csv"))

    status = sub.add_parser("status", help="per-series first/last date and last load (read-only)")
    status.add_argument("--source", choices=["ecb", "ustreasury", "eia"])
    return parser


def run_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Orchestrate fetch -> cache -> parse -> validate -> load for the selected series.

    The only function that writes ``etl_runs``. Returns :data:`EXIT_OK` when every series
    loaded, :data:`EXIT_SKIPPED` when any series was skipped or failed.
    """
    raise NotImplementedError


def run_status(args: argparse.Namespace, settings: Settings) -> int:
    """Print per-series coverage from ``prices`` and the last ``etl_runs`` row. Read-only."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, load settings, dispatch."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.command == "status":
        return run_status(args, settings)
    return run_sync(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
