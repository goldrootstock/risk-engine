"""Pinned configuration values (design note 00 §3-1).

Each table here is the *approved* content of a config file. The test parses the file
directly (not through the application loader) so that changing a value in the file
breaks CI until the table — and therefore the commit — changes with it. A parameter
change is then always visible as a two-file diff with a commit message explaining why.
"""

import csv
import tomllib
from datetime import date
from pathlib import Path

from risk_engine.data.etl.http import DEFAULT_RETRY_POLICY, RETRY_STATUSES
from risk_engine.data.etl.validate import load_thresholds

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_VALIDATION_THRESHOLDS: dict[str, dict[str, float]] = {
    "defaults": {"max_abs_return": 0.30, "max_abs_change_bp": 100.0, "max_abs_change": 10.0},
    "WTI": {"max_abs_change": 10.0},
    "BRENT": {"max_abs_change": 10.0},
    "GASOLINE_NYH": {"max_abs_change": 0.25},
    "GASOLINE_USGC": {"max_abs_change": 0.25},
    "HEATOIL_NYH": {"max_abs_change": 0.25},
    "JET_USGC": {"max_abs_change": 0.25},
    "PROPANE_MB": {"max_abs_change": 0.15},
    "HENRYHUB": {"max_abs_change": 2.0},
}

EXPECTED_RETRY_POLICY = {"attempts": 3, "backoff_seconds": 1.0, "timeout_seconds": 30.0}
EXPECTED_RETRY_STATUSES = {429, 500, 502, 503, 504}


def test_retry_policy_is_pinned() -> None:
    """The retry schedule lives in one place (etl/http.py) and is changed only with this table."""
    got = {
        "attempts": DEFAULT_RETRY_POLICY.attempts,
        "backoff_seconds": DEFAULT_RETRY_POLICY.backoff_seconds,
        "timeout_seconds": DEFAULT_RETRY_POLICY.timeout_seconds,
    }
    assert got == EXPECTED_RETRY_POLICY
    assert set(RETRY_STATUSES) == EXPECTED_RETRY_STATUSES


