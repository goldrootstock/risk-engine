"""Margin core, floor, blend, add-ons and legacy SPAN on synthetic data (no database)."""

from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.margin import core, span
from risk_engine.margin.params import MarginParams
from risk_engine.risk import fhs
from risk_engine.risk.returns import build

REPO_ROOT = Path(__file__).resolve().parents[1]

SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "EURGBP": InstrumentSpec(2, "ecb", "GBP", "EURGBP", "price", "log", "GBP", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
    "BRENT": InstrumentSpec(4, "eia", "RBRTE", "BRENT", "price", "absolute", "USD", 1.0),
    "UST_10Y": InstrumentSpec(5, "fred", "DGS10", "UST_10Y", "yield", "absolute", "USD", 1.0),
}
POS = {"EURUSD": 1_000_000.0, "WTI": 10_000.0, "BRENT": -5_000.0, "UST_10Y": 10_000_000.0}
FHS_KW = dict(lam=0.94, window=500, warmup=75)


def _params(**over: object) -> MarginParams:
    base = dict(
        confidence=0.99,
        measure="es",
        mpor_days=2,
        floor_lookback_days=2500,
        stress_weight=0.25,
        stress_window_days=250,
        liquidity_bp=MappingProxyType({"rates": 2.0, "fx": 2.0, "commodity": 5.0}),
        concentration_rate=0.005,
        concentration_threshold_usd=MappingProxyType({"rates": 5e8, "fx": 1e8, "commodity": 2e7}),
        span_scan_confidence=0.99,
        span_extreme_multiple=2.0,
        span_extreme_weight=0.35,
        span_vol_scan_pct=0.0,
        span_spread_credits=MappingProxyType({("WTI", "BRENT"): 0.8}),
        coverage_target=0.99,
        coverage_window_days=250,
        cover=2,
        allocation="pro_rata_im",
        sha256="0" * 64,
    )
    base.update(over)
    return MarginParams(**base)  # type: ignore[arg-type]


def _levels(n: int = 1200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n)
    common = rng.standard_normal(n)
    return pd.DataFrame(
        {
            "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(n) * 0.006)),
            "EURGBP": 0.85 * np.exp(np.cumsum(rng.standard_normal(n) * 0.005)),
            "WTI": 60 + np.cumsum((0.9 * common + 0.44 * rng.standard_normal(n)) * 1.5),
            "BRENT": 63 + np.cumsum((0.9 * common + 0.44 * rng.standard_normal(n)) * 1.5),
            "UST_10Y": 2.0 + np.cumsum(rng.standard_normal(n) * 0.05),
        },
        index=dates,
    )


def test_decomposition_identity_and_nonnegative_increments() -> None:
    rm = build(_levels(), SPECS)
    r = core.evaluate(rm, POS, SPECS, params=_params(), **FHS_KW)
    parts = r.im_core + r.im_floor + r.im_stress_blend + r.im_liquidity_addon
    parts += r.im_concentration_addon
    assert r.im == pytest.approx(parts, rel=1e-9)
    assert min(r.im_floor, r.im_stress_blend, r.im_liquidity_addon, r.im_concentration_addon) >= 0
    assert sum(r.component.values()) == pytest.approx(r.im_core, rel=1e-9)
    assert r.horizon == 2 and r.n_scenarios == 499  # W - h + 1 block scenarios
    assert r.var <= r.es  # same alpha: ES dominates VaR
    assert r.im_core == pytest.approx(r.es)  # measure = es
    assert r.stressed_window[0] < r.stressed_window[1]
    assert all(r.sigma_used[f] >= r.sigma_today[f] for f in r.sigma_used)


def test_two_day_block_exceeds_one_day_and_var_core_option() -> None:
    rm = build(_levels(), SPECS)
    one = core.evaluate(rm, POS, SPECS, params=_params(mpor_days=1), **FHS_KW)
    two = core.evaluate(rm, POS, SPECS, params=_params(), **FHS_KW)
    assert one.n_scenarios == 500 and two.n_scenarios == 499
    assert 1.1 < two.im_core / one.im_core < 2.0  # roughly sqrt(2) scaling on iid-ish data
    var_core = core.evaluate(rm, POS, SPECS, params=_params(measure="var"), **FHS_KW)
    assert var_core.im_core == pytest.approx(var_core.var)
    assert sum(var_core.component.values()) == pytest.approx(var_core.im_core, rel=1e-9)


def test_one_day_core_equals_the_risk_engine_es_at_the_same_alpha() -> None:
    """With h = 1 and no floor binding, im_core is the A1 FHS ES at 99 % (same primitives)."""
    lv = _levels()
    rm = build(lv, SPECS)
    p = _params(mpor_days=1, floor_lookback_days=1)  # floor from the last obs only: tiny
    r = core.evaluate(rm, POS, SPECS, params=p, **FHS_KW)
    a1 = fhs.evaluate(rm, POS, SPECS, var_alpha=0.99, es_alpha=0.99, stressed_window=250, **FHS_KW)
    assert r.es == pytest.approx(a1.es, rel=1e-12) and r.var == pytest.approx(a1.var, rel=1e-12)


