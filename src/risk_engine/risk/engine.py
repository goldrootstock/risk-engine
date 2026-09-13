"""One risk run: data -> returns -> method -> measures (design note 05 §5).

Reads only; :mod:`risk_engine.risk.record` persists the result.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from risk_engine.data.etl.load import read_universe
from risk_engine.risk import fhs as fhs_mod
from risk_engine.risk import parametric as par_mod
from risk_engine.risk.levels import load_levels, load_specs
from risk_engine.risk.positions import load_snapshot
from risk_engine.risk.returns import Align, ReturnMatrix, build
from risk_engine.risk.universe import load_set

DEFAULT_PARAMS_PATH = Path("config/risk_params.toml")
DEFAULT_UNIVERSE_CSV = Path("config/universe.csv")


@dataclass(frozen=True, slots=True)
class RiskParams:
    """Pinned engine parameters plus the sha256 of the file they came from."""

    lam: float
    window_days: int
    warmup_days: int
    var_confidence: float
    es_confidence: float
    horizon_days: int
    stressed_window_days: int
    mc_paths: int
    mc_seed: int
    sha256: str

    @classmethod
    def load(cls, path: Path = DEFAULT_PARAMS_PATH) -> RiskParams:
        """Read ``config/risk_params.toml``."""
        raw = path.read_bytes()
        cfg = tomllib.loads(raw.decode("utf-8"))
        return cls(
            lam=float(cfg["fhs"]["lambda"]),
            window_days=int(cfg["fhs"]["window_days"]),
            warmup_days=int(cfg["fhs"]["warmup_days"]),
            var_confidence=float(cfg["measures"]["var_confidence"]),
            es_confidence=float(cfg["measures"]["es_confidence"]),
            horizon_days=int(cfg["measures"]["horizon_days"]),
            stressed_window_days=int(cfg["measures"]["stressed_window_days"]),
            mc_paths=int(cfg["montecarlo"]["paths"]),
            mc_seed=int(cfg["montecarlo"]["seed"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class Measure:
    """One ``risk_measures`` row."""

    measure: str
    confidence: float | None
    scope_type: str
    scope_key: str
    value: float


@dataclass(frozen=True, slots=True)
class RunResult:
    """Header fields plus measures for one run; written by :mod:`risk_engine.risk.record`."""

    portfolio_code: str
    as_of_date: date
    positions_as_of: date
    method: str
    horizon_days: int
    window_days: int
    n_scenarios: int
    portfolio_value: float
    measures: tuple[Measure, ...]
    params: dict[str, Any] = field(default_factory=dict)
    tag: str = "adhoc"


def prepare(
    conn: psycopg.Connection[Any],
    universe_name: str,
    as_of: date,
    *,
    align: Align = "intersection",
    max_gap: int = 1,
    universe_csv: Path = DEFAULT_UNIVERSE_CSV,
) -> tuple[ReturnMatrix, dict[str, Any], dict[str, Any]]:
    """Load levels for the universe set up to ``as_of`` and build the return matrix.

    Returns ``(rm, specs, meta)``; ``meta`` carries the universe name/sha and alignment stats
    for ``risk_runs.params``.
    """
    uset = load_set(universe_name)
    tickers = uset.select([r["ticker"] for r in read_universe(universe_csv)])
    levels = load_levels(conn, tickers, uset.start, as_of)
    specs = load_specs(conn, tickers)
    rm = build(levels, specs, align=align, max_gap=max_gap)
    meta = {"universe": uset.name, "universe_sha256": uset.sha256, **_jsonable(rm.meta)}
    return rm, specs, meta


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    """Dates -> ISO strings so that ``risk_runs.params`` serialises."""
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def run(
    conn: psycopg.Connection[Any],
    portfolio_code: str,
    as_of: date,
    *,
    method: str = "fhs",
    universe_name: str = "default",
    params: RiskParams | None = None,
    vol: str = "ewma",
    tag: str = "adhoc",
    align: Align = "intersection",
    prepared: tuple[ReturnMatrix, dict[str, Any], dict[str, Any]] | None = None,
) -> RunResult:
    """Evaluate one method for one portfolio on one date. Reads only."""
    p = params or RiskParams.load()
    rm, specs, meta = prepared or prepare(conn, universe_name, as_of, align=align)
    if rm.meta["last"] is None or rm.meta["last"] > as_of:
        raise ValueError("return matrix extends past as_of")
    positions, positions_as_of = load_snapshot(conn, portfolio_code, as_of)
    missing = [t for t in positions if t not in specs]
    if missing:
        raise KeyError(f"positions outside the universe set: {missing}")

    common = {
        **_jsonable(meta),
        "vol": vol,
        "lambda": p.lam,
        "warmup_days": p.warmup_days,
        "risk_params_sha256": p.sha256,
        "data_last_date": rm.meta["last"].isoformat(),
    }
    measures: list[Measure] = []
    extra: dict[str, Any]
    if method == "fhs":
        r = fhs_mod.evaluate(
            rm,
            positions,
            specs,
            lam=p.lam,
            window=p.window_days,
            warmup=p.warmup_days,
            var_alpha=p.var_confidence,
            es_alpha=p.es_confidence,
            stressed_window=p.stressed_window_days,
            vol=vol,
        )
        measures += [
            Measure("var", p.var_confidence, "portfolio", "", r.var),
            Measure("es", p.es_confidence, "portfolio", "", r.es),
            Measure("stressed_es", p.es_confidence, "portfolio", "", r.stressed_es),
            *[
                Measure(
                    "component_es", p.es_confidence, "instrument", str(specs[t].instrument_id), v
                )
                for t, v in r.component_es.items()
            ],
        ]
        extra = {
            "stressed_window": [d.isoformat() for d in r.stressed_window],
            "pool": [r.pool_start.isoformat(), r.pool_end.isoformat()],
            "sigma_today": r.sigma_today,
            "residual_mean": r.residual_mean,
        }
        n_scen, pv = r.n_scenarios, r.portfolio_value
    elif method == "parametric":
        pr = par_mod.parametric(
            rm,
            positions,
            specs,
            lam=p.lam,
            window=p.window_days,
            var_alpha=p.var_confidence,
            es_alpha=p.es_confidence,
        )
        measures += [
            Measure("var", p.var_confidence, "portfolio", "", pr.var),
            Measure("es", p.es_confidence, "portfolio", "", pr.es),
        ]
        extra = {"sigma_pnl": pr.sigma_pnl}
        n_scen = pr.n_obs
        pv = fhs_mod.portfolio_value(rm.levels.iloc[-1], positions, specs, rm.factor_of)
    elif method == "mc":
        mr = par_mod.monte_carlo(
            rm,
            positions,
            specs,
            lam=p.lam,
            window=p.window_days,
            paths=p.mc_paths,
            seed=p.mc_seed,
            var_alpha=p.var_confidence,
            es_alpha=p.es_confidence,
        )
        measures += [
            Measure("var", p.var_confidence, "portfolio", "", mr.var),
            Measure("es", p.es_confidence, "portfolio", "", mr.es),
            *[
                Measure(
                    "component_es", p.es_confidence, "instrument", str(specs[t].instrument_id), v
                )
                for t, v in mr.component_es.items()
            ],
        ]
        extra = {"paths": p.mc_paths, "seed": p.mc_seed}
        n_scen = mr.n_scenarios
        pv = fhs_mod.portfolio_value(rm.levels.iloc[-1], positions, specs, rm.factor_of)
    else:
        raise ValueError(f"unknown method {method!r}")

    return RunResult(
        portfolio_code=portfolio_code,
        as_of_date=as_of,
        positions_as_of=positions_as_of,
        method=method,
        horizon_days=p.horizon_days,
        window_days=p.window_days,
        n_scenarios=n_scen,
        portfolio_value=pv,
        measures=tuple(measures),
        params={**common, **extra},
        tag=tag,
    )
