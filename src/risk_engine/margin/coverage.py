"""Margin coverage backtest (design note 09 §6).

The official margin series (``risk_runs`` with ``tag = 'margin_batch'`` and
``horizon_days = MPOR``) is compared with the realised MPOR loss of holding the day-t book
(:func:`risk_engine.backtest.hpl.horizon_pnl`). Read-only: :func:`load_inputs` only reads,
:func:`backtest` takes no connection and no margin parameters (note 00 §3 #8 — floors cannot
be re-tuned inside the test), :func:`record` is the only writer of
``margin_coverage_results`` / ``margin_coverage_summaries``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from risk_engine.backtest.hpl import DailyPnl, horizon_pnl, top_attribution
from risk_engine.backtest.statistics import LrTest, kupiec_pof
from risk_engine.margin import core
from risk_engine.margin.engine import OFFICIAL_TAG
from risk_engine.margin.params import MarginParams
from risk_engine.risk.engine import RiskParams, prepare
from risk_engine.risk.positions import load_snapshot
from risk_engine.risk.returns import ReturnMatrix

RUNS_SQL = """
SELECT r.run_id, r.as_of_date,
       max(m.value) FILTER (WHERE m.measure = 'im')             AS im,
       max(m.value) FILTER (WHERE m.measure = 'im_core')        AS im_core,
       max(m.value) FILTER (WHERE m.measure = 'im_span_legacy') AS im_span
FROM risk_runs r JOIN risk_measures m USING (run_id)
WHERE r.universe = %(universe)s AND r.portfolio_code = %(portfolio)s AND r.tag = %(tag)s
  AND r.method = 'fhs' AND r.horizon_days = %(mpor)s AND m.scope_type = 'portfolio'
GROUP BY r.run_id ORDER BY r.as_of_date
"""


@dataclass(frozen=True, slots=True)
class CoverageConfig:
    """Frozen settings of the test itself (target, window); never the margin parameters."""

    target: float
    window_days: int
    mpor_days: int
    sha256: str

    @classmethod
    def from_params(cls, mp: MarginParams) -> CoverageConfig:
        """The ``[coverage]`` section of ``config/margin_params.toml``."""
        return cls(mp.coverage_target, mp.coverage_window_days, mp.mpor_days, mp.sha256)


@dataclass(frozen=True, slots=True)
class MarginRunRecord:
    """One margin run as read from the database."""

    run_id: int
    as_of: date
    im: float
    im_core: float
    im_span: float | None


@dataclass(frozen=True, slots=True)
class DayCoverage:
    """One ``margin_coverage_results`` row."""

    run_id: int
    as_of: date
    pnl_date: date
    h: int
    realised_loss: float
    im: float
    im_core: float
    im_span: float | None
    im_h_block: float | None
    breach: bool
    breach_raw: bool
    breach_core: bool
    breach_span: bool | None
    shortfall: float
    attribution: dict[str, float]


@dataclass(frozen=True, slots=True)
class WindowCoverage:
    """One ``margin_coverage_summaries`` row."""

    window_start: date
    window_end: date
    n_obs: int
    breaches: int
    breaches_raw: int
    breaches_core: int
    breaches_span: int | None
    coverage: float
    target: float
    kupiec: LrTest
    max_shortfall: float
    max_shortfall_over_im: float | None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Everything the writer needs."""

    universe: str
    portfolio_code: str
    days: tuple[DayCoverage, ...]
    windows: tuple[WindowCoverage, ...]
    config: CoverageConfig
    meta: dict[str, Any] = field(default_factory=dict)


def load_inputs(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio_code: str,
    *,
    cfg: CoverageConfig,
    tag: str = OFFICIAL_TAG,
    risk_params: RiskParams | None = None,
    margin_params: MarginParams | None = None,
) -> tuple[list[MarginRunRecord], list[DailyPnl], dict[date, float]]:
    """Read the margin series and the realised MPOR losses per run date. Read-only.

    Transitions that span more business days than the MPOR (holidays, dropped dates) get the
    margin recomputed at that horizon, keyed by run date. Only the trailing ``mpor`` runs may
    lack their realised loss; any other gap raises.
    """
    rows = conn.execute(
        RUNS_SQL,
        {"universe": universe, "portfolio": portfolio_code, "tag": tag, "mpor": cfg.mpor_days},
    ).fetchall()
    runs = [
        MarginRunRecord(
            int(r[0]), r[1], float(r[2]), float(r[3]), float(r[4]) if r[4] is not None else None
        )
        for r in rows
        if r[2] is not None
    ]
    if not runs:
        return [], [], {}
    rp, mp = risk_params or RiskParams.load(), margin_params or MarginParams.load()
    rm, specs, _ = prepare(conn, universe, date.max)
    positions, _ = load_snapshot(conn, portfolio_code, runs[-1].as_of)
    idx = rm.changes.index
    h = cfg.mpor_days
    pnls: list[DailyPnl] = []
    skipped: list[date] = []
    im_h: dict[date, float] = {}
    for run in runs:
        t = pd.Timestamp(run.as_of)
        pos = int(idx.get_indexer(pd.DatetimeIndex([t]))[0])
        if pos < 0 or pos + h >= len(idx):
            skipped.append(run.as_of)
            continue
        p = horizon_pnl(rm, t, h, positions, specs)
        pnls.append(p)
        hb = int(np.busday_count(p.as_of, p.pnl_date))
        if hb > h:
            sliced = ReturnMatrix(
                changes=rm.changes.iloc[: pos + 1],
                levels=rm.levels.iloc[: pos + 1],
                kind=rm.kind,
                factor_of=rm.factor_of,
                meta={},
            )
            im_h[p.as_of] = core.evaluate(
                sliced,
                positions,
                specs,
                lam=rp.lam,
                window=rp.window_days,
                warmup=rp.warmup_days,
                params=mp,
                horizon=hb,
            ).im
    trailing = {r.as_of for r in runs[-h:]}
    if any(d not in trailing for d in skipped):
        raise ValueError(f"{len(skipped)} run dates have no realised {h}-day loss: {skipped[:5]}")
    return runs, pnls, im_h


