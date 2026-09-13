"""Loss, VaR and ES definitions (design note 05 §3).

``to_loss`` is the **only** place in the code base where P&L is negated (note 01 §1-1). Every
measure below takes losses (positive = loss) in the run's base currency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def to_loss(pnl: np.ndarray) -> np.ndarray:
    """``loss = -pnl``. The one sign flip."""
    return -np.asarray(pnl, dtype="float64")


def value_at_risk(loss: np.ndarray, alpha: float) -> float:
    """Empirical VaR: the ``ceil(alpha * n)``-th smallest loss (Basel order-statistic convention).

    With n = 500 and alpha = 0.99 this is the 5th largest loss.
    """
    srt = np.sort(np.asarray(loss, dtype="float64"))
    n = len(srt)
    idx = int(np.ceil(alpha * n)) - 1
    return float(srt[min(max(idx, 0), n - 1)])


def tail_weights(loss: np.ndarray, alpha: float) -> np.ndarray:
    """Acerbi-Tasche (2002) tail weights.

    1 on the worst ``floor((1-alpha) n)`` losses, a fraction on the boundary loss, 0
    elsewhere. ``weights.sum() == (1-alpha) * n`` exactly.
    """
    loss = np.asarray(loss, dtype="float64")
    n = len(loss)
    m = (1.0 - alpha) * n
    order = np.argsort(-loss, kind="mergesort")  # worst first
    w = np.zeros(n)
    full = int(np.floor(m))
    w[order[:full]] = 1.0
    if full < n:
        w[order[full]] = m - full
    return w


def expected_shortfall(loss: np.ndarray, alpha: float) -> float:
    """ES_alpha = sum(w * loss) / ((1-alpha) n) with Acerbi-Tasche weights."""
    loss = np.asarray(loss, dtype="float64")
    w = tail_weights(loss, alpha)
    return float((w * loss).sum() / ((1.0 - alpha) * len(loss)))


def component_es(loss_by_instrument: np.ndarray, alpha: float) -> np.ndarray:
    """Euler contributions: per-instrument mean loss over the same tail weights.

    ``loss_by_instrument`` has shape (n scenarios, k instruments); the portfolio loss is the
    row sum. Contributions sum to the portfolio ES exactly.
    """
    lbi = np.asarray(loss_by_instrument, dtype="float64")
    port = lbi.sum(axis=1)
    w = tail_weights(port, alpha)
    out: np.ndarray = (w[:, None] * lbi).sum(axis=0) / ((1.0 - alpha) * lbi.shape[0])
    return out


@dataclass(frozen=True, slots=True)
class TailMeasures:
    """VaR and ES of one loss distribution."""

    var: float
    es: float
    n_scenarios: int


def tail_measures(loss: np.ndarray, var_alpha: float, es_alpha: float) -> TailMeasures:
    """VaR and ES in one call."""
    return TailMeasures(
        value_at_risk(loss, var_alpha), expected_shortfall(loss, es_alpha), len(loss)
    )
