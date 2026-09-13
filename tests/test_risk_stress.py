"""Stress scenarios on synthetic data (no database)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk import stress
from risk_engine.risk.returns import build

SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
    "HENRYHUB": InstrumentSpec(5, "eia", "RNGWHHD", "HENRYHUB", "price", "absolute", "USD", 1.0),
    "UST_10Y": InstrumentSpec(4, "fred", "DGS10", "UST_10Y", "yield", "absolute", "USD", 1.0),
}
POS = {"EURUSD": 1_000_000.0, "WTI": 10_000.0, "HENRYHUB": 100_000.0, "UST_10Y": 10_000_000.0}


def _rm(n: int = 900, seed: int = 0):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n)
    lv = pd.DataFrame(
        {
            "EURUSD": 1.1 * np.exp(np.cumsum(rng.standard_normal(n) * 0.006)),
            "WTI": 60 + np.cumsum(rng.standard_normal(n) * 1.5),
            "HENRYHUB": 3 + np.cumsum(rng.standard_normal(n) * 0.1),
            "UST_10Y": 2.0 + np.cumsum(rng.standard_normal(n) * 0.05),
        },
        index=dates,
    )
    return build(lv, SPECS), lv


def test_historical_is_the_unscaled_cumulative_change() -> None:
    rm, lv = _rm()
    idx = rm.changes.index
    start, end = idx[100].date(), idx[110].date()
    r = stress.historical(rm, "w", start, end, POS, SPECS)
    # WTI leg: quantity x (level_end - level_start), no scaling, sign flipped to loss
    assert r.attribution["WTI"] == pytest.approx(
        -10_000.0 * (lv.loc[idx[110], "WTI"] - lv.loc[idx[100], "WTI"])
    )
    assert r.window_start == start and r.window_end == end
    assert r.worst_day is not None and start < r.worst_day <= end
    assert r.worst_day_loss is not None and r.worst_day_loss <= max(r.loss, r.worst_day_loss)
    # window boundaries snap to aligned observations
    off = stress.historical(
        rm, "w2", date(2020, 1, 4), date(2020, 1, 12), POS, SPECS
    )  # weekend endpoints
    # Sat 01-04 snaps right to Mon 01-06, Sun 01-12 snaps left to Fri 01-10 (dates, not
    # positions: changes.index drops the first level date, so idx[k] is one day later)
    assert off.window_start == date(2020, 1, 6) and off.window_end == date(2020, 1, 10)
    with pytest.raises(ValueError):
        stress.historical(rm, "bad", idx[5].date(), idx[5].date(), POS, SPECS)


def test_hypothetical_shock_signs_and_units() -> None:
    rm, _ = _rm()
    up = stress.hypothetical(rm, "rates_up", {"rates_bp": 100}, POS, SPECS)
    assert (
        up.attribution["UST_10Y"] > 0 and up.attribution["WTI"] == 0.0
    )  # yields up -> long bond loses
    usd_up = stress.hypothetical(rm, "usd_up", {"fx_pct": -10}, POS, SPECS)
    assert usd_up.attribution["EURUSD"] == pytest.approx(
        1_000_000 * rm.levels["EUR"].iloc[-1] * 0.10
    )  # long EUR loses 10 %
    oil = stress.hypothetical(rm, "oil", {"energy_pct": -30, "gas_pct": 50}, POS, SPECS)
    assert oil.attribution["WTI"] == pytest.approx(10_000 * 0.30 * rm.levels["WTI"].iloc[-1])
    assert oil.attribution["HENRYHUB"] == pytest.approx(
        -100_000 * 0.50 * rm.levels["HENRYHUB"].iloc[-1]
    )  # long gas gains


def test_correlation_break_ordering_and_lag() -> None:
    rm, _ = _rm(seed=2)
    res = stress.correlation_break(
        rm, POS, SPECS, lam=0.94, window=500, warmup=75, es_alpha=0.975, permutations=5, seed=1
    )
    by = {r.scenario: r.loss for r in res}
    assert by["es_undiversified"] >= by["es_joint"] > 0  # rho = 1 bound dominates the joint ES
    assert set(res[2].attribution) == set(POS)
    hist = [
        stress.historical(
            rm, "w", rm.changes.index[200].date(), rm.changes.index[260].date(), POS, SPECS
        )
    ]
    lag = stress.vol_filter_lag(hist, es_today=by["es_joint"])
    assert lag[0].kind == "vol_lag" and lag[0].loss == hist[0].worst_day_loss


def test_scenario_file_loads_with_required_entries() -> None:
    scen = stress.ScenarioSet.load()
    assert "wti_negative_2020" in scen.historical  # the day the model missed must be present
    assert scen.historical["wti_negative_2020"]["end"] == date(2020, 4, 20)
    assert all("why" in s for s in scen.historical.values())
    assert scen.permutations == 20 and len(scen.sha256) == 64