def test_validation_thresholds_are_pinned() -> None:
    with (REPO_ROOT / "config" / "validation.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    actual = {"defaults": cfg["defaults"], **cfg["tickers"]}
    assert actual == EXPECTED_VALIDATION_THRESHOLDS, (
        "config/validation.toml changed: update EXPECTED_VALIDATION_THRESHOLDS in the same "
        "commit and say why in the message (design note 00 §3-1)"
    )


def test_loader_returns_exactly_the_toml_values() -> None:
    """Guards the other half: validate() must take its thresholds from the file, not from code."""
    path = REPO_ROOT / "config" / "validation.toml"
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    for ticker, override in cfg["tickers"].items():
        expected = {**cfg["defaults"], **override}
        got = load_thresholds(ticker, path)
        assert (got.max_abs_return, got.max_abs_change_bp, got.max_abs_change) == (
            expected["max_abs_return"],
            expected["max_abs_change_bp"],
            expected["max_abs_change"],
        ), ticker
    unlisted = load_thresholds("EURUSD", path)
    assert (unlisted.max_abs_return, unlisted.max_abs_change_bp, unlisted.max_abs_change) == (
        cfg["defaults"]["max_abs_return"],
        cfg["defaults"]["max_abs_change_bp"],
        cfg["defaults"]["max_abs_change"],
    )


EXPECTED_RISK_PARAMS = {
    "fhs": {"lambda": 0.94, "window_days": 500, "warmup_days": 75},
    "measures": {
        "var_confidence": 0.99,
        "es_confidence": 0.975,
        "horizon_days": 1,
        "stressed_window_days": 250,
    },
    "montecarlo": {"paths": 10000, "seed": 20260913},
}


def test_risk_params_are_pinned() -> None:
    with (REPO_ROOT / "config" / "risk_params.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg == EXPECTED_RISK_PARAMS, (
        "config/risk_params.toml changed: update EXPECTED_RISK_PARAMS in the same commit "
        "and say why in the message (design note 00 §3-1)"
    )


EXPECTED_UNIVERSES = {
    "default": {"start": date(2006, 2, 9), "exclude": []},
    "from_1999": {"start": date(1999, 1, 4), "exclude": ["UST_1M", "EURCNY"]},
    "rates_energy_1990": {
        "start": date(1990, 4, 2),
        "include": [
            "UST_3M",
            "UST_6M",
            "UST_1Y",
            "UST_2Y",
            "UST_3Y",
            "UST_5Y",
            "UST_7Y",
            "UST_10Y",
            "WTI",
            "BRENT",
            "GASOLINE_NYH",
            "GASOLINE_USGC",
            "HEATOIL_NYH",
            "JET_USGC",
        ],
    },
}


def test_universe_sets_are_pinned() -> None:
    with (REPO_ROOT / "config" / "universes.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg == EXPECTED_UNIVERSES, "config/universes.toml changed: update EXPECTED_UNIVERSES"


EXPECTED_POSITIONS_MAIN = {
    "EURUSD": 20_000_000,
    "EURJPY": 2_000_000_000,
    "EURGBP": -5_000_000,
    "EURAUD": 10_000_000,
    "EURCHF": 5_000_000,
    "EURKRW": -10_000_000_000,
    "WTI": 200_000,
    "BRENT": -100_000,
    "HENRYHUB": 500_000,
    "GASOLINE_NYH": 2_000_000,
    "HEATOIL_NYH": -1_000_000,
    "UST_3M": 20_000_000,
    "UST_2Y": 50_000_000,
    "UST_5Y": 30_000_000,
    "UST_10Y": 40_000_000,
    "UST_30Y": -10_000_000,
}


def test_positions_main_is_pinned() -> None:
    """The book drives every risk number; changing it is a decision, not a tweak."""
    with (REPO_ROOT / "config" / "positions_main.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["ticker"]: float(r["quantity"]) for r in rows} == EXPECTED_POSITIONS_MAIN
    assert {r["portfolio_code"] for r in rows} == {"MAIN"}
    assert {r["as_of_date"] for r in rows} == {"1999-01-04"}  # before every sample set start


EXPECTED_BACKTEST_PARAMS = {
    "window": {"days": 250},
    "exceptions": {"confidence": 0.99, "significance": 0.05},
    "traffic_light": {"yellow_from": 5, "red_from": 10},
    "pla": {"spearman_green": 0.80, "spearman_amber": 0.70, "ks_green": 0.09, "ks_amber": 0.12},
}


def test_backtest_params_are_pinned() -> None:
    with (REPO_ROOT / "config" / "backtest_params.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg == EXPECTED_BACKTEST_PARAMS, "config/backtest_params.toml changed: update the table"


EXPECTED_STRESS_HISTORICAL = {
    "lehman_2008": (date(2008, 9, 12), date(2008, 10, 10)),
    "gfc_worst_week_2008": (date(2008, 10, 3), date(2008, 10, 10)),
    "covid_2020": (date(2020, 2, 19), date(2020, 3, 23)),
    "wti_negative_2020": (date(2020, 4, 17), date(2020, 4, 20)),
    "rates_2022": (date(2022, 1, 3), date(2022, 10, 21)),
    "rates_june_2022": (date(2022, 6, 9), date(2022, 6, 14)),
    "taper_2013": (date(2013, 5, 21), date(2013, 6, 24)),
    "oil_crash_2014": (date(2014, 11, 26), date(2015, 1, 15)),
    "sept11_2001": (date(2001, 9, 10), date(2001, 9, 21)),
    "election_2016": (date(2016, 11, 8), date(2016, 11, 14)),
    "texas_freeze_2021": (date(2021, 2, 11), date(2021, 2, 18)),
    "ukraine_2022": (date(2022, 2, 23), date(2022, 3, 8)),
}
EXPECTED_STRESS_HYPOTHETICAL = {
    "rates_up_100": {"rates_bp": 100},
    "rates_down_100": {"rates_bp": -100},
    "rates_up_200": {"rates_bp": 200},
    "usd_up_10": {"fx_pct": -10},
    "usd_down_10": {"fx_pct": 10},
    "oil_down_30": {"energy_pct": -30},
    "oil_up_30": {"energy_pct": 30},
    "gas_up_50": {"gas_pct": 50},
    "stagflation": {"rates_bp": 150, "energy_pct": 40, "fx_pct": -5},
    "deflation_shock": {"rates_bp": -150, "energy_pct": -40, "fx_pct": -8},
}


def test_stress_scenarios_are_pinned() -> None:
    with (REPO_ROOT / "config" / "stress_scenarios.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    hist = {k: (v["start"], v["end"]) for k, v in cfg["historical"].items()}
    hyp = {
        k: {kk: vv for kk, vv in v.items() if kk != "why"} for k, v in cfg["hypothetical"].items()
    }
    assert hist == EXPECTED_STRESS_HISTORICAL and hyp == EXPECTED_STRESS_HYPOTHETICAL
    assert cfg["correlation"] == {"permutations": 20, "seed": 20260913}
