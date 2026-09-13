"""Volatility filters for FHS (design note 05 §2-1, §2-3).

Conventions: ``x`` is the change matrix (T x k) in each factor's own unit. ``sigma2[t]`` is
the variance *forecast* for day ``t`` made with information up to ``t-1``, so ``x[t] /
sigma[t]`` is an out-of-sample standardised residual. Means are taken as zero (RiskMetrics).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize


def ewma_variance(x: np.ndarray, lam: float, init_window: int) -> np.ndarray:
    """EWMA variance forecasts, column by column.

    ``sigma2[0:init_window]`` equals the sample second moment of the first ``init_window``
    rows (the warm-up seed); from there ``sigma2[t] = lam*sigma2[t-1] + (1-lam)*x[t-1]^2``.
    Rows inside the warm-up are still returned so that callers can drop them explicitly.
    """
    x = np.asarray(x, dtype="float64")
    t_len = x.shape[0]
    if t_len < init_window + 1:
        raise ValueError(f"need more than init_window={init_window} rows, got {t_len}")
    sigma2 = np.empty_like(x)
    seed = np.mean(x[:init_window] ** 2, axis=0)
    sigma2[: init_window + 1] = seed
    for t in range(init_window + 1, t_len):
        sigma2[t] = lam * sigma2[t - 1] + (1.0 - lam) * x[t - 1] ** 2
    return sigma2


def ewma_covariance(x: np.ndarray, lam: float) -> np.ndarray:
    """EWMA covariance matrix at the end of the sample (RiskMetrics), zero means.

    Weights ``(1-lam) * lam^j`` on ``x[T-1-j]``, normalised to sum to one over the sample.
    """
    x = np.asarray(x, dtype="float64")
    t_len = x.shape[0]
    w = (1.0 - lam) * lam ** np.arange(t_len - 1, -1, -1)
    w = w / w.sum()
    cov: np.ndarray = (x * w[:, None]).T @ x
    return cov


@dataclass(frozen=True, slots=True)
class Garch11:
    """GARCH(1,1) parameters ``sigma2_t = omega + alpha x_{t-1}^2 + beta sigma2_{t-1}``."""

    omega: float
    alpha: float
    beta: float

    @property
    def persistence(self) -> float:
        """``alpha + beta``; must be < 1 for a finite unconditional variance."""
        return self.alpha + self.beta


def garch11_variance(x: np.ndarray, params: Garch11, init_window: int) -> np.ndarray:
    """Variance forecasts for one series under ``params`` with the same warm-up seed as EWMA."""
    x = np.asarray(x, dtype="float64")
    sigma2 = np.empty_like(x)
    seed = float(np.mean(x[:init_window] ** 2))
    sigma2[: init_window + 1] = seed
    for t in range(init_window + 1, len(x)):
        sigma2[t] = params.omega + params.alpha * x[t - 1] ** 2 + params.beta * sigma2[t - 1]
    return sigma2


def garch11_fit(x: np.ndarray, init_window: int) -> Garch11:
    """Gaussian quasi-MLE of GARCH(1,1) with variance targeting.

    ``omega`` is tied to the sample variance (``omega = var * (1 - alpha - beta)``), leaving
    ``alpha`` and ``beta`` to the optimiser (Engle & Mezrich 1996). Bounds keep
    ``alpha + beta < 1``. Returns the fitted parameters; the caller decides what to do with a
    fit that hits a bound.
    """
    x = np.asarray(x, dtype="float64")
    var = float(np.var(x))

    def negloglik(theta: np.ndarray) -> float:
        alpha, beta = theta
        if alpha < 0 or beta < 0 or alpha + beta >= 0.9999:
            return 1e12
        p = Garch11(var * (1.0 - alpha - beta), alpha, beta)
        s2 = garch11_variance(x, p, init_window)[init_window:]
        r = x[init_window:]
        return float(0.5 * np.sum(np.log(s2) + r**2 / s2))

    best = None
    for start in ((0.05, 0.90), (0.10, 0.85), (0.02, 0.95)):
        res = minimize(
            negloglik,
            np.array(start),
            method="Nelder-Mead",
            options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 2000},
        )
        if best is None or res.fun < best.fun:
            best = res
    assert best is not None
    alpha, beta = (float(v) for v in best.x)
    return Garch11(var * (1.0 - alpha - beta), alpha, beta)
