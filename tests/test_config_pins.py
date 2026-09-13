"""Pinned configuration values (design note 00 §3-1).

Each table here is the *approved* content of a config file. The test parses the file
directly (not through the application loader) so that changing a value in the file
breaks CI until the table — and therefore the commit — changes with it. A parameter
change is then always visible as a two-file diff with a commit message explaining why.
"""

import tomllib
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
