"""Return builder and P&L mapping on synthetic data (no database)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.risk.pnl import dv01, maturity_years, modified_duration, pnl_matrix
from risk_engine.risk.returns import build, factor_name, kind_of
from risk_engine.risk.universe import load_set

SPECS = {
    "EURUSD": InstrumentSpec(1, "ecb", "USD", "EURUSD", "price", "log", "USD", 1.0),
    "EURJPY": InstrumentSpec(2, "ecb", "JPY", "EURJPY", "price", "log", "JPY", 1.0),
    "WTI": InstrumentSpec(3, "eia", "RWTC", "WTI", "price", "absolute", "USD", 1.0),
    "UST_10Y": InstrumentSpec(4, "fred", "DGS10", "UST_10Y", "yield", "absolute", "USD", 1.0),
}
DATES = pd.to_datetime(["2020-04-17", "2020-04-20", "2020-04-21", "2020-04-22"])


def _levels() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EURUSD": [1.08, 1.09, 1.10, 1.10],
            "EURJPY": [116.0, 117.0, 116.0, 118.0],
            "WTI": [18.31, -36.98, 8.91, 13.64],
            "UST_10Y": [0.65, 0.63, 0.58, 0.63],
        },
        index=DATES,
    )


def test_kinds_and_factor_names() -> None:
    assert [kind_of(s) for s in SPECS.values()] == ["log", "log", "abs", "bp"]
    assert [factor_name(s) for s in SPECS.values()] == ["EUR", "JPY", "WTI", "UST_10Y"]


def test_build_changes_by_kind_and_fx_cross() -> None:
    rm = build(_levels(), SPECS)
    assert list(rm.changes.columns) == ["EUR", "JPY", "WTI", "UST_10Y"]
    assert rm.meta["n_obs"] == 3 and rm.meta["dropped_dates"] == 0
    assert rm.changes.index.equals(rm.levels.index)
    # FX: S_JPY = EURUSD / EURJPY, log change
    s_jpy = np.array([1.08 / 116.0, 1.09 / 117.0, 1.10 / 116.0, 1.10 / 118.0])
    np.testing.assert_allclose(rm.changes["JPY"].to_numpy(), np.diff(np.log(s_jpy)))
    np.testing.assert_allclose(rm.levels["JPY"].to_numpy(), s_jpy[1:])
    np.testing.assert_allclose(
        rm.changes["EUR"].to_numpy(), np.diff(np.log([1.08, 1.09, 1.10, 1.10]))
    )
    # energy: absolute difference survives the negative print
    np.testing.assert_allclose(rm.changes["WTI"].to_numpy(), [-55.29, 45.89, 4.73])
    # yields: basis points
    np.testing.assert_allclose(rm.changes["UST_10Y"].to_numpy(), [-2.0, -5.0, 5.0])


def test_intersection_drops_dates_and_ffill_fills_them() -> None:
    lv = _levels()
    lv.loc[DATES[2], "UST_10Y"] = np.nan  # FRED T+1 style hole
    inter = build(lv, SPECS)
    assert inter.meta["dropped_dates"] == 1 and inter.meta["n_obs"] == 2
    assert DATES[2] not in inter.changes.index
    ff = build(lv, SPECS, align="ffill", max_gap=1)
    assert ff.meta["filled_cells"] == 1 and ff.meta["n_obs"] == 3
    assert ff.changes.loc[DATES[2], "UST_10Y"] == 0.0  # the artificial zero the note warns about
    lv.loc[DATES[3], "UST_10Y"] = np.nan  # gap of 2 > max_gap: dropped
    assert build(lv, SPECS, align="ffill", max_gap=1).meta["dropped_dates"] == 1


def test_build_rejects_nonpositive_log_levels_and_unknown_specs() -> None:
    lv = _levels()
    lv.loc[DATES[1], "EURUSD"] = 0.0
    with pytest.raises(ValueError, match="positive"):
        build(lv, SPECS)
    with pytest.raises(KeyError):
        build(_levels().rename(columns={"WTI": "BRENT"}), SPECS)


def test_duration_closed_form_matches_numerical_bump() -> None:
    y, t = 0.0093, 10.0

    def par_bond_price(yield_, coupon, maturity):  # type: ignore[no-untyped-def]
        n = round(2 * maturity)
        c = coupon / 2
        return sum(c / (1 + yield_ / 2) ** k for k in range(1, n + 1)) + 1 / (1 + yield_ / 2) ** n

    h = 1e-6
    numeric = -(par_bond_price(y + h, y, t) - par_bond_price(y - h, y, t)) / (2 * h)
    assert modified_duration(y, t) == pytest.approx(numeric, rel=1e-6)
    assert modified_duration(0.0, 2.0) == 2.0  # y -> 0 limit is T
    assert modified_duration(-0.001, 1 / 12) == pytest.approx(1 / 12, rel=1e-3)  # negative bills
    assert maturity_years("UST_1M") == pytest.approx(1 / 12) and maturity_years("UST_30Y") == 30.0
    assert dv01(1_000_000, 0.93, 10.0) == pytest.approx(1_000_000 * modified_duration(y, t) * 1e-4)


def test_pnl_signs_and_units() -> None:
    rm = build(_levels(), SPECS)
    today = rm.levels.iloc[-1]
    x = np.array([[0.01, -0.02, -5.0, 10.0]])  # EUR +1%, JPY -2%, WTI -5 USD, 10Y +10 bp
    positions = {
        "EURUSD": 1_000_000.0,
        "EURJPY": 100_000_000.0,
        "WTI": 1_000.0,
        "UST_10Y": 10_000_000.0,
    }
    pnl = pnl_matrix(x, list(rm.changes.columns), today, positions, SPECS, rm.factor_of)
    eur, jpy, wti, ust = pnl[0]
    assert eur == pytest.approx(1_000_000 * 1.10 * np.expm1(0.01))  # long EUR gains when EUR rises
    assert jpy == pytest.approx(100_000_000 * (1.10 / 118.0) * np.expm1(-0.02))  # long JPY loses
    assert wti == pytest.approx(-5_000.0)  # 1,000 bbl x -5 USD
    assert ust == pytest.approx(-dv01(10_000_000, 0.63, 10.0) * 10.0)  # yields up, long bond loses
    assert ust < 0 < eur


def test_universe_sets_from_config() -> None:
    default = load_set("default")
    assert default.start == date(2006, 2, 9) and default.include is None and default.exclude == ()
    ex = load_set("from_1999")
    assert ex.select(["UST_1M", "UST_10Y", "EURCNY", "WTI"]) == ["UST_10Y", "WTI"]
    inc = load_set("rates_energy_1990")
    assert inc.include is not None and "EURUSD" not in inc.select(["EURUSD", "WTI", "UST_10Y"])
    with pytest.raises(KeyError):
        load_set("nope")
