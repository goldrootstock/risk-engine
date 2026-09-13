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
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

from risk_engine.data.etl.cache import RawCache, sha256_hex
from risk_engine.data.etl.contract import Finding, InstrumentSpec, RawFile, Report, Source
from risk_engine.data.etl.load import (
    instrument_specs,
    read_universe,
    record_etl_run,
    upsert_instruments,
    upsert_prices,
)
from risk_engine.data.etl.sources import SOURCE_NAMES, build_sources
from risk_engine.data.etl.validate import load_thresholds, thresholds_sha256, validate
from risk_engine.settings import Settings, SettingsError, load_settings

#: Calendar days re-fetched behind the last loaded date on ``--incremental`` (note 03 §13).
OVERLAP_DAYS = 14

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
    sync.add_argument("--source", choices=list(SOURCE_NAMES), help="one source only")
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
    status.add_argument("--source", choices=list(SOURCE_NAMES))
    return parser


def run_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Orchestrate fetch -> cache -> parse -> validate -> load for the selected series.

    The only function that writes ``etl_runs``. Returns :data:`EXIT_OK` when every series
    loaded, :data:`EXIT_SKIPPED` when any series was skipped or failed.
    """
    sources = build_sources(settings.eia_api_key, settings.fred_api_key)
    wanted = [args.source] if args.source else list(sources)
    missing = [n for n in SOURCE_NAMES if n not in sources and args.source in (None, n)]
    if missing:
        print(
            f"error: API key not set for source(s): {', '.join(missing)} "
            "(EIA_API_KEY / FRED_API_KEY in .env)",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    cache = RawCache(args.cache_dir)
    version = code_version()
    thresholds_path = Path("config/validation.toml")
    base_params: dict[str, Any] = {
        "since": args.since.isoformat() if args.since else None,
        "incremental": args.incremental,
        "offline": args.offline,
        "dry_run": args.dry_run,
        "overlap_days": OVERLAP_DAYS,
        "thresholds_sha256": thresholds_sha256(thresholds_path),
    }
    problems = 0
    # autocommit: every conn.transaction() below is a real transaction committed per series.
    # Without it psycopg opens an implicit transaction at the first SELECT and nothing is
    # committed until the connection closes.
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        upsert_instruments(conn, read_universe(args.universe))
        for name in wanted:
            specs = instrument_specs(conn, name)
            if args.ticker:
                specs = {k: v for k, v in specs.items() if v.ticker in args.ticker}
            if not specs:
                continue
            start = _start_for(conn, specs, args)
            problems += _sync_source(
                conn,
                sources[name],
                specs,
                start,
                cache,
                args,
                base_params,
                version,
                thresholds_path,
            )
    return EXIT_SKIPPED if problems else EXIT_OK


def _start_for(
    conn: psycopg.Connection[Any], specs: dict[str, InstrumentSpec], args: argparse.Namespace
) -> date | None:
    """``--since`` as given, or on ``--incremental`` the earliest last-loaded date minus overlap."""
    if not args.incremental:
        since: date | None = args.since
        return since
    ids = [s.instrument_id for s in specs.values()]
    row = conn.execute(
        "SELECT min(last_date) FROM (SELECT instrument_id, max(price_date) AS last_date "
        "FROM prices WHERE instrument_id = ANY(%s) GROUP BY instrument_id) t "
        "HAVING count(*) = %s",
        (ids, len(ids)),
    ).fetchone()
    if row is None or not isinstance(row[0], date):
        return None  # at least one series has never been loaded: full history
    last: date = row[0]
    return last - timedelta(days=OVERLAP_DAYS)


def _sync_source(
    conn: psycopg.Connection[Any],
    source: Source,
    specs: dict[str, InstrumentSpec],
    start: date | None,
    cache: RawCache,
    args: argparse.Namespace,
    base_params: dict[str, Any],
    version: str | None,
    thresholds_path: Path,
) -> int:
    """Fetch/parse/validate/load every series of one source; returns the number of problems."""
    started = datetime.now(UTC)
    t0 = time.perf_counter()

    def record(
        spec_id: str,
        spec: InstrumentSpec | None,
        status: str,
        report: Report | None,
        result: Any,
        sha: str | None,
    ) -> None:
        record_etl_run(
            conn,
            started_at=started,
            finished_at=datetime.now(UTC),
            source=source.name,
            source_id=spec_id,
            instrument_id=spec.instrument_id if spec else None,
            status=status,
            report=report,
            result=result,
            cache_sha256=sha,
            params=base_params,
            code_version=version,
        )

    try:
        if args.offline:
            raws = _offline_files(cache, source.name)
        else:
            raws = list(source.fetch(list(specs), start, None))
            for raw in raws:
                cache.store(raw)
        frames = [source.parse(raw) for raw in raws]
    except Exception as exc:  # fetch/parse failure applies to every series of the source
        log.error("%s: %s: %s", source.name, type(exc).__name__, exc)
        report = Report(
            source.name,
            "*",
            0,
            None,
            None,
            (Finding("error", "http_error", f"{type(exc).__name__}: {exc}"),),
        )
        for sid, spec in specs.items():
            record(sid, spec, "failed", report, None, None)
        return len(specs)

    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(specs))
    sha_by_scope = {raw.scope: sha256_hex(raw.content) for raw in raws}
    problems = 0
    for sid, spec in specs.items():
        series = frame[frame["source_id"] == sid].reset_index(drop=True) if len(frame) else frame
        report = validate(series, spec, load_thresholds(spec.ticker, thresholds_path))
        sha = (
            sha_by_scope.get(sid)
            or sha_by_scope.get("_all")
            or next(iter(sha_by_scope.values()), None)
        )
        if not report.ok:
            problems += 1
            record(sid, spec, "skipped", report, None, sha)
            log.warning(
                "%s/%s skipped: %s",
                source.name,
                sid,
                [f.code for f in report.findings if f.level == "error"],
            )
            continue
        if args.dry_run:
            record(sid, spec, "dry_run", report, None, sha)
            log.info(
                "%s/%s dry-run rows=%d findings=%d",
                source.name,
                sid,
                report.rows,
                len(report.findings),
            )
            continue
        try:
            result = upsert_prices(conn, spec, series)
        except Exception as exc:
            problems += 1
            failed = Report(
                report.source,
                report.source_id,
                report.rows,
                report.first,
                report.last,
                (
                    *report.findings,
                    Finding("error", "http_error", f"load failed: {type(exc).__name__}: {exc}"),
                ),
            )
            record(sid, spec, "failed", failed, None, sha)
            log.error("%s/%s load failed: %s", source.name, sid, exc)
            continue
        record(sid, spec, "loaded", report, result, sha)
        log.info(
            "%s/%s %s..%s rows=%d inserted=%d updated=%d unchanged=%d findings=%d elapsed=%.1fs",
            source.name,
            sid,
            report.first,
            report.last,
            result.fetched,
            result.inserted,
            result.updated,
            result.unchanged,
            len(report.findings),
            time.perf_counter() - t0,
        )
    return problems


def _offline_files(cache: RawCache, source: str) -> list[RawFile]:
    """Latest cached payload of every scope of ``source``."""
    base = cache.root / source
    if not base.is_dir():
        return []
    files: list[RawFile] = []
    for scope_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        files.extend(cache.latest(source, scope_dir.name))
    return files


def code_version() -> str | None:
    """Short git SHA of the working tree, or ``None`` outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def run_status(args: argparse.Namespace, settings: Settings) -> int:
    """Print per-series coverage from ``prices`` and the last ``etl_runs`` row. Read-only."""
    sql = """
        SELECT i.source, i.ticker, i.source_id,
               min(p.price_date), max(p.price_date), count(p.price_date), max(p.loaded_at),
               (SELECT status FROM etl_runs r WHERE r.instrument_id = i.instrument_id
                 ORDER BY finished_at DESC LIMIT 1)
        FROM instruments i LEFT JOIN prices p USING (instrument_id)
        WHERE i.is_active AND (%(source)s::text IS NULL OR i.source = %(source)s::text)
        GROUP BY i.instrument_id ORDER BY i.source, i.instrument_id
    """
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        rows = conn.execute(sql, {"source": args.source}).fetchall()
    header = ("source", "ticker", "first", "last", "rows", "last loaded", "last run")
    print(
        f"{header[0]:<11}{header[1]:<15}{header[2]:<12}{header[3]:<12}{header[4]:>7}  "
        f"{header[5]:<26}{header[6]}"
    )
    for source, ticker, _sid, first, last, rows_n, loaded_at, status in rows:
        first_s, last_s = f"{first or '-'!s}", f"{last or '-'!s}"
        loaded_s = loaded_at.isoformat(timespec="seconds") if loaded_at else "-"
        print(
            f"{source:<11}{ticker:<15}{first_s:<12}{last_s:<12}{rows_n:>7}  "
            f"{loaded_s:<26}{status or '-'}"
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, load settings, dispatch."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, query string included: that would print the EIA
    # API key. Only its warnings are allowed through.
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
