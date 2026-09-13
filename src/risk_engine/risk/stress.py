"""Stress scenarios (design note 07).

Historical replay with raw shocks, hypothetical shocks from the scenario file, correlation
break, volatility-filter lag.
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

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk import fhs as fhs_mod
from risk_engine.risk.measures import expected_shortfall, to_loss
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import ReturnMatrix

DEFAULT_SCENARIOS_PATH = Path("config/stress_scenarios.toml")


@dataclass(frozen=True, slots=True)
class ScenarioSet:
    """Parsed scenario file plus its digest."""

    historical: dict[str, dict[str, Any]]
    hypothetical: dict[str, dict[str, Any]]
    permutations: int
    seed: int
    sha256: str

    @classmethod
    def load(cls, path: Path = DEFAULT_SCENARIOS_PATH) -> ScenarioSet:
        """Read ``config/stress_scenarios.toml``."""
        raw = path.read_bytes()
        cfg = tomllib.loads(raw.decode("utf-8"))
        return cls(
            historical=dict(cfg.get("historical", {})),
            hypothetical=dict(cfg.get("hypothetical", {})),
            permutations=int(cfg["correlation"]["permutations"]),
            seed=int(cfg["correlation"]["seed"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class StressResult:
    """One ``stress_results`` row."""

    scenario: str
    kind: str
    loss: float
    attribution: dict[str, float] = field(default_factory=dict)
    window_start: date | None = None
    window_end: date | None = None
    worst_day: date | None = None
    worst_day_loss: float | None = None


def _snap(idx: pd.Index, d: date, side: str) -> int:
    """Position of ``d`` in ``idx``, snapped to an aligned date.

    ``side='left'`` snaps to the nearest aligned date before ``d``, ``'right'`` to the one after.
    """
    pos = int(idx.searchsorted(pd.Timestamp(d), side="left"))
    if pos < len(idx) and pd.Timestamp(idx[pos]).date() == d:
        return pos
    return max(pos - 1, 0) if side == "left" else min(pos, len(idx) - 1)


def _loss_vector(
    rm: ReturnMatrix,
    x: np.ndarray,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> np.ndarray:
    """Per-instrument losses for one change vector applied to today's levels."""
    pnl = pnl_matrix(
        x[None, :],
        list(rm.changes.columns),
        rm.levels.iloc[-1],
        dict(positions),
        specs,
        rm.factor_of,
    )[0]
    return to_loss(pnl)


