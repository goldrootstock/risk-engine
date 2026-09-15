"""Command line: ``python -m risk_engine.margin run|backfill|load-members|coverage|default-fund``.

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
from typing import Any

import psycopg

from risk_engine.margin import coverage, default_fund, engine
from risk_engine.margin.params import MarginParams
from risk_engine.risk import engine as risk_engine
from risk_engine.risk import record, stress
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

    cv = sub.add_parser("coverage", help="coverage backtest of the recorded margin series")
    cv.add_argument("--portfolio", default="MAIN")
    cv.add_argument("--universe", default="from_1999")
    cv.add_argument("--tag", default=engine.OFFICIAL_TAG)

    dfp = sub.add_parser("default-fund", help="Cover-N default fund from members' stress losses")
    dfp.add_argument("--as-of", type=date.fromisoformat, required=True)
    dfp.add_argument("--universe", default="from_1999")
    dfp.add_argument("--members", default="CM_ENERGY,CM_RATES,CM_DIVERSIFIED,CM_HEDGED")
    dfp.add_argument("--tag", default=engine.OFFICIAL_TAG, help="tag of the members' margin runs")
    dfp.add_argument(
        "--run-margin",
        action="store_true",
        help="compute and record a margin run for each member on --as-of first",
    )
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
        if args.command == "coverage":
            return run_coverage(conn, args, rp, mp, version)
        if args.command == "default-fund":
            return run_default_fund(conn, args, rp, mp, version)
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


def run_coverage(
    conn: psycopg.Connection[Any],
    args: argparse.Namespace,
    rp: risk_engine.RiskParams,
    mp: MarginParams,
    version: str | None,
) -> int:
    """Read the margin series, test coverage, record the report."""
    cfg = coverage.CoverageConfig.from_params(mp)
    runs, pnls, im_h = coverage.load_inputs(
        conn, args.universe, args.portfolio, cfg=cfg, tag=args.tag, risk_params=rp, margin_params=mp
    )
    if not runs:
        print("coverage: no margin runs for that series", file=sys.stderr)
        return 1
    rep = coverage.backtest(
        runs, pnls, cfg, universe=args.universe, portfolio_code=args.portfolio, im_h=im_h
    )
    n = coverage.record(conn, rep, version)
    days = rep.days
    print(
        f"coverage: {len(days)} days, breaches {sum(d.breach for d in days)} "
        f"(raw {sum(d.breach_raw for d in days)}, core {sum(d.breach_core for d in days)}, "
        f"span {sum(bool(d.breach_span) for d in days)}), {n} windows recorded"
    )
    for w in rep.windows:
        print(
            f"  {w.window_start}..{w.window_end} n={w.n_obs} breaches={w.breaches} "
            f"coverage={w.coverage:.4f} kupiec_p={w.kupiec.p_value:.3f} "
            f"max_shortfall={w.max_shortfall:,.0f}"
        )
    return 0


def run_default_fund(
    conn: psycopg.Connection[Any],
    args: argparse.Namespace,
    rp: risk_engine.RiskParams,
    mp: MarginParams,
    version: str | None,
) -> int:
    """Size the default fund against the members' recorded margins on ``--as-of``."""
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if args.run_margin:
        for code in members:
            res = engine.run(
                conn,
                code,
                args.as_of,
                universe_name=args.universe,
                risk_params=rp,
                margin_params=mp,
                tag=args.tag,
            )
            _print_head(record.write(conn, res, version), res)
    scen = stress.ScenarioSet.load()
    rm, specs, books, ims = default_fund.load_inputs(
        conn, args.universe, args.as_of, members, mpor_days=mp.mpor_days, tag=args.tag
    )
    # official basis first (historical scenarios, worst MPOR window inside each), then the
    # supervisory-reference view (adds the hypothetical shocks) and the path view
    labels = {
        "mpor_historical": "official (historical scenarios, worst MPOR window)",
        "mpor": "supervisory reference (adds hypothetical shocks)",
        "path": "path / liquidity (whole window)",
    }
    for basis in default_fund.BASES:
        horizon = None if basis == "path" else mp.mpor_days
        losses = default_fund.losses_for_basis(rm, books, specs, scen, basis, mp.mpor_days)
        rep = default_fund.cover_n(losses, ims, mp.cover)
        run_id = default_fund.record(
            conn,
            rep,
            universe=args.universe,
            as_of=args.as_of,
            scenario_sha256=scen.sha256,
            margin_params_sha256=mp.sha256,
            params={
                "basis": basis,
                "horizon": horizon,
                "members": members,
                "tag": args.tag,
                "allocation": mp.allocation,
            },
            code_version=version,
            losses=losses,
        )
        print(
            f"default_fund_run_id={run_id} basis={basis} [{labels[basis]}] as_of={args.as_of} "
            f"cover={rep.cover} default_fund={rep.default_fund:,.0f} "
            f"scenario={rep.binding_scenario} members={','.join(rep.binding_members)}"
        )
        for code in members:
            im = ims[code].im
            worst = max(
                (r for r in rep.rows if r.portfolio_code == code), key=lambda r: r.uncovered
            )
            print(
                f"  {code:16} im={im:14,.0f} worst={worst.scenario:22} "
                f"loss={worst.stress_loss:14,.0f} uncovered={worst.uncovered:14,.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