def backtest(
    runs: list[MarginRunRecord],
    pnls: list[DailyPnl],
    cfg: CoverageConfig,
    *,
    universe: str,
    portfolio_code: str,
    im_h: dict[date, float] | None = None,
) -> CoverageReport:
    """Breaches per day plus window statistics. Pure; nothing here can change a margin."""
    by_date = {p.as_of: p for p in pnls}
    hv = dict(im_h or {})
    days: list[DayCoverage] = []
    for run in runs:
        p = by_date.get(run.as_of)
        if p is None:
            continue
        loss = -p.hpl
        h = int(np.busday_count(p.as_of, p.pnl_date))
        block = hv.get(run.as_of) if h > cfg.mpor_days else None
        if h > cfg.mpor_days and block is None:
            raise ValueError(f"no {h}-day margin for {run.as_of}")
        official = block if block is not None else run.im
        breach, breach_raw = loss > official, loss > run.im
        days.append(
            DayCoverage(
                run_id=run.run_id,
                as_of=run.as_of,
                pnl_date=p.pnl_date,
                h=h,
                realised_loss=loss,
                im=run.im,
                im_core=run.im_core,
                im_span=run.im_span,
                im_h_block=block,
                breach=breach,
                breach_raw=breach_raw,
                breach_core=loss > run.im_core,
                breach_span=(loss > run.im_span) if run.im_span is not None else None,
                shortfall=max(0.0, loss - run.im),
                attribution=top_attribution(p.loss_by_instrument) if (breach or breach_raw) else {},
            )
        )
    w = cfg.window_days
    windows = [summarise(days[i : i + w], cfg) for i in range(0, max(0, len(days) - w + 1), w)]
    if len(days) >= w and len(days) % w != 0:
        windows.append(summarise(days[-w:], cfg))
    return CoverageReport(
        universe, portfolio_code, tuple(days), tuple(windows), cfg, {"n_days": len(days)}
    )


def summarise(days: list[DayCoverage], cfg: CoverageConfig) -> WindowCoverage:
    """Window statistics for consecutive day results."""
    n = len(days)
    x = sum(d.breach for d in days)
    spans = [d.breach_span for d in days]
    shortfalls = [d.shortfall for d in days]
    worst = max(range(n), key=lambda i: shortfalls[i])
    return WindowCoverage(
        window_start=days[0].as_of,
        window_end=days[-1].as_of,
        n_obs=n,
        breaches=x,
        breaches_raw=sum(d.breach_raw for d in days),
        breaches_core=sum(d.breach_core for d in days),
        breaches_span=None if any(s is None for s in spans) else sum(bool(s) for s in spans),
        coverage=1.0 - x / n,
        target=cfg.target,
        kupiec=kupiec_pof(n, x, 1.0 - cfg.target),
        max_shortfall=shortfalls[worst],
        max_shortfall_over_im=(shortfalls[worst] / days[worst].im if days[worst].im > 0 else None),
    )


UPSERT_DAY = """
INSERT INTO margin_coverage_results (run_id, universe, portfolio_code, as_of_date, pnl_date,
    h_business_days, realised_loss, im, im_core, im_span_legacy, im_h_block, breach, breach_raw,
    breach_core, breach_span, shortfall, attribution)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO UPDATE SET pnl_date = EXCLUDED.pnl_date,
    h_business_days = EXCLUDED.h_business_days, realised_loss = EXCLUDED.realised_loss,
    im = EXCLUDED.im, im_core = EXCLUDED.im_core, im_span_legacy = EXCLUDED.im_span_legacy,
    im_h_block = EXCLUDED.im_h_block, breach = EXCLUDED.breach, breach_raw = EXCLUDED.breach_raw,
    breach_core = EXCLUDED.breach_core, breach_span = EXCLUDED.breach_span,
    shortfall = EXCLUDED.shortfall, attribution = EXCLUDED.attribution, created_at = now()
"""

INSERT_WINDOW = """
INSERT INTO margin_coverage_summaries (universe, portfolio_code, window_start, window_end,
    n_obs, breaches, breaches_raw, breaches_core, breaches_span, coverage, target, kupiec_lr,
    kupiec_p, max_shortfall, max_shortfall_over_im, params, code_version)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def record(
    conn: psycopg.Connection[Any], report: CoverageReport, code_version: str | None = None
) -> int:
    """Upsert day rows (keyed by run_id) and append window rows; returns windows written."""
    cfg = report.config
    params = {
        "target": cfg.target,
        "window_days": cfg.window_days,
        "mpor_days": cfg.mpor_days,
        "margin_params_sha256": cfg.sha256,
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
                    d.h,
                    d.realised_loss,
                    d.im,
                    d.im_core,
                    d.im_span,
                    d.im_h_block,
                    d.breach,
                    d.breach_raw,
                    d.breach_core,
                    d.breach_span,
                    d.shortfall,
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
                    w.breaches,
                    w.breaches_raw,
                    w.breaches_core,
                    w.breaches_span,
                    w.coverage,
                    w.target,
                    w.kupiec.statistic,
                    w.kupiec.p_value,
                    w.max_shortfall,
                    w.max_shortfall_over_im,
                    Jsonb(params),
                    code_version,
                )
                for w in report.windows
            ],
        )
    return len(report.windows)
