"""Core initial margin: FHS tail measure at the MPOR, floor, stress blend, add-ons (note 09 §2-§4).

Pure functions of a :class:`~risk_engine.risk.returns.ReturnMatrix`, a book and the frozen
:class:`~risk_engine.margin.params.MarginParams`. Reuses the A1 primitives
(:func:`~risk_engine.risk.fhs.standardised_residuals`, :func:`~risk_engine.risk.fhs.sigma_next`,
:func:`~risk_engine.risk.pnl.pnl_matrix`, :mod:`~risk_engine.risk.measures`) and changes none
of them; the 1-day results of the risk engine are untouched.

Decomposition (fixed by the 0002 catalogue, note 01 §10-1)::

    im = im_core + im_floor + im_stress_blend + im_liquidity_addon + im_concentration_addon

with every increment >= 0. ``to_loss`` remains the only sign flip.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.margin.params import MarginParams
from risk_engine.risk.fhs import portfolio_value, sigma_next, standardised_residuals
from risk_engine.risk.measures import (
    component_es,
    expected_shortfall,
    to_loss,
    value_at_risk,
)
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import ReturnMatrix, kind_of

#: Change kind -> asset class used for add-on rates (note 09 §4). v1 has exactly one kind per
#: asset class, so the mapping is a rule, not a parameter.
ASSET_CLASS_OF_KIND: Mapping[str, str] = {"bp": "rates", "log": "fx", "abs": "commodity"}


@dataclass(frozen=True, slots=True)
class MarginResult:
    """Everything a margin run records (note 09 §2-§4). Losses and margins are positive USD."""

    im: float
    im_core: float
    im_floor: float
    im_stress_blend: float
    im_liquidity_addon: float
    im_concentration_addon: float
    var: float  # VaR at `confidence` over the horizon
    es: float  # ES at `confidence` over the horizon
    es_975: float  # ES 97.5 % over the horizon, for comparison with the risk engine
    stressed_es: float  # core measure over the worst stressed window, floored sigma
    stressed_window: tuple[date, date]
    component: dict[str, float]  # Euler split of im_core per ticker; sums to im_core
    liquidity_by_ticker: dict[str, float]
    concentration_by_ticker: dict[str, float]
    gross_notional: dict[str, float]
    n_scenarios: int
    horizon: int
    portfolio_value: float
    sigma_today: dict[str, float]
    sigma_floor: dict[str, float]
    sigma_used: dict[str, float]
    pool_start: date
    pool_end: date


def block_sums(arr: np.ndarray, h: int) -> np.ndarray:
    """Sums of ``h`` consecutive rows: shape ``(n - h + 1, k)``. ``h = 1`` returns ``arr``."""
    if h <= 1:
        return arr
    if len(arr) < h:
        raise ValueError(f"{len(arr)} rows cannot form blocks of {h}")
    cs = np.vstack([np.zeros((1, arr.shape[1])), np.cumsum(arr, axis=0)])
    out: np.ndarray = cs[h:] - cs[:-h]
    return out


def tail_measure(loss: np.ndarray, params: MarginParams) -> float:
    """The core measure (ES or VaR) at ``params.confidence``."""
    if params.measure == "es":
        return expected_shortfall(loss, params.confidence)
    return value_at_risk(loss, params.confidence)


def gross_notional(
    levels_today: pd.Series,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    factor_of: Mapping[str, str],
) -> dict[str, float]:
    """USD gross notional per ticker: |q m S| (FX), |q m P| (energy), |q m| (Treasuries at par)."""
    out: dict[str, float] = {}
    for ticker, qty in positions.items():
        spec = specs[ticker]
        if spec.quote_type == "yield":
            out[ticker] = abs(qty * spec.multiplier)
        else:
            out[ticker] = abs(qty * spec.multiplier * float(levels_today[factor_of[ticker]]))
    return out


def add_ons(
    notional: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    params: MarginParams,
) -> tuple[dict[str, float], dict[str, float]]:
    """Liquidity (rate x notional) and concentration (rate x excess over threshold) per ticker."""
    liquidity: dict[str, float] = {}
    concentration: dict[str, float] = {}
    for ticker, gross in notional.items():
        ac = ASSET_CLASS_OF_KIND[kind_of(specs[ticker])]
        liquidity[ticker] = gross * params.liquidity_bp[ac] / 1e4
        excess = max(0.0, gross - params.concentration_threshold_usd[ac])
        concentration[ticker] = excess * params.concentration_rate
    return liquidity, concentration


def evaluate(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    lam: float,
    window: int,
    warmup: int,
    params: MarginParams,
    vol: str = "ewma",
    horizon: int | None = None,
) -> MarginResult:
    """Initial margin of ``positions`` on the last date of ``rm``.

    Steps (note 09 §2-§4): standardise by forecast volatility; take the last ``window``
    residual vectors, block-summed over ``horizon`` days (date-wise joint sampling kept);
    rescale by today's sigma -> ``im_core``; rescale by max(today's sigma, unfiltered
    ``floor_lookback_days`` sigma) -> floor increment; blend in the worst
    ``stress_window_days`` window of the whole pool at the floored sigma -> stress increment;
    add liquidity and concentration charges on gross notional.

    ``horizon`` defaults to ``params.mpor_days``; the coverage backtest passes the actual
    business-day gap of a transition.
    """
    h = int(horizon or params.mpor_days)
    factors = list(rm.changes.columns)
    z, sigma = standardised_residuals(rm.changes, lam, warmup, vol)
    s_today = sigma_next(rm.changes, sigma, lam, vol).reindex(factors)
    pool = z.iloc[warmup:]
    if len(pool) < window:
        raise ValueError(f"residual pool has {len(pool)} rows, window needs {window}")
    if len(pool) - h + 1 < params.stress_window_days:
        raise ValueError("residual pool shorter than the stress window at this horizon")
    levels_today = rm.levels.iloc[-1]
    pos = dict(positions)

    # unfiltered long-run volatility floor (EMIR RTS Art. 28(1)(c))
    x_floor = rm.changes.iloc[warmup:].iloc[-params.floor_lookback_days :]
    s_floor = np.sqrt((x_floor.to_numpy(dtype="float64") ** 2).mean(axis=0))
    s_used = np.maximum(s_today.to_numpy(dtype="float64"), s_floor)

    def losses(zblock: np.ndarray, s: np.ndarray) -> np.ndarray:
        scen = zblock * s[None, :]
        return to_loss(pnl_matrix(scen, factors, levels_today, pos, specs, rm.factor_of))

    recent = pool.iloc[-window:]
    recent_h = block_sums(recent.to_numpy(dtype="float64"), h)
    l0 = losses(recent_h, s_today.to_numpy(dtype="float64"))
    port0 = l0.sum(axis=1)
    im_core = tail_measure(port0, params)
    comp = component_es(l0, params.confidence) if params.measure == "es" else None
    l1 = losses(recent_h, s_used).sum(axis=1)
    level1 = tail_measure(l1, params)
    im_floor = max(0.0, level1 - im_core)

    # stressed window: worst contiguous block of the whole pool at the floored sigma
    all_h = block_sums(pool.to_numpy(dtype="float64"), h)
    all_losses = losses(all_h, s_used).sum(axis=1)
    sw = params.stress_window_days
    best, best_start = -np.inf, 0
    for start in range(0, len(all_losses) - sw + 1):
        m = tail_measure(all_losses[start : start + sw], params)
        if m > best:
            best, best_start = m, start
    # block i ends on pool row i + h - 1
    stressed_window = (
        pool.index[best_start + h - 1].date(),
        pool.index[best_start + sw - 1 + h - 1].date(),
    )
    im_stress_blend = params.stress_weight * max(0.0, float(best) - level1)

    notional = gross_notional(levels_today, pos, specs, rm.factor_of)
    liquidity, concentration = add_ons(notional, specs, params)
    im_liq, im_conc = float(sum(liquidity.values())), float(sum(concentration.values()))

    if comp is None:  # VaR core: attribute the core pro rata to stand-alone VaRs
        stand_alone = np.array(
            [value_at_risk(l0[:, j], params.confidence) for j in range(len(pos))]
        )
        total = float(np.abs(stand_alone).sum())
        comp = stand_alone / total * im_core if total > 0 else np.zeros(len(pos))

    return MarginResult(
        im=level1 + im_stress_blend + im_liq + im_conc,
        im_core=im_core,
        im_floor=im_floor,
        im_stress_blend=im_stress_blend,
        im_liquidity_addon=im_liq,
        im_concentration_addon=im_conc,
        var=value_at_risk(port0, params.confidence),
        es=expected_shortfall(port0, params.confidence),
        es_975=expected_shortfall(port0, 0.975),
        stressed_es=float(best),
        stressed_window=stressed_window,
        component={t: float(c) for t, c in zip(pos, comp, strict=True)},
        liquidity_by_ticker=liquidity,
        concentration_by_ticker=concentration,
        gross_notional=notional,
        n_scenarios=len(recent_h),
        horizon=h,
        portfolio_value=portfolio_value(levels_today, pos, specs, rm.factor_of),
        sigma_today={f: float(s_today[f]) for f in factors},
        sigma_floor={f: float(v) for f, v in zip(factors, s_floor, strict=True)},
        sigma_used={f: float(v) for f, v in zip(factors, s_used, strict=True)},
        pool_start=recent.index[0].date(),
        pool_end=recent.index[-1].date(),
    )
