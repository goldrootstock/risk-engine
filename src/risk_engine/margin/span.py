"""Legacy SPAN 16-scenario margin for comparison (design note 09 §5).

Per instrument: the worst loss over the 16 SPAN scenarios (price 0, +/-1/3, +/-2/3, +/-1 of
the price scan range x volatility up/down, plus two extreme price moves counted at a
fraction) is the scan risk. Inter-commodity spread credits on opposite-signed pairs are
subtracted. The book is linear, so the volatility scenarios do not move P&L and the table
degenerates to price moves — recorded as a limit, not hidden.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.margin.core import block_sums
from risk_engine.margin.params import MarginParams
from risk_engine.risk.measures import to_loss
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import ReturnMatrix

PRICE_MOVES: tuple[float, ...] = (0.0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1.0, -1.0)


@dataclass(frozen=True, slots=True)
class SpanResult:
    """Scan risk per ticker, spread credits per pair, and the resulting margin."""

    im_span_legacy: float
    scan_risk: dict[str, float]
    spread_credit: dict[str, float]
    price_scan_range: dict[str, float]  # per factor, in the factor's own unit


def price_scan_ranges(
    rm: ReturnMatrix, *, window: int, warmup: int, horizon: int, confidence: float
) -> dict[str, float]:
    """Empirical ``confidence`` quantile of |h-day change| over the last ``window`` raw changes."""
    x = rm.changes.iloc[warmup:].iloc[-window:].to_numpy(dtype="float64")
    blocks = np.abs(block_sums(x, horizon))
    q = np.quantile(blocks, confidence, axis=0)
    return {f: float(v) for f, v in zip(rm.changes.columns, q, strict=True)}


def scenario_table(psr: float, params: MarginParams) -> list[tuple[float, float, float]]:
    """The 16 SPAN scenarios as ``(price move, volatility move, weight)`` for one factor."""
    rows: list[tuple[float, float, float]] = []
    for m in PRICE_MOVES:
        for v in (params.span_vol_scan_pct, -params.span_vol_scan_pct):
            rows.append((m * psr, v, 1.0))
    e = params.span_extreme_multiple * psr
    rows.append((e, 0.0, params.span_extreme_weight))
    rows.append((-e, 0.0, params.span_extreme_weight))
    return rows


def evaluate(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    window: int,
    warmup: int,
    params: MarginParams,
    horizon: int | None = None,
) -> SpanResult:
    """Legacy SPAN margin of ``positions`` on the last date of ``rm``."""
    h = int(horizon or params.mpor_days)
    factors = list(rm.changes.columns)
    psr = price_scan_ranges(
        rm, window=window, warmup=warmup, horizon=h, confidence=params.span_scan_confidence
    )
    levels_today = rm.levels.iloc[-1]
    pos = dict(positions)
    tickers = list(pos)
    scan: dict[str, float] = {}
    for j, ticker in enumerate(tickers):
        f = rm.factor_of[ticker]
        rows = scenario_table(psr[f], params)
        x = np.zeros((len(rows), len(factors)))
        x[:, factors.index(f)] = [r[0] for r in rows]
        # volatility moves are part of the table but cannot change a linear instrument's P&L
        losses = to_loss(pnl_matrix(x, factors, levels_today, pos, specs, rm.factor_of))[:, j]
        weights = np.array([r[2] for r in rows])
        scan[ticker] = float(max(0.0, np.max(losses * weights)))
    credits: dict[str, float] = {}
    for (a, b), rate in params.span_spread_credits.items():
        if a in pos and b in pos and np.sign(pos[a]) != np.sign(pos[b]):
            credits[f"{a}/{b}"] = rate * min(scan[a], scan[b])
    total = max(0.0, float(sum(scan.values())) - float(sum(credits.values())))
    return SpanResult(
        im_span_legacy=total, scan_risk=scan, spread_credit=credits, price_scan_range=psr
    )
