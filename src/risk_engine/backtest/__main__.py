"""``python -m risk_engine.backtest run --universe from_1999 --portfolio MAIN``."""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from risk_engine.backtest import record
from risk_engine.backtest.runner import BacktestConfig, backtest, load_inputs
from risk_engine.risk.record import code_version
from risk_engine.settings import SettingsError, load_settings


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="python -m risk_engine.backtest")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--universe", default="from_1999")
    run.add_argument("--portfolio", default="MAIN")
    run.add_argument("--tag", default="daily_batch")
    run.add_argument("--method", default="fhs")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cfg = BacktestConfig.load()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        runs, pnls, hv = load_inputs(
            conn, args.universe, args.portfolio, tag=args.tag, method=args.method, cfg=cfg
        )
        if not runs:
            print("no runs found", file=sys.stderr)
            return 1
        report = backtest(
            runs, pnls, cfg, universe=args.universe, portfolio_code=args.portfolio, horizon_var=hv
        )
        n = record.write(conn, report, code_version())
    total = sum(d.exception for d in report.days)
    raw = sum(d.exception_raw for d in report.days)
    sq = sum(d.exception_sqrt for d in report.days)
    print(
        f"days={len(report.days)} exceptions[{cfg.horizon_method}]={total} raw={raw} sqrt={sq} "
        f"expected={len(report.days) * (1 - cfg.confidence):.1f} windows={n}"
    )
    for w in report.windows:
        print(
            f"{w.window_start}..{w.window_end} n={w.n_obs} x={w.exceptions} "
            f"(raw {w.exceptions_raw}, sqrt {w.exceptions_sqrt}) "
            f"kupiec_p={w.kupiec.p_value:.3f} "
            f"ind_p={w.independence.p_value:.3f} cc_p={w.cc.p_value:.3f} zone={w.traffic_light} "
            f"pla rho={w.pla.spearman:.3f} ks={w.pla.ks:.3f} {w.pla.zone}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
