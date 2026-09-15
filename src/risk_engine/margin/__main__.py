"""Command line: ``python -m risk_engine.margin run|backfill|load-members``.

Mirrors ``python -m risk_engine.risk``: one run, or every aligned date in a range (the
official ``margin_batch`` series for the coverage backtest). Every command propagates
exceptions; nothing reports success on failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import psycopg

from risk_engine.margin import engine
from risk_engine.margin.params import MarginParams
from risk_engine.risk import engine as risk_engine
from risk_engine.risk import record
from risk_engine.risk.positions import load_positions_csv
from risk_engine.settings import SettingsError, load_settings

log = logging.getLogger("risk_engine.margin")


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    parser = argparse.ArgumentParser(prog="python -m risk_engine.margin")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--portfolio", default="MAIN")
        p.add_argument("--universe", default="from_1999")
        p.add_argument("--vol", choices=["ewma", "garch"], default="ewma")
        p.add_argument("--tag", default="margin_adhoc")

    run = sub.add_parser("run", help="margin of one book on one date")
    common(run)
    run.add_argument("--as-of", type=date.fromisoformat, required=True)

    bf = sub.add_parser("backfill", help="every aligned date in a range (coverage backtest)")
    common(bf)
    bf.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    bf.add_argument("--to", dest="end", type=date.fromisoformat, required=True)

    lm = sub.add_parser("load-members", help="upsert the clearing members' positions CSV")
    lm.add_argument("path", type=Path, nargs="?", default=Path("config/positions_members.csv"))
    return parser


def _print_head(run_id: int, res: risk_engine.RunResult) -> None:
    head = {
        m.measure: m.value
        for m in res.measures
        if m.scope_type == "portfolio" and m.measure.startswith("im")
    }
    print(
        f"run_id={run_id} as_of={res.as_of_date} portfolio={res.portfolio_code} "
        f"h={res.horizon_days} pv={res.portfolio_value:,.0f} "
        + " ".join(f"{k}={v:,.0f}" for k, v in head.items())
    )


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
        if args.command == "load-members":
            n = load_positions_csv(conn, args.path)
            print(f"member position rows written: {n}")
            return 0
        rp, mp = risk_engine.RiskParams.load(), MarginParams.load()
        if args.command == "run":
            res = engine.run(
                conn,
                args.portfolio,
                args.as_of,
                universe_name=args.universe,
                risk_params=rp,
                margin_params=mp,
                vol=args.vol,
                tag=args.tag,
            )
            _print_head(record.write(conn, res, version), res)
            return 0
        t0 = time.perf_counter()
        rm, specs, meta = risk_engine.prepare(conn, args.universe, args.end)
        first_eligible = rp.warmup_days + rp.window_days
        eligible = rm.changes.index[first_eligible:]
        dates = [d for d in eligible if args.start <= d.date() <= args.end]
        if not dates:
            print("backfill: no eligible dates in range", file=sys.stderr)
            return 1
        log.info("backfill: first eligible date %s, %d runs", dates[0].date(), len(dates))
        done = 0
        for d in dates:
            sliced = risk_engine.ReturnMatrix(
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
                universe_name=args.universe,
                risk_params=rp,
                margin_params=mp,
                vol=args.vol,
                tag=args.tag,
                prepared=(sliced, specs, {**meta, "last": d.date()}),
            )
            record.write(conn, res, version)
            done += 1
            if done % 250 == 0:
                log.info("backfill %d/%d runs (%.0fs)", done, len(dates), time.perf_counter() - t0)
        print(
            f"backfill complete: {done} margin runs {args.portfolio} {args.start}..{args.end} "
            f"in {time.perf_counter() - t0:.0f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
