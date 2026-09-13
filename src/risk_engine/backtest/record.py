"""Write a :class:`~risk_engine.backtest.runner.BacktestReport` (the only backtest writer)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from risk_engine.backtest.runner import BacktestReport

UPSERT_DAY = """
INSERT INTO backtest_results (run_id, universe, portfolio_code, as_of_date, pnl_date, hpl,
                              rtpl, var_99, es_975, exception, attribution)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO UPDATE SET pnl_date = EXCLUDED.pnl_date, hpl = EXCLUDED.hpl,
    rtpl = EXCLUDED.rtpl, var_99 = EXCLUDED.var_99, es_975 = EXCLUDED.es_975,
    exception = EXCLUDED.exception, attribution = EXCLUDED.attribution, created_at = now()
"""

INSERT_WINDOW = """
INSERT INTO backtest_summaries (universe, portfolio_code, window_start, window_end, n_obs,
    exceptions,
    expected, kupiec_lr, kupiec_p, christoffersen_lr, christoffersen_p, cc_lr, cc_p, traffic_light,
    pla_spearman, pla_ks, pla_zone, params, code_version)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write(
    conn: psycopg.Connection[Any], report: BacktestReport, code_version: str | None = None
) -> int:
    """Upsert day rows (keyed by run_id) and append window rows; returns windows written."""
    cfg = report.config
    params = {
        "window_days": cfg.window_days,
        "confidence": cfg.confidence,
        "significance": cfg.significance,
        "backtest_params_sha256": cfg.sha256,
        **report.meta,
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            UPSERT_DAY,
            [
                (
                    d.run_id,
                    report.universe,
                    report.portfolio_code,
                    d.as_of,
                    d.pnl_date,
                    d.hpl,
                    d.rtpl,
                    d.var,
                    d.es,
                    d.exception,
                    Jsonb(d.attribution),
                )
                for d in report.days
            ],
        )
        cur.executemany(
            INSERT_WINDOW,
            [
                (
                    report.universe,
                    report.portfolio_code,
                    w.window_start,
                    w.window_end,
                    w.n_obs,
                    w.exceptions,
                    w.expected,
                    w.kupiec.statistic,
                    w.kupiec.p_value,
                    w.independence.statistic,
                    w.independence.p_value,
                    w.cc.statistic,
                    w.cc.p_value,
                    w.traffic_light,
                    w.pla.spearman,
                    w.pla.ks,
                    w.pla.zone,
                    Jsonb(params),
                    code_version,
                )
                for w in report.windows
            ],
        )
    return len(report.windows)
