"""Backtest statistics (design note 06 §2). Pure functions of exception sequences and P&L series."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import chi2, ks_2samp, spearmanr


@dataclass(frozen=True, slots=True)
class LrTest:
    """A likelihood-ratio test result."""

    statistic: float
    p_value: float
    df: int

    def rejects(self, significance: float) -> bool:
        """True when the null is rejected at ``significance``."""
        return self.p_value < significance


def _xlogy(x: float, y: float) -> float:
    return 0.0 if x == 0 else x * np.log(y)


def kupiec_pof(n: int, x: int, p: float) -> LrTest:
    """Kupiec (1995) proportion-of-failures test: H0 exception rate = ``p``.

    ``LR_uc = -2 [ln((1-p)^(n-x) p^x) - ln((1-x/n)^(n-x) (x/n)^x)] ~ chi2(1)``.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    pi = x / n
    ll0 = _xlogy(n - x, 1 - p) + _xlogy(x, p)
    ll1 = _xlogy(n - x, 1 - pi) + _xlogy(x, pi)
    lr = -2.0 * (ll0 - ll1)
    return LrTest(float(lr), float(chi2.sf(lr, 1)), 1)


def christoffersen_independence(exceptions: np.ndarray) -> LrTest:
    """Christoffersen (1998) independence test on a 0/1 exception sequence.

    Compares a first-order Markov chain (``pi01 != pi11``) with the i.i.d. alternative.
    ``LR_ind ~ chi2(1)``. With no exceptions at all the statistic is 0.
    """
    e = np.asarray(exceptions, dtype=int)
    if len(e) < 2:
        raise ValueError("need at least two observations")
    prev, curr = e[:-1], e[1:]
    n00 = int(((prev == 0) & (curr == 0)).sum())
    n01 = int(((prev == 0) & (curr == 1)).sum())
    n10 = int(((prev == 1) & (curr == 0)).sum())
    n11 = int(((prev == 1) & (curr == 1)).sum())
    pi01 = n01 / (n00 + n01) if (n00 + n01) else 0.0
    pi11 = n11 / (n10 + n11) if (n10 + n11) else 0.0
    pi = (n01 + n11) / (n00 + n01 + n10 + n11)
    ll0 = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    ll1 = _xlogy(n00, 1 - pi01) + _xlogy(n01, pi01) + _xlogy(n10, 1 - pi11) + _xlogy(n11, pi11)
    lr = max(0.0, -2.0 * (ll0 - ll1))
    return LrTest(float(lr), float(chi2.sf(lr, 1)), 1)


def conditional_coverage(uc: LrTest, ind: LrTest) -> LrTest:
    """``LR_cc = LR_uc + LR_ind ~ chi2(2)`` (Christoffersen 1998)."""
    lr = uc.statistic + ind.statistic
    return LrTest(float(lr), float(chi2.sf(lr, 2)), 2)


def traffic_light(exceptions: int, yellow_from: int = 5, red_from: int = 10) -> str:
    """Basel (1996) zones for exception counts over 250 days at 99 %."""
    if exceptions >= red_from:
        return "red"
    if exceptions >= yellow_from:
        return "yellow"
    return "green"


@dataclass(frozen=True, slots=True)
class PlaResult:
    """FRTB-style P&L attribution test between HPL and RTPL."""

    spearman: float
    ks: float
    zone: str


def pla(
    hpl: np.ndarray,
    rtpl: np.ndarray,
    *,
    spearman_green: float,
    spearman_amber: float,
    ks_green: float,
    ks_amber: float,
) -> PlaResult:
    """Spearman correlation and two-sample KS statistic with FRTB zoning (MAR32.11-13)."""
    rho = float(spearmanr(hpl, rtpl).statistic)
    ks = float(ks_2samp(hpl, rtpl).statistic)
    if rho > spearman_green and ks < ks_green:
        zone = "green"
    elif rho < spearman_amber or ks > ks_amber:
        zone = "red"
    else:
        zone = "amber"
    return PlaResult(rho, ks, zone)