def historical(
    rm: ReturnMatrix,
    name: str,
    start: date,
    end: date,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> StressResult:
    """Cumulative raw change over ``[start, end]`` applied to today's book. No scaling."""
    idx = rm.changes.index
    a, b = _snap(idx, start, "right"), _snap(idx, end, "left")
    if b <= a:
        raise ValueError(f"{name}: window {start}..{end} has no aligned observations")
    block = rm.changes.iloc[a + 1 : b + 1]  # changes *into* each date after the start date
    cum = block.sum(axis=0).to_numpy(dtype="float64")
    losses = _loss_vector(rm, cum, positions, specs)
    daily = np.array(
        [_loss_vector(rm, row, positions, specs).sum() for row in block.to_numpy(dtype="float64")]
    )
    worst = int(np.argmax(daily))
    tickers = list(positions)
    return StressResult(
        scenario=name,
        kind="historical",
        loss=float(losses.sum()),
        attribution={t: float(v) for t, v in zip(tickers, losses, strict=True)},
        window_start=idx[a].date(),
        window_end=idx[b].date(),
        worst_day=block.index[worst].date(),
        worst_day_loss=float(daily[worst]),
    )


def hypothetical(
    rm: ReturnMatrix,
    name: str,
    spec: Mapping[str, Any],
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> StressResult:
    """Shock vector from the scenario file.

    ``rates_bp`` shifts every bp factor; ``fx_pct`` moves every log factor by ``log(1+pct)``;
    ``energy_pct`` moves every abs factor by a percentage of today's level; ``gas_pct``
    overrides ``energy_pct`` for HENRYHUB.
    """
    factors = list(rm.changes.columns)
    x = np.zeros(len(factors))
    levels = rm.levels.iloc[-1]
    for j, f in enumerate(factors):
        k = rm.kind[f]
        if k == "bp" and "rates_bp" in spec:
            x[j] = float(spec["rates_bp"])
        elif k == "log" and "fx_pct" in spec:
            x[j] = float(np.log1p(float(spec["fx_pct"]) / 100.0))
        elif k == "abs":
            pct = (
                spec.get("gas_pct")
                if f == "HENRYHUB" and "gas_pct" in spec
                else spec.get("energy_pct")
            )
            if pct is not None:
                x[j] = float(levels[f]) * float(pct) / 100.0
    losses = _loss_vector(rm, x, positions, specs)
    return StressResult(
        name,
        "hypothetical",
        float(losses.sum()),
        {t: float(v) for t, v in zip(positions, losses, strict=True)},
    )


def correlation_break(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    *,
    lam: float,
    window: int,
    warmup: int,
    es_alpha: float,
    permutations: int,
    seed: int,
) -> list[StressResult]:
    """Joint FHS ES vs independently permuted residuals vs the sum of stand-alone ESs."""
    factors = list(rm.changes.columns)
    z, sigma = fhs_mod.standardised_residuals(rm.changes, lam, warmup)
    s_today = fhs_mod.sigma_next(rm.changes, sigma, lam).to_numpy(dtype="float64")
    pool = z.iloc[warmup:].iloc[-window:].to_numpy(dtype="float64")
    pos = dict(positions)

    def lbi(zblock: np.ndarray) -> np.ndarray:
        return to_loss(
            pnl_matrix(
                zblock * s_today[None, :], factors, rm.levels.iloc[-1], pos, specs, rm.factor_of
            )
        )

    joint = lbi(pool)
    es_joint = expected_shortfall(joint.sum(axis=1), es_alpha)
    rng = np.random.default_rng(seed)
    indep = []
    for _ in range(permutations):
        shuffled = np.column_stack([rng.permutation(pool[:, j]) for j in range(pool.shape[1])])
        indep.append(expected_shortfall(lbi(shuffled).sum(axis=1), es_alpha))
    es_indep = float(np.mean(indep))
    standalone = {t: expected_shortfall(joint[:, j], es_alpha) for j, t in enumerate(pos)}
    es_undiv = float(sum(standalone.values()))
    return [
        StressResult("es_joint", "correlation", float(es_joint)),
        StressResult("es_independent", "correlation", es_indep),
        StressResult("es_undiversified", "correlation", es_undiv, attribution=standalone),
    ]


def vol_filter_lag(historicals: list[StressResult], es_today: float) -> list[StressResult]:
    """Worst single historical day replayed against today's ES: loss / ES = filter lag."""
    out = []
    for h in historicals:
        if h.worst_day_loss is None:
            continue
        out.append(
            StressResult(
                f"lag:{h.scenario}",
                "vol_lag",
                float(h.worst_day_loss),
                worst_day=h.worst_day,
                window_start=h.window_start,
                window_end=h.window_end,
            )
        )
    return sorted(out, key=lambda r: -r.loss)


def run_all(
    rm: ReturnMatrix,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
    scen: ScenarioSet,
    *,
    lam: float,
    window: int,
    warmup: int,
    es_alpha: float,
    es_today: float,
) -> list[StressResult]:
    """Every scenario kind in one list."""
    hist = [
        historical(rm, n, s["start"], s["end"], positions, specs)
        for n, s in scen.historical.items()
    ]
    hyp = [hypothetical(rm, n, s, positions, specs) for n, s in scen.hypothetical.items()]
    corr = correlation_break(
        rm,
        positions,
        specs,
        lam=lam,
        window=window,
        warmup=warmup,
        es_alpha=es_alpha,
        permutations=scen.permutations,
        seed=scen.seed,
    )
    return hist + hyp + corr + vol_filter_lag(hist, es_today)
