"""Hypothetical and risk-theoretical P&L from actual factor changes (design note 06 §1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.measures import to_loss
from risk_engine.risk.parametric import deltas
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import ReturnMatrix


@dataclass(frozen=True, slots=True)
class DailyPnl:
    """P&L of holding the day-t book over the change to the next aligned observation."""

    as_of: date
    pnl_date: date
    hpl: float
    rtpl: float
    loss_by_instrument: dict[str, float]


def daily_pnl(
    rm: ReturnMatrix,
    t: pd.Timestamp,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> DailyPnl:
    """HPL (exact mapping) and RTPL (delta-linear) for the observation after ``t``."""
    idx = rm.changes.index
    pos_t = int(idx.get_indexer(pd.DatetimeIndex([t]))[0])
    if pos_t < 0:
        raise KeyError(f"{t.date()} is not an aligned observation")
    if pos_t + 1 >= len(idx):
        raise ValueError(f"no observation after {t.date()}")
    nxt = pd.Timestamp(idx[pos_t + 1])
    factors = list(rm.changes.columns)
    x = rm.changes.iloc[pos_t + 1].to_numpy(dtype="float64")[None, :]
    levels_t: pd.Series = rm.levels.iloc[pos_t]
    pos = dict(positions)
    pnl = pnl_matrix(x, factors, levels_t, pos, specs, rm.factor_of)[0]
    d = deltas(levels_t, pos, specs, rm.factor_of, factors)
    rtpl = float(d @ x[0])
    losses = to_loss(pnl)
    return DailyPnl(
        as_of=t.date(),
        pnl_date=nxt.date(),
        hpl=float(pnl.sum()),
        rtpl=rtpl,
        loss_by_instrument={tk: float(v) for tk, v in zip(pos, losses, strict=True)},
    )


def horizon_pnl(
    rm: ReturnMatrix,
    t: pd.Timestamp,
    h: int,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> DailyPnl:
    """P&L of holding the day-t book over the next ``h`` aligned observations (note 06 §6-3).

    The ``h`` change vectors are summed by kind (log, abs and bp all add) and passed through
    the exact mapping once, exactly as the h-day block bootstrap builds its scenarios, so the
    realised loss and the h-day margin are measured the same way. ``h = 1`` reproduces
    :func:`daily_pnl`. ``pnl_date`` is the h-th observation after ``t``; the caller converts
    the gap to business days.
    """
    if h < 1:
        raise ValueError("h must be >= 1")
    idx = rm.changes.index
    pos_t = int(idx.get_indexer(pd.DatetimeIndex([t]))[0])
    if pos_t < 0:
        raise KeyError(f"{t.date()} is not an aligned observation")
    if pos_t + h >= len(idx):
        raise ValueError(f"fewer than {h} observations after {t.date()}")
    factors = list(rm.changes.columns)
    x = rm.changes.iloc[pos_t + 1 : pos_t + h + 1].to_numpy(dtype="float64").sum(axis=0)[None, :]
    levels_t: pd.Series = rm.levels.iloc[pos_t]
    pos = dict(positions)
    pnl = pnl_matrix(x, factors, levels_t, pos, specs, rm.factor_of)[0]
    d = deltas(levels_t, pos, specs, rm.factor_of, factors)
    losses = to_loss(pnl)
    return DailyPnl(
        as_of=t.date(),
        pnl_date=pd.Timestamp(idx[pos_t + h]).date(),
        hpl=float(pnl.sum()),
        rtpl=float(d @ x[0]),
        loss_by_instrument={tk: float(v) for tk, v in zip(pos, losses, strict=True)},
    )


def top_attribution(losses: Mapping[str, float], top: int = 5) -> dict[str, float]:
    """Largest ``top`` instrument losses plus the total, for the exception record."""
    ranked = sorted(losses.items(), key=lambda kv: -kv[1])[:top]
    out = {k: round(v, 2) for k, v in ranked}
    out["_total"] = round(float(sum(losses.values())), 2)
    return out
