"""Kupiec, Christoffersen, traffic light, PLA and daily P&L on synthetic data."""

import numpy as np
import pandas as pd
import pytest
from scipy.stats import chi2

from risk_engine.backtest.hpl import daily_pnl, top_attribution
from risk_engine.backtest.runner import BacktestConfig, RunRecord, backtest
from risk_engine.backtest.statistics import (
    christoffersen_independence,
    conditional_coverage,
    kupiec_pof,
    pla,
    traffic_light,
)
from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.returns import build


def test_kupiec_known_values() -> None:
    # Kupiec (1995): n=250, p=1%, x=2.5 expected. x=2 -> not rejected; x=10 -> rejected.
    ok = kupiec_pof(250, 2, 0.01)
    bad = kupiec_pof(250, 10, 0.01)
    assert ok.p_value > 0.05 and bad.p_value < 0.05
    assert bad.statistic == pytest.approx(chi2.isf(bad.p_value, 1))
    zero = kupiec_pof(250, 0, 0.01)
    assert zero.statistic == pytest.approx(-2 * 250 * np.log(0.99))
    exact = kupiec_pof(1000, 10, 0.01)
    assert exact.statistic == pytest.approx(0.0, abs=1e-12)  # observed rate equals p


def test_christoffersen_detects_clustering() -> None:
    # seed chosen so that the i.i.d. draw is not a 5 % false positive (p = 0.046 for seed 0)
    rng = np.random.default_rng(1)
    iid = (rng.random(2000) < 0.05).astype(int)
    clustered = np.zeros(2000, dtype=int)
    clustered[500:560] = 1
    clustered[1200:1240] = 1
    assert christoffersen_independence(iid).p_value > 0.05
    assert christoffersen_independence(clustered).p_value < 1e-6
    none = christoffersen_independence(np.zeros(300, dtype=int))
    assert none.statistic == 0.0
    cc = conditional_coverage(
        kupiec_pof(2000, int(clustered.sum()), 0.01), christoffersen_independence(clustered)
    )
    assert cc.df == 2 and cc.p_value < 1e-6


def test_traffic_light_zones() -> None:
    assert [traffic_light(x) for x in (0, 4, 5, 9, 10, 20)] == [
        "green",
        "green",
        "yellow",
        "yellow",
        "red",
        "red",
    ]


def test_pla_zones() -> None:
    rng = np.random.default_rng(1)
    h = rng.standard_normal(250)
    kw = dict(spearman_green=0.80, spearman_amber=0.70, ks_green=0.09, ks_amber=0.12)
    assert pla(h, h * 1.01, **kw).zone == "green"
    assert pla(h, rng.standard_normal(250), **kw).zone == "red"
    r = pla(h, h + rng.standard_normal(250) * 0.9, **kw)
    assert r.zone in {"amber", "red"} and 0 < r.ks < 1


SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
}


def test_daily_pnl_matches_mapping_and_rtpl_is_linear() -> None:
    dates = pd.bdate_range("2020-01-01", periods=5)
    lv = pd.DataFrame(
        {"EURUSD": [1.10, 1.11, 1.09, 1.12, 1.10], "WTI": [60.0, 61.0, 58.0, 59.5, 62.0]},
        index=dates,
    )
    rm = build(lv, SPECS)
    t = rm.changes.index[1]
    pos = {"EURUSD": 1_000_000.0, "WTI": 1_000.0}
    d = daily_pnl(rm, t, pos, SPECS)
    x = rm.changes.loc[rm.changes.index[2]].to_numpy()[None, :]
    expected = pnl_matrix(x, list(rm.changes.columns), rm.levels.loc[t], pos, SPECS, rm.factor_of)[
        0
    ]
    assert d.hpl == pytest.approx(expected.sum())
    assert d.loss_by_instrument["WTI"] == pytest.approx(-1_000.0 * (59.5 - 58.0))
    # RTPL is the delta-linear P&L: q*S*ln(S1/S0) for FX, exact for the linear WTI leg
    assert d.rtpl == pytest.approx(1_000_000 * 1.09 * np.log(1.12 / 1.09) + 1_000.0 * 1.5)
    assert d.hpl == pytest.approx(1_000_000 * (1.12 - 1.09) + 1_000.0 * 1.5)
    with pytest.raises(ValueError):
        daily_pnl(rm, rm.changes.index[-1], pos, SPECS)
    assert top_attribution({"a": 3.0, "b": -1.0, "c": 2.0}, top=2) == {
        "a": 3.0,
        "c": 2.0,
        "_total": 4.0,
    }


def test_backtest_windows_and_exceptions() -> None:
    cfg = BacktestConfig(250, 0.99, 0.05, 5, 10, 0.80, 0.70, 0.09, 0.12, "0" * 64)
    from datetime import date, timedelta

    from risk_engine.backtest.hpl import DailyPnl

    runs, pnls = [], []
    start = date(2020, 1, 1)
    for i in range(600):
        d = start + timedelta(days=i)
        hpl = -150.0 if i % 100 == 0 else 10.0  # 6 exceptions, every 100 days
        runs.append(RunRecord(i + 1, d, 100.0, 120.0))
        pnls.append(DailyPnl(d, d + timedelta(days=1), hpl, hpl, {"WTI": -hpl}))
    rep = backtest(runs, pnls, cfg, universe="t", portfolio_code="P")
    assert len(rep.days) == 600 and sum(x.exception for x in rep.days) == 6
    assert [w.n_obs for w in rep.windows] == [250, 250, 250]  # two blocks + trailing window
    assert rep.windows[0].exceptions == 3 and rep.windows[0].traffic_light == "green"
    assert rep.days[0].attribution == {"WTI": 150.0, "_total": 150.0}
    assert rep.config is cfg  # never replaced
