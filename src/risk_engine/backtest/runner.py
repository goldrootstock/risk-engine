"""Backtest orchestration (design note 06).

``backtest()`` takes no connection: it receives the run series and the P&L series and returns
a report. Loading is read-only; writing is in :mod:`risk_engine.backtest.record`.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg

from risk_engine.backtest.hpl import DailyPnl, daily_pnl, top_attribution
from risk_engine.backtest.statistics import (
    LrTest,
    PlaResult,
    christoffersen_independence,
    conditional_coverage,
    kupiec_pof,
    pla,
    traffic_light,
)
from risk_engine.risk import fhs as fhs_mod
from risk_engine.risk.engine import RiskParams, prepare
from risk_engine.risk.positions import load_snapshot
from risk_engine.risk.returns import ReturnMatrix

DEFAULT_PARAMS_PATH = Path("config/backtest_params.toml")

RUNS_SQL = """
SELECT r.run_id, r.as_of_date, r.portfolio_code,
       max(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = %(alpha)s) AS var_a,
       max(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975)    AS es_975
FROM risk_runs r JOIN risk_measures m USING (run_id)
WHERE r.universe = %(universe)s AND r.portfolio_code = %(portfolio)s AND r.tag = %(tag)s
  AND r.method = %(method)s AND m.scope_type = 'portfolio'
