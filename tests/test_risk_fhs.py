"""FHS, parametric and Monte Carlo on synthetic data (no database)."""

import numpy as np
import pandas as pd
import pytest

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk import fhs, parametric
from risk_engine.risk.returns import build

SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
    "UST_10Y": InstrumentSpec(4, "fred", "DGS10", "UST_10Y", "yield", "absolute", "USD", 1.0),
}
KW = dict(lam=0.94, window=500, warmup=75, var_alpha=0.99, es_alpha=0.975, stressed_window=250)


def _levels(n: int = 900, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n)
    eur = 1.1 * np.exp(np.cumsum(rng.standard_normal(n) * 0.006))
    wti = 60 + np.cumsum(rng.standard_normal(n) * 1.5)
    y10 = 2.0 + np.cumsum(rng.standard_normal(n) * 0.05)
    return pd.DataFrame({"EURUSD": eur, "WTI": wti, "UST_10Y": y10}, index=dates)


POS = {"EURUSD": 1_000_000.0, "WTI": 10_000.0, "UST_10Y": 10_000_000.0}


def test_fhs_result_shapes_and_component_sum() -> None:
    rm = build(_levels(), SPECS)
    r = fhs.evaluate(rm, POS, SPECS, **KW)
    assert r.n_scenarios == 500
    assert r.var > 0 and r.es > 0  # ES 97.5 vs VaR 99 has no fixed order; both are losses
    assert sum(r.component_es.values()) == pytest.approx(r.es, rel=1e-9)
    assert r.stressed_es >= r.es * 0.5 and r.stressed_window[0] < r.stressed_window[1]
    assert set(r.sigma_today) == {"EUR", "WTI", "UST_10Y"}
    assert r.portfolio_value == pytest.approx(
        1_000_000 * rm.levels["EUR"].iloc[-1] + 10_000 * rm.levels["WTI"].iloc[-1] + 10_000_000
    )


def test_fhs_scales_with_todays_volatility() -> None:
    lv = _levels()
    rm = build(lv, SPECS)
    base = fhs.evaluate(rm, {"WTI": 1_000.0}, SPECS, **KW)
    # double the last 100 days' WTI moves -> today's EWMA sigma roughly doubles -> ES ~doubles
    lv2 = lv.copy()
    d = lv["WTI"].diff()
    lv2["WTI"] = lv["WTI"].iloc[0] + (d.where(d.index < d.index[-100], d * 2)).cumsum().fillna(0)
    boosted = fhs.evaluate(build(lv2, SPECS), {"WTI": 1_000.0}, SPECS, **KW)
    assert 1.5 < boosted.es / base.es < 2.6


def test_parametric_and_mc_agree_on_linear_book() -> None:
    rm = build(_levels(2000, seed=5), SPECS)
    pos = {"WTI": 10_000.0}  # absolute kind: exactly linear
    p = parametric.parametric(rm, pos, SPECS, lam=0.94, window=500, var_alpha=0.99, es_alpha=0.975)
    m = parametric.monte_carlo(
        rm, pos, SPECS, lam=0.94, window=500, paths=200_000, seed=1, var_alpha=0.99, es_alpha=0.975
    )
    assert m.var == pytest.approx(p.var, rel=0.03)
    assert m.es == pytest.approx(p.es, rel=0.03)
    assert m.component_es["WTI"] == pytest.approx(m.es)


def test_pool_too_short_raises() -> None:
    rm = build(_levels(300), SPECS)
    with pytest.raises(ValueError, match="window"):
        fhs.evaluate(rm, POS, SPECS, **KW)
