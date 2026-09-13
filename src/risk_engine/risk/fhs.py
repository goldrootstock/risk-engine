"""Filtered historical simulation (design note 05 §2).

Per-asset volatility scaling, date-wise joint sampling of standardised residuals, exact P&L
mapping through :func:`risk_engine.risk.pnl.pnl_matrix`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.measures import (
    component_es,
    expected_shortfall,
    tail_measures,
    to_loss,
)
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import ReturnMatrix
from risk_engine.risk.volatility import ewma_variance, garch11_fit, garch11_variance


@dataclass(frozen=True, slots=True)
class FhsResult:
    """Everything a run records from one FHS evaluation."""

    var: float
    es: float
    stressed_es: float
    stressed_window: tuple[date, date]
    component_es: dict[str, float]  # per instrument ticker
    n_scenarios: int
    portfolio_value: float
    sigma_today: dict[str, float]
    pool_start: date
    pool_end: date
    residual_mean: dict[str, float]


def standardised_residuals(
    changes: pd.DataFrame, lam: float, warmup: int, vol: str = "ewma"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(z, sigma)`` frames aligned with ``changes``; warm-up rows are NaN in ``z``.

    ``sigma[t]`` is the forecast made with data to ``t-1``; the last row of ``sigma`` is
    therefore *not* today's forecast — see :func:`sigma_next`.
    """
    x = changes.to_numpy(dtype="float64")
    if vol == "ewma":
        s2 = ewma_variance(x, lam, warmup)
    elif vol == "garch":
        s2 = np.column_stack(
            [
                garch11_variance(x[:, j], garch11_fit(x[:, j], warmup), warmup)
                for j in range(x.shape[1])
            ]
        )
    else:
        raise ValueError(f"unknown vol filter {vol!r}")
    sigma = pd.DataFrame(np.sqrt(s2), index=changes.index, columns=changes.columns)
    z = changes / sigma
    z.iloc[:warmup] = np.nan
    return z, sigma


def sigma_next(
    changes: pd.DataFrame, sigma: pd.DataFrame, lam: float, vol: str = "ewma"
) -> pd.Series:
    """Volatility forecast for the day after the last row (today's sigma for the scenarios)."""
    if vol == "ewma":
        last_x, last_s2 = changes.iloc[-1], sigma.iloc[-1] ** 2
        return pd.Series(np.sqrt(lam * last_s2 + (1.0 - lam) * last_x**2), index=changes.columns)
    # garch: refit is expensive; reuse the one-step recursion with fitted params per column
    out = {}
    for col in changes.columns:
        x = changes[col].to_numpy(dtype="float64")
        p = garch11_fit(x, min(75, len(x) - 2))
        out[col] = float(
            np.sqrt(p.omega + p.alpha * x[-1] ** 2 + p.beta * sigma[col].iloc[-1] ** 2)
        )
    return pd.Series(out)


def portfolio_value(
    levels_today: pd.Series,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    factor_of: Mapping[str, str],
) -> float:
    """Market value in USD: FX q*S, energy q*P, Treasuries at par (q is the notional)."""
    total = 0.0
    for ticker, qty in positions.items():
        spec = specs[ticker]
        if spec.quote_type == "yield":
            total += qty * spec.multiplier
        else:
            total += qty * spec.multiplier * float(levels_today[factor_of[ticker]])
    return total


def evaluate(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    lam: float,
    window: int,
    warmup: int,
    var_alpha: float,
    es_alpha: float,
    stressed_window: int,
    vol: str = "ewma",
) -> FhsResult:
    """FHS ES/VaR for the last date of ``rm`` with today's positions.

    Steps: standardise every factor by its own forecast volatility; take the last ``window``
    residual vectors (date-wise, jointly); rescale by today's volatility; map to P&L with
    today's levels and positions; take losses; read VaR/ES/component ES. The stressed ES scans
    every ``stressed_window``-day block of residuals after the warm-up.
    """
    factors = list(rm.changes.columns)
    z, sigma = standardised_residuals(rm.changes, lam, warmup, vol)
    s_today = sigma_next(rm.changes, sigma, lam, vol)
    pool = z.iloc[warmup:]
    if len(pool) < window:
        raise ValueError(f"residual pool has {len(pool)} rows, window needs {window}")
    levels_today = rm.levels.iloc[-1]
    pos = dict(positions)

    def losses_for(zblock: pd.DataFrame) -> np.ndarray:
        scen = zblock.to_numpy(dtype="float64") * s_today.to_numpy(dtype="float64")[None, :]
        pnl = pnl_matrix(scen, factors, levels_today, pos, specs, rm.factor_of)
        return to_loss(pnl)  # (n, k instruments)

    recent = pool.iloc[-window:]
    lbi = losses_for(recent)
    port = lbi.sum(axis=1)
    tm = tail_measures(port, var_alpha, es_alpha)
    comp = component_es(lbi, es_alpha)

    # stressed window: worst contiguous block of the whole post-warm-up pool for this book
    all_losses = losses_for(pool).sum(axis=1)
    best_es, best_start = -np.inf, 0
    for start in range(0, len(pool) - stressed_window + 1):
        e = expected_shortfall(all_losses[start : start + stressed_window], es_alpha)
        if e > best_es:
            best_es, best_start = e, start
    sw = (pool.index[best_start].date(), pool.index[best_start + stressed_window - 1].date())

    return FhsResult(
        var=tm.var,
        es=tm.es,
        stressed_es=float(best_es),
        stressed_window=sw,
        component_es={t: float(c) for t, c in zip(pos, comp, strict=True)},
        n_scenarios=tm.n_scenarios,
        portfolio_value=portfolio_value(levels_today, pos, specs, rm.factor_of),
        sigma_today={f: float(s_today[f]) for f in factors},
        pool_start=recent.index[0].date(),
        pool_end=recent.index[-1].date(),
        residual_mean={f: float(recent[f].mean()) for f in factors},
    )
