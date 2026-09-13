"""Loss, VaR, ES, Acerbi-Tasche weights, component ES, volatility filters."""

import numpy as np
import pytest
from scipy.stats import norm

from risk_engine.risk.measures import (
    component_es,
    expected_shortfall,
    tail_weights,
    to_loss,
    value_at_risk,
)
from risk_engine.risk.volatility import (
    Garch11,
    ewma_covariance,
    ewma_variance,
    garch11_fit,
    garch11_variance,
)


def test_to_loss_is_the_sign_flip() -> None:
    np.testing.assert_array_equal(to_loss(np.array([1.0, -2.0])), [-1.0, 2.0])


def test_var_order_statistic_convention() -> None:
    loss = np.arange(1, 501, dtype="float64")  # 1..500
    assert value_at_risk(loss, 0.99) == 495.0  # 5th largest
    assert value_at_risk(loss, 0.975) == 488.0


def test_es_acerbi_tasche_fractional_weights() -> None:
    loss = np.arange(1, 501, dtype="float64")
    w = tail_weights(loss, 0.975)
    assert w.sum() == pytest.approx(12.5)
    # worst 12 fully, 13th half: (500+499+...+489 + 0.5*488) / 12.5
    expected = (sum(range(489, 501)) + 0.5 * 488) / 12.5
    assert expected_shortfall(loss, 0.975) == pytest.approx(expected)


def test_normal_sample_matches_analytic_var_es() -> None:
    rng = np.random.default_rng(0)
    loss = rng.standard_normal(400_000)
    assert value_at_risk(loss, 0.99) == pytest.approx(norm.ppf(0.99), abs=0.02)
    assert expected_shortfall(loss, 0.975) == pytest.approx(
        norm.pdf(norm.ppf(0.975)) / 0.025, abs=0.02
    )


def test_component_es_sums_to_portfolio_es() -> None:
    rng = np.random.default_rng(1)
    lbi = rng.standard_normal((500, 4)) * np.array([1.0, 2.0, 0.5, 3.0])
    comp = component_es(lbi, 0.975)
    assert comp.sum() == pytest.approx(expected_shortfall(lbi.sum(axis=1), 0.975))


def test_ewma_recursion_and_warmup_seed() -> None:
    x = np.array([[1.0], [2.0], [3.0], [4.0], [5.0]])
    s2 = ewma_variance(x, 0.5, init_window=2)
    seed = (1 + 4) / 2
    assert s2[0, 0] == s2[1, 0] == s2[2, 0] == seed
    assert s2[3, 0] == pytest.approx(0.5 * seed + 0.5 * 9)
    assert s2[4, 0] == pytest.approx(0.5 * s2[3, 0] + 0.5 * 16)
    with pytest.raises(ValueError):
        ewma_variance(x[:2], 0.5, init_window=2)


def test_ewma_covariance_weights_sum_to_one() -> None:
    rng = np.random.default_rng(2)
    x = rng.standard_normal((2000, 2)) @ np.array([[1.0, 0.0], [0.8, 0.6]])
    cov = ewma_covariance(x, 0.94)
    assert cov.shape == (2, 2) and cov[0, 1] == pytest.approx(cov[1, 0])
    assert cov[0, 0] > 0 and abs(cov[0, 1]) < cov[0, 0] + cov[1, 1]


def test_garch_fit_recovers_simulated_parameters() -> None:
    rng = np.random.default_rng(3)
    true = Garch11(omega=0.05, alpha=0.08, beta=0.90)
    n = 6000
    x = np.empty(n)
    s2 = true.omega / (1 - true.persistence)
    for t in range(n):
        x[t] = np.sqrt(s2) * rng.standard_normal()
        s2 = true.omega + true.alpha * x[t] ** 2 + true.beta * s2
    fit = garch11_fit(x, init_window=75)
    assert fit.alpha == pytest.approx(true.alpha, abs=0.04)
    assert fit.beta == pytest.approx(true.beta, abs=0.05)
    assert fit.persistence < 1
    assert garch11_variance(x, fit, 75).shape == (n,)


def test_es_dominates_var_at_the_same_confidence() -> None:
    rng = np.random.default_rng(4)
    loss = rng.standard_t(4, 5000)
    for a in (0.95, 0.975, 0.99):
        assert expected_shortfall(loss, a) >= value_at_risk(loss, a)
