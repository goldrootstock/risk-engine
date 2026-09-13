"""Command line: ``python -m risk_engine.risk run|backfill|load-positions``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import psycopg

from risk_engine.risk import engine, record
from risk_engine.risk.positions import load_positions_csv
from risk_engine.settings import SettingsError, load_settings

log = logging.getLogger("risk_engine.risk")


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    parser = argparse.ArgumentParser(prog="python -m risk_engine.risk")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--portfolio", default="MAIN")
        p.add_argument("--method", choices=["fhs", "parametric", "mc"], default="fhs")
        p.add_argument("--universe", default="default")
        p.add_argument("--vol", choices=["ewma", "garch"], default="ewma")
        p.add_argument("--tag", default="adhoc")

    run = sub.add_parser("run", help="one date")
    common(run)
    run.add_argument("--as-of", type=date.fromisoformat, required=True)

    bf = sub.add_parser("backfill", help="every aligned date in a range (for backtests)")
    common(bf)
    bf.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    bf.add_argument("--to", dest="end", type=date.fromisoformat, required=True)

    lp = sub.add_parser("load-positions", help="upsert a positions CSV")
    lp.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    version = record.code_version()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if args.command == "load-positions":
            n = load_positions_csv(conn, args.path)
            print(f"positions rows written: {n}")
            return 0
        params = engine.RiskParams.load()
        if args.command == "run":
            res = engine.run(
                conn,
                args.portfolio,
                args.as_of,
                method=args.method,
                universe_name=args.universe,
                params=params,
                vol=args.vol,
                tag=args.tag,
            )
            run_id = record.write(conn, res, version)
            head = {m.measure: m.value for m in res.measures if m.scope_type == "portfolio"}
            print(
                f"run_id={run_id} as_of={res.as_of_date} pv={res.portfolio_value:,.0f} "
                + " ".join(f"{k}={v:,.0f}" for k, v in head.items())
            )
            return 0
        # backfill: build the return matrix once to the end date and slice per date
        t0 = time.perf_counter()
        rm, specs, meta = engine.prepare(conn, args.universe, args.end)
        dates = [d for d in rm.changes.index if args.start <= d.date() <= args.end]
        done = 0
        for d in dates:
            sliced = engine.ReturnMatrix(
                changes=rm.changes.loc[:d],
                levels=rm.levels.loc[:d],
                kind=rm.kind,
                factor_of=rm.factor_of,
                meta={**rm.meta, "last": d.date(), "n_obs": int((rm.changes.index <= d).sum())},
            )
            res = engine.run(
                conn,
                args.portfolio,
                d.date(),
                method=args.method,
                universe_name=args.universe,
                params=params,
                vol=args.vol,
                tag=args.tag,
                prepared=(sliced, specs, {**meta, "last": d.date()}),
            )
            record.write(conn, res, version)
            done += 1
            if done % 250 == 0:
                log.info(
                    "backfill %s: %d/%d runs (%.0fs)",
                    args.method,
                    done,
                    len(dates),
                    time.perf_counter() - t0,
                )
        elapsed = time.perf_counter() - t0
        print(
            f"backfill complete: {done} runs {args.method} "
            f"{args.start}..{args.end} in {elapsed:.0f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
