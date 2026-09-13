"""Changes -> P&L in USD (design note 04 §4). Signed P&L; the loss flip lives in ES/VaR."""

from __future__ import annotations

import re
from collections.abc import Mapping

import numpy as np
import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.returns import Kind, kind_of

_MATURITY = re.compile(r"^UST_(\d+)([MY])$")


def maturity_years(ticker: str) -> float:
    """``UST_1M`` -> 1/12, ``UST_10Y`` -> 10.0. A naming rule, not a parameter."""
    m = _MATURITY.match(ticker)
    if not m:
        raise ValueError(f"not a Treasury maturity ticker: {ticker}")
    n, unit = int(m.group(1)), m.group(2)
    return n / 12.0 if unit == "M" else float(n)


def modified_duration(y: float, maturity: float) -> float:
    """Modified duration of a semiannual-coupon par bond: ``[1 - (1 + y/2)^(-2T)] / y``.

    ``y`` is a decimal yield (0.0093 for 0.93 %). The ``y -> 0`` limit is ``T``; the formula
    also holds for negative ``y`` (1-month bills in 2015 and 2020).
    """
    if abs(y) < 1e-8:
        return maturity
    return float((1.0 - (1.0 + y / 2.0) ** (-2.0 * maturity)) / y)


def dv01(notional: float, y_percent: float, maturity: float) -> float:
    """USD change in value for a 1 bp fall in yield: ``notional * D_mod * 1e-4``."""
    return notional * modified_duration(y_percent / 100.0, maturity) * 1e-4


def pnl_matrix(
    changes: np.ndarray,
    factors: list[str],
    levels_today: pd.Series,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    factor_of: Mapping[str, str],
) -> np.ndarray:
    """P&L per scenario and instrument, USD, signed (profit positive).

    Args:
        changes: Scenario matrix X, shape (n, k), columns ordered as ``factors``.
        factors: Factor names for the columns of ``changes``.
        levels_today: Today's level per factor (USD per unit for FX, percent for yields).
        positions: ``{ticker: quantity}`` — foreign-currency amount (FX), physical quantity
            (energy) or par notional in USD (Treasuries). Multiplier from the spec.
        specs: ``{ticker: InstrumentSpec}``.
        factor_of: ``{ticker: factor}`` from :class:`~risk_engine.risk.returns.ReturnMatrix`.

    Returns:
        Array of shape (n, len(positions)) in the order of ``positions``.
    """
    col = {f: i for i, f in enumerate(factors)}
    out = np.zeros((changes.shape[0], len(positions)), dtype="float64")
    for j, (ticker, qty) in enumerate(positions.items()):
        spec = specs[ticker]
        f = factor_of[ticker]
        x = changes[:, col[f]]
        k: Kind = kind_of(spec)
        level = float(levels_today[f])
        if k == "log":
            out[:, j] = qty * spec.multiplier * level * np.expm1(x)
        elif k == "abs":
            out[:, j] = qty * spec.multiplier * x
        else:  # bp: yield up -> long bond loses
            out[:, j] = -dv01(qty * spec.multiplier, level, maturity_years(ticker)) * x
    return out
