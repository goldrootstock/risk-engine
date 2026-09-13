"""Variance-covariance (RiskMetrics) and Monte Carlo ES/VaR (design note 05 §4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.measures import component_es, tail_measures, to_loss
from risk_engine.risk.pnl import dv01, maturity_years, pnl_matrix
from risk_engine.risk.returns import ReturnMatrix, kind_of
from risk_engine.risk.volatility import ewma_covariance


def deltas(
    levels_today: pd.Series,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    factor_of: Mapping[str, str],
    factors: list[str],
) -> np.ndarray:
    """First-order P&L sensitivity to each factor's change, USD per unit change."""
    d = np.zeros(len(factors))
    col = {f: i for i, f in enumerate(factors)}
    for ticker, qty in positions.items():
        spec = specs[ticker]
        f = factor_of[ticker]
        k = kind_of(spec)
        if k == "log":
            d[col[f]] += qty * spec.multiplier * float(levels_today[f])
        elif k == "abs":
            d[col[f]] += qty * spec.multiplier
        else:
            d[col[f]] += -dv01(
                qty * spec.multiplier, float(levels_today[f]), maturity_years(ticker)
            )
    return d


@dataclass(frozen=True, slots=True)
class ParametricResult:
    """Closed-form normal VaR/ES on the linearised P&L."""

    var: float
    es: float
    sigma_pnl: float
    n_obs: int


def parametric(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    lam: float,
    window: int,
    var_alpha: float,
    es_alpha: float,
) -> ParametricResult:
    """Delta-normal VaR/ES with an EWMA covariance over the last ``window`` changes."""
    factors = list(rm.changes.columns)
    x = rm.changes.iloc[-window:].to_numpy(dtype="float64")
    cov = ewma_covariance(x, lam)
    d = deltas(rm.levels.iloc[-1], positions, specs, rm.factor_of, factors)
    sigma_p = float(np.sqrt(d @ cov @ d))
    z_var = float(norm.ppf(var_alpha))
    z_es = float(norm.ppf(es_alpha))
    return ParametricResult(
        var=z_var * sigma_p,
        es=float(norm.pdf(z_es) / (1.0 - es_alpha)) * sigma_p,
        sigma_pnl=sigma_p,
        n_obs=len(x),
    )


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    """Simulated normal changes through the exact P&L mapping."""

    var: float
    es: float
    component_es: dict[str, float]
    n_scenarios: int


def monte_carlo(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    lam: float,
    window: int,
    paths: int,
    seed: int,
    var_alpha: float,
    es_alpha: float,
) -> MonteCarloResult:
    """Multivariate-normal scenarios from the EWMA covariance, exact P&L, tail measures."""
    factors = list(rm.changes.columns)
    x = rm.changes.iloc[-window:].to_numpy(dtype="float64")
    cov = ewma_covariance(x, lam)
    cov = cov + np.eye(len(factors)) * 1e-12 * np.trace(cov) / len(factors)  # PSD guard
    rng = np.random.default_rng(seed)
    scen = rng.multivariate_normal(np.zeros(len(factors)), cov, size=paths, method="cholesky")
    pos = dict(positions)
    lbi = to_loss(pnl_matrix(scen, factors, rm.levels.iloc[-1], pos, specs, rm.factor_of))
    tm = tail_measures(lbi.sum(axis=1), var_alpha, es_alpha)
    comp = component_es(lbi, es_alpha)
    return MonteCarloResult(
        var=tm.var,
        es=tm.es,
        component_es={t: float(c) for t, c in zip(pos, comp, strict=True)},
        n_scenarios=paths,
    )