GROUP BY r.run_id ORDER BY r.as_of_date
"""


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Frozen backtest parameters (note 00 §3: no re-tuning inside the test)."""

    window_days: int
    confidence: float
    significance: float
    yellow_from: int
    red_from: int
    spearman_green: float
    spearman_amber: float
    ks_green: float
    ks_amber: float
    sha256: str
    horizon_method: str = "block"

    @classmethod
    def load(cls, path: Path = DEFAULT_PARAMS_PATH) -> BacktestConfig:
        """Read ``config/backtest_params.toml``."""
        raw = path.read_bytes()
        c = tomllib.loads(raw.decode("utf-8"))
        return cls(
            window_days=int(c["window"]["days"]),
            confidence=float(c["exceptions"]["confidence"]),
            significance=float(c["exceptions"]["significance"]),
            yellow_from=int(c["traffic_light"]["yellow_from"]),
            red_from=int(c["traffic_light"]["red_from"]),
            spearman_green=float(c["pla"]["spearman_green"]),
            spearman_amber=float(c["pla"]["spearman_amber"]),
            ks_green=float(c["pla"]["ks_green"]),
            ks_amber=float(c["pla"]["ks_amber"]),
            sha256=hashlib.sha256(raw).hexdigest(),
            horizon_method=str(c.get("horizon", {}).get("method", "block")),
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One daily run as read from the database."""

    run_id: int
    as_of: date
    var: float
    es: float


@dataclass(frozen=True, slots=True)
class DayResult:
    """One ``backtest_results`` row. ``exception`` is the official (horizon-consistent) one."""

    run_id: int
    as_of: date
    pnl_date: date
    hpl: float
    rtpl: float
    var: float
    es: float
    exception: bool
    attribution: dict[str, float]
    h: int = 1
    var_block: float | None = None
    var_sqrt: float | None = None
    exception_raw: bool = False
    exception_sqrt: bool = False


@dataclass(frozen=True, slots=True)
class WindowSummary:
    """One ``backtest_summaries`` row."""

    window_start: date
    window_end: date
    n_obs: int
    exceptions: int
    expected: float
    exceptions_raw: int
    exceptions_sqrt: int
    kupiec: LrTest
    independence: LrTest
    cc: LrTest
    traffic_light: str
    pla: PlaResult


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Everything the writer needs."""

    universe: str
    portfolio_code: str
    days: tuple[DayResult, ...]
    windows: tuple[WindowSummary, ...]
    config: BacktestConfig
    meta: dict[str, Any] = field(default_factory=dict)


def load_inputs(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio_code: str,
    *,
    tag: str = "daily_batch",
    method: str = "fhs",
    cfg: BacktestConfig,
    risk_params: RiskParams | None = None,
) -> tuple[list[RunRecord], list[DailyPnl], dict[date, float]]:
    """Read the run series and compute HPL/RTPL per run date. Read-only.

    For multi-business-day transitions the h-day block-bootstrap VaR is computed as well and
    returned keyed by run date.
    """
    rows = conn.execute(
        RUNS_SQL,
        {
            "alpha": cfg.confidence,
            "universe": universe,
            "portfolio": portfolio_code,
            "tag": tag,
            "method": method,
        },
    ).fetchall()
    runs = [RunRecord(int(r[0]), r[1], float(r[3]), float(r[4])) for r in rows if r[3] is not None]
    if not runs:
        return [], [], {}
    last = runs[-1].as_of
    rm, specs, _ = prepare(conn, universe, date.max)  # full series, so the day after `last` exists
    positions, _ = load_snapshot(conn, portfolio_code, last)
    idx = rm.changes.index
    pnls: list[DailyPnl] = []
    skipped: list[date] = []
    horizon_var: dict[date, float] = {}
    rp = risk_params or RiskParams.load()
    for run in runs:
        t = pd.Timestamp(run.as_of)
        pos = int(idx.get_indexer(pd.DatetimeIndex([t]))[0])
        if pos < 0 or pos + 1 >= len(idx):
            skipped.append(run.as_of)
            continue
        p = daily_pnl(rm, t, positions, specs)
        pnls.append(p)
        h = int(np.busday_count(p.as_of, p.pnl_date))
        if h >= 2:
            horizon_var[p.as_of] = _block_var(rm, pos, positions, specs, rp, h)
    # The last run can never be backtested (no t+1 yet); anything else missing is a data gap
    # that must be visible, never silently dropped.
    if len(skipped) > 1 or (skipped and skipped[0] != last):
        raise ValueError(
            f"{len(skipped)} run dates have no aligned next observation: {skipped[:5]}"
        )
    return runs, pnls, horizon_var


def _block_var(
    rm: ReturnMatrix,
    pos: int,
    positions: Mapping[str, float],
    specs: dict[str, Any],
    rp: RiskParams,
    h: int,
) -> float:
    """h-day FHS VaR by block bootstrap on the data available at position ``pos``."""
    sliced = ReturnMatrix(
        changes=rm.changes.iloc[: pos + 1],
        levels=rm.levels.iloc[: pos + 1],
        kind=rm.kind,
        factor_of=rm.factor_of,
        meta={},
    )
    r = fhs_mod.evaluate(
        sliced,
        positions,
        specs,
        lam=rp.lam,
        window=rp.window_days,
        warmup=rp.warmup_days,
        var_alpha=rp.var_confidence,
        es_alpha=rp.es_confidence,
        stressed_window=rp.stressed_window_days,
        horizon=h,
    )
    return r.var


def backtest(
    runs: list[RunRecord],
    pnls: list[DailyPnl],
    cfg: BacktestConfig,
    *,
    universe: str,
    portfolio_code: str,
    horizon_var: Mapping[date, float] | None = None,
) -> BacktestReport:
    """Exceptions per day plus window statistics. Pure; ``cfg`` is never modified.

    For a transition spanning ``h`` business days the official exception compares the loss
    with the ``h``-day VaR chosen by ``cfg.horizon_method`` (block bootstrap by default);
    the raw 1-day and sqrt(h) comparisons are always recorded alongside.
    """
    by_date = {p.as_of: p for p in pnls}
    hv = dict(horizon_var or {})
    days: list[DayResult] = []
    for run in runs:
        p = by_date.get(run.as_of)
        if p is None:
            continue
        loss = -p.hpl
        h = int(np.busday_count(p.as_of, p.pnl_date))
        var_sqrt = run.var * float(np.sqrt(h)) if h >= 2 else None
        var_block = hv.get(run.as_of) if h >= 2 else None
        if h >= 2 and var_block is None and cfg.horizon_method == "block":
            raise ValueError(f"no block VaR for {run.as_of} (h={h})")
        official = {"raw": run.var, "sqrt": var_sqrt, "block": var_block}[cfg.horizon_method]
        if official is None:
            official = run.var
        exc = loss > official
        days.append(
            DayResult(
                run_id=run.run_id,
                as_of=run.as_of,
                pnl_date=p.pnl_date,
                hpl=p.hpl,
                rtpl=p.rtpl,
                var=run.var,
                es=run.es,
                exception=exc,
                attribution=(
                    top_attribution(p.loss_by_instrument) if (exc or loss > run.var) else {}
                ),
                h=h,
                var_block=var_block,
                var_sqrt=var_sqrt,
                exception_raw=loss > run.var,
                exception_sqrt=(loss > var_sqrt) if var_sqrt is not None else loss > run.var,
            )
        )
    windows = [
        summarise(days[i : i + cfg.window_days], cfg)
        for i in range(0, max(0, len(days) - cfg.window_days + 1), cfg.window_days)
    ]
    if len(days) >= cfg.window_days and (len(days) % cfg.window_days) != 0:
        windows.append(summarise(days[-cfg.window_days :], cfg))  # trailing window ending today
    return BacktestReport(
        universe, portfolio_code, tuple(days), tuple(windows), cfg, {"n_days": len(days)}
    )


def summarise(days: list[DayResult], cfg: BacktestConfig) -> WindowSummary:
    """Window statistics for a list of consecutive day results."""
    e = np.array([d.exception for d in days], dtype=int)
    n, x = len(days), int(e.sum())
    uc = kupiec_pof(n, x, 1.0 - cfg.confidence)
    ind = christoffersen_independence(e)
    hpl = np.array([d.hpl for d in days])
    rtpl = np.array([d.rtpl for d in days])
    return WindowSummary(
        window_start=days[0].as_of,
        window_end=days[-1].as_of,
        n_obs=n,
        exceptions=x,
        expected=n * (1.0 - cfg.confidence),
        exceptions_raw=int(sum(d.exception_raw for d in days)),
        exceptions_sqrt=int(sum(d.exception_sqrt for d in days)),
        kupiec=uc,
        independence=ind,
        cc=conditional_coverage(uc, ind),
        traffic_light=traffic_light(x, cfg.yellow_from, cfg.red_from),
        pla=pla(
            hpl,
            rtpl,
            spearman_green=cfg.spearman_green,
            spearman_amber=cfg.spearman_amber,
            ks_green=cfg.ks_green,
            ks_amber=cfg.ks_amber,
        ),
    )
