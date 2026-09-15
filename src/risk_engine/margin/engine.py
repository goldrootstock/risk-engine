"""One margin run: data -> core + SPAN -> measures (design note 09 §2-§5, §8).

Reads only. The result is a :class:`~risk_engine.risk.engine.RunResult` written by
:func:`risk_engine.risk.record.write` into ``risk_runs`` / ``risk_measures`` — the same
header table as the risk engine, kept apart by ``tag`` and ``horizon_days`` (note 01 §10-2):

* ``method = 'fhs'``, ``horizon_days = params.mpor_days`` (2), ``tag = 'margin_batch'`` for
  the official series (``margin_adhoc`` otherwise);
* measures ``im``, ``im_core``, ``im_floor``, ``im_stress_blend``, ``im_liquidity_addon``,
  ``im_concentration_addon``, ``im_span_legacy`` (portfolio scope, no confidence);
  ``var`` / ``es`` / ``stressed_es`` at the core confidence and ``es`` at 0.975 over the
  MPOR; ``component_es`` and the two add-ons per instrument.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from risk_engine.margin import core, span
from risk_engine.margin.params import MarginParams
from risk_engine.risk.engine import Measure, RiskParams, RunResult, _jsonable, prepare
from risk_engine.risk.positions import load_snapshot
from risk_engine.risk.returns import Align, ReturnMatrix

OFFICIAL_TAG = "margin_batch"
IM_MEASURES = (
    "im",
    "im_core",
    "im_floor",
    "im_stress_blend",
    "im_liquidity_addon",
    "im_concentration_addon",
)


def run(
    conn: psycopg.Connection[Any],
    portfolio_code: str,
    as_of: date,
    *,
    universe_name: str = "from_1999",
    risk_params: RiskParams | None = None,
    margin_params: MarginParams | None = None,
    vol: str = "ewma",
    tag: str = "margin_adhoc",
    align: Align = "intersection",
    prepared: tuple[ReturnMatrix, dict[str, Any], dict[str, Any]] | None = None,
    horizon: int | None = None,
) -> RunResult:
    """Margin of one book on one date. Reads only; ``horizon`` overrides the MPOR (coverage)."""
    rp = risk_params or RiskParams.load()
    mp = margin_params or MarginParams.load()
    rm, specs, meta = prepared or prepare(conn, universe_name, as_of, align=align)
    if rm.meta["last"] is None or rm.meta["last"] > as_of:
        raise ValueError("return matrix extends past as_of")
    positions, positions_as_of = load_snapshot(conn, portfolio_code, as_of)
    missing = [t for t in positions if t not in specs]
    if missing:
        raise KeyError(f"positions outside the universe set: {missing}")

    r = core.evaluate(
        rm,
        positions,
        specs,
        lam=rp.lam,
        window=rp.window_days,
        warmup=rp.warmup_days,
        params=mp,
        vol=vol,
        horizon=horizon,
    )
    s = span.evaluate(
        rm,
        positions,
        specs,
        window=rp.window_days,
        warmup=rp.warmup_days,
        params=mp,
        horizon=horizon,
    )
    ids = {t: str(specs[t].instrument_id) for t in positions}
    a = mp.confidence
    measures: list[Measure] = [
        *(Measure(m, None, "portfolio", "", getattr(r, m)) for m in IM_MEASURES),
        Measure("im_span_legacy", None, "portfolio", "", s.im_span_legacy),
        Measure("var", a, "portfolio", "", r.var),
        Measure("es", a, "portfolio", "", r.es),
        Measure("es", 0.975, "portfolio", "", r.es_975),
        Measure("stressed_es", a, "portfolio", "", r.stressed_es),
        *(Measure("component_es", a, "instrument", ids[t], v) for t, v in r.component.items()),
        *(
            Measure("im_liquidity_addon", None, "instrument", ids[t], v)
            for t, v in r.liquidity_by_ticker.items()
        ),
        *(
            Measure("im_concentration_addon", None, "instrument", ids[t], v)
            for t, v in r.concentration_by_ticker.items()
        ),
    ]
    params = {
        **_jsonable(meta),
        "vol": vol,
        "lambda": rp.lam,
        "window_days": rp.window_days,
        "warmup_days": rp.warmup_days,
        "risk_params_sha256": rp.sha256,
        "data_last_date": rm.meta["last"].isoformat(),
        **mp.as_record(),
        "horizon": r.horizon,
        "stressed_window": [d.isoformat() for d in r.stressed_window],
        "pool": [r.pool_start.isoformat(), r.pool_end.isoformat()],
        "sigma_today": r.sigma_today,
        "sigma_floor": r.sigma_floor,
        "sigma_used": r.sigma_used,
        "gross_notional": r.gross_notional,
        "span": {
            "price_scan_range": s.price_scan_range,
            "scan_risk": s.scan_risk,
            "spread_credit": s.spread_credit,
        },
    }
    return RunResult(
        portfolio_code=portfolio_code,
        as_of_date=as_of,
        positions_as_of=positions_as_of,
        method="fhs",
        horizon_days=r.horizon,
        window_days=rp.window_days,
        n_scenarios=r.n_scenarios,
        portfolio_value=r.portfolio_value,
        measures=tuple(measures),
        params=params,
        tag=tag,
    )
