"""Return construction: aligned levels -> change matrix X (design note 04 §1-§2).

Pure function of a wide level frame and the instrument specs. Three kinds of change:
``log`` (FX, log return of the USD-per-unit rate), ``abs`` (energy spot, price difference
in USD per unit) and ``bp`` (Treasury yields, difference in basis points). FX factors are
rebuilt from the ECB EUR-based pairs as ``S_CCY = EURUSD / EURCCY`` **after** alignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec

Kind = Literal["log", "abs", "bp"]
Align = Literal["intersection", "ffill"]

#: ECB tickers are ``EUR<CCY>``; the factor is the currency.
_EUR_PREFIX = "EUR"


@dataclass(frozen=True, slots=True)
class ReturnMatrix:
    """Aligned levels and changes with their kinds.

    Attributes:
        changes: Change matrix X (date x factor), first aligned date dropped.
        levels: Aligned levels on the same dates as ``changes`` (FX already as USD per unit).
        kind: Change kind per factor.
        factor_of: Ticker -> factor column name (FX tickers map to the currency code).
        meta: Alignment bookkeeping: ``align``, ``max_gap``, ``dropped_dates``,
            ``filled_cells``, ``n_obs``, ``first``, ``last``.
    """

    changes: pd.DataFrame
    levels: pd.DataFrame
    kind: dict[str, Kind]
    factor_of: dict[str, str]
    meta: dict[str, Any] = field(default_factory=dict)


def kind_of(spec: InstrumentSpec) -> Kind:
    """Map an instrument to its change kind (note 04 §1)."""
    if spec.quote_type == "yield":
        return "bp"
    return "log" if spec.return_type == "log" else "abs"


def factor_name(spec: InstrumentSpec) -> str:
    """FX tickers ``EURxxx`` become the currency code; everything else keeps its ticker."""
    if spec.source == "ecb" and spec.ticker.startswith(_EUR_PREFIX) and len(spec.ticker) == 6:
        ccy = spec.ticker[3:]
        return "EUR" if ccy == "USD" else ccy
    return spec.ticker


def align_levels(
    levels: pd.DataFrame, *, align: Align, max_gap: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Calendar alignment (note 04 §2). Returns the aligned frame and bookkeeping."""
    total = len(levels)
    if align == "intersection":
        out = levels.dropna(how="any")
        return out, {"dropped_dates": total - len(out), "filled_cells": 0}
    if align == "ffill":
        filled = levels.ffill(limit=max_gap)
        filled_cells = int((levels.isna() & filled.notna()).sum().sum())
        out = filled.dropna(how="any")
        return out, {"dropped_dates": total - len(out), "filled_cells": filled_cells}
    raise ValueError(f"unknown align: {align!r}")


def to_usd_per_unit(levels: pd.DataFrame, specs: dict[str, InstrumentSpec]) -> pd.DataFrame:
    """Rewrite ECB ``EURxxx`` columns as ``S_CCY = EURUSD / EURCCY`` and rename to factors.

    Requires ``EURUSD`` whenever any other ECB pair is present.
    """
    ecb = [t for t, s in specs.items() if s.source == "ecb" and t in levels.columns]
    out = levels.copy()
    if ecb:
        if "EURUSD" not in levels.columns:
            raise ValueError("EURUSD is required to express ECB pairs in USD per unit")
        eurusd = levels["EURUSD"]
        for t in ecb:
            out[t] = eurusd if t == "EURUSD" else eurusd / levels[t]
    return out.rename(columns={t: factor_name(s) for t, s in specs.items()})


def build(
    levels: pd.DataFrame,
    specs: dict[str, InstrumentSpec],
    *,
    align: Align = "intersection",
    max_gap: int = 1,
) -> ReturnMatrix:
    """Aligned levels -> changes. Pure; never touches the database.

    Steps: (1) align the calendar on raw levels, (2) express FX as USD per unit, (3)
    difference each factor by its kind. The first aligned date has no change and is dropped
    from both ``changes`` and ``levels`` so the two frames share an index.
    """
    missing = [t for t in levels.columns if t not in specs]
    if missing:
        raise KeyError(f"no InstrumentSpec for {missing}")
    aligned, book = align_levels(levels, align=align, max_gap=max_gap)
    usd = to_usd_per_unit(aligned, specs)
    factor_of = {t: factor_name(s) for t, s in specs.items() if t in levels.columns}
    kinds: dict[str, Kind] = {factor_of[t]: kind_of(specs[t]) for t in factor_of}

    changes = pd.DataFrame(index=usd.index, columns=list(usd.columns), dtype="float64")
    for col in usd.columns:
        series = usd[col].to_numpy(dtype="float64")
        k = kinds[col]
        if k == "log":
            if np.any(series <= 0):
                raise ValueError(f"log kind requires positive levels: {col}")
            x = np.diff(np.log(series))
        elif k == "bp":
            x = np.diff(series) * 100.0
        else:
            x = np.diff(series)
        changes[col] = np.concatenate([[np.nan], x])
    changes = changes.iloc[1:]
    usd = usd.iloc[1:]
    meta = {
        "align": align,
        "max_gap": max_gap,
        **book,
        "n_obs": len(changes),
        "first": changes.index[0].date() if len(changes) else None,
        "last": changes.index[-1].date() if len(changes) else None,
    }
    return ReturnMatrix(changes=changes, levels=usd, kind=kinds, factor_of=factor_of, meta=meta)