def test_floor_binds_when_todays_volatility_is_low() -> None:
    lv = _levels()
    quiet = lv.copy()
    d = lv["WTI"].diff()
    # damp the last 120 days of WTI moves to a tenth: today's EWMA sigma << 10-year sigma
    quiet["WTI"] = lv["WTI"].iloc[0] + d.where(d.index < d.index[-120], d * 0.1).cumsum().fillna(0)
    r = core.evaluate(build(quiet, SPECS), {"WTI": 10_000.0}, SPECS, params=_params(), **FHS_KW)
    assert r.im_floor > 0 and r.sigma_used["WTI"] > r.sigma_today["WTI"] * 3
    loud = lv.copy()
    loud["WTI"] = lv["WTI"].iloc[0] + d.where(d.index < d.index[-120], d * 3).cumsum().fillna(0)
    r2 = core.evaluate(build(loud, SPECS), {"WTI": 10_000.0}, SPECS, params=_params(), **FHS_KW)
    assert r2.im_floor == 0.0 and r2.sigma_used["WTI"] == r2.sigma_today["WTI"]


def test_stress_blend_is_the_weighted_excess_of_the_worst_window() -> None:
    rm = build(_levels(), SPECS)
    r = core.evaluate(rm, POS, SPECS, params=_params(), **FHS_KW)
    level1 = r.im_core + r.im_floor
    assert r.im_stress_blend == pytest.approx(0.25 * max(0.0, r.stressed_es - level1))
    zero_w = core.evaluate(rm, POS, SPECS, params=_params(stress_weight=0.0), **FHS_KW)
    assert zero_w.im_stress_blend == 0.0


def test_add_ons_are_rate_times_notional_and_excess() -> None:
    rm = build(_levels(), SPECS)
    lv = rm.levels.iloc[-1]
    r = core.evaluate(rm, POS, SPECS, params=_params(), **FHS_KW)
    assert r.gross_notional["WTI"] == pytest.approx(10_000 * lv["WTI"])
    assert r.gross_notional["EURUSD"] == pytest.approx(1_000_000 * lv["EUR"])
    assert r.gross_notional["UST_10Y"] == 10_000_000  # par
    assert r.liquidity_by_ticker["WTI"] == pytest.approx(10_000 * lv["WTI"] * 5 / 1e4)
    assert r.liquidity_by_ticker["UST_10Y"] == pytest.approx(10_000_000 * 2 / 1e4)
    assert r.im_concentration_addon == 0.0  # every position below its threshold
    big = core.evaluate(rm, {"WTI": 1_000_000.0}, SPECS, params=_params(), **FHS_KW)
    excess = 1_000_000 * lv["WTI"] - 2e7
    assert big.concentration_by_ticker["WTI"] == pytest.approx(excess * 0.005)


def test_span_scan_risk_and_spread_credit() -> None:
    rm = build(_levels(), SPECS)
    p = _params()
    single = span.evaluate(rm, {"WTI": 10_000.0}, SPECS, params=p, window=500, warmup=75)
    psr = single.price_scan_range["WTI"]
    assert psr > 0
    # linear abs instrument: worst of the 16 scenarios is the full price scan move
    # (the 2 x 35 % extreme = 0.70 x PSR never binds)
    assert single.scan_risk["WTI"] == pytest.approx(10_000 * psr)
    assert single.im_span_legacy == pytest.approx(10_000 * psr)
    three_x = span.evaluate(
        rm,
        {"WTI": 10_000.0},
        SPECS,
        params=_params(span_extreme_multiple=3.0),
        window=500,
        warmup=75,
    )
    assert three_x.scan_risk["WTI"] == pytest.approx(10_000 * psr * 3 * 0.35)  # 1.05 x binds
    pair = span.evaluate(
        rm, {"WTI": 10_000.0, "BRENT": -5_000.0}, SPECS, params=p, window=500, warmup=75
    )
    assert pair.spread_credit["WTI/BRENT"] == pytest.approx(
        0.8 * min(pair.scan_risk["WTI"], pair.scan_risk["BRENT"])
    )
    assert pair.im_span_legacy == pytest.approx(
        pair.scan_risk["WTI"] + pair.scan_risk["BRENT"] - pair.spread_credit["WTI/BRENT"]
    )
    same_side = span.evaluate(
        rm, {"WTI": 10_000.0, "BRENT": 5_000.0}, SPECS, params=p, window=500, warmup=75
    )
    assert same_side.spread_credit == {}


def test_block_sums() -> None:
    a = np.arange(10, dtype=float).reshape(5, 2)
    b = core.block_sums(a, 2)
    assert b.shape == (4, 2) and b[0].tolist() == [2.0, 4.0] and b[-1].tolist() == [14.0, 16.0]
    assert core.block_sums(a, 1) is a
    with pytest.raises(ValueError):
        core.block_sums(a, 6)


def test_pool_too_short_for_stress_window_raises() -> None:
    rm = build(_levels(700), SPECS)
    with pytest.raises(ValueError, match="stress window"):
        core.evaluate(rm, POS, SPECS, params=_params(stress_window_days=700), **FHS_KW)


def test_params_load_from_repo_file_and_validation(tmp_path: Path) -> None:
    p = MarginParams.load(REPO_ROOT / "config" / "margin_params.toml")
    assert p.measure == "es" and p.mpor_days == 2 and len(p.sha256) == 64
    assert p.span_spread_credits[("WTI", "BRENT")] == 0.8
    assert p.as_record()["span_spread_credits"]["WTI/BRENT"] == 0.8
    bad = tmp_path / "m.toml"
    bad.write_text(
        (REPO_ROOT / "config" / "margin_params.toml").read_text().replace('"es"', '"cvar"')
    )
    with pytest.raises(ValueError, match="measure"):
        MarginParams.load(bad)
