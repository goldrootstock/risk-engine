"""Read-only validation of a parsed series (design note 03 §5, note 00 #1).

:func:`validate` receives a frame and returns a :class:`Report`. It has no database
connection parameter on purpose: the function cannot persist anything, so it cannot be the
place where checks turn into corrections. The orchestrator decides what to do with the
report and writes ``etl_runs``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from risk_engine.data.etl.contract import InstrumentSpec, JumpThresholds, Report

DEFAULT_THRESHOLDS_PATH = Path("config/validation.toml")


def load_thresholds(ticker: str, path: Path = DEFAULT_THRESHOLDS_PATH) -> JumpThresholds:
    """Thresholds for ``ticker``: ``[tickers.<ticker>]`` overrides ``[defaults]`` key by key.

    Reads with :mod:`tomllib`. Missing ticker section means defaults only.
    """
    raise NotImplementedError


def thresholds_sha256(path: Path = DEFAULT_THRESHOLDS_PATH) -> str:
    """SHA-256 of the thresholds file, recorded in ``etl_runs.params`` for every run."""
    raise NotImplementedError


def validate(
    frame: pd.DataFrame,
    spec: InstrumentSpec,
    thresholds: JumpThresholds,
    *,
    today: date | None = None,
) -> Report:
    """Check one series' frame and return findings. Never modifies ``frame``.

    ``frame`` holds a single ``source_id`` (the orchestrator splits the parsed long frame).
    ``today`` defaults to ``date.today()`` and is injectable for tests.

    Errors (series is skipped entirely):
        * ``missing_column`` / ``bad_dtype`` — contract violation.
        * ``empty_frame`` — nothing to load.
        * ``duplicate_date`` — would violate the ``prices`` primary key.
        * ``future_date`` — ``price_date > today``.
        * ``nonpositive_price`` — ``close <= 0`` on a ``return_type = 'log'`` series
          (log returns undefined). On an ``absolute`` series the same condition is a
          **warning**: WTI 2020-04-20 is real data.

    Warnings (recorded, nothing changed):
        * ``jump`` — day-over-day change beyond the threshold for the series' return type:
          ``|P_t/P_{t-1} - 1| > max_abs_return`` (log), ``|Δy| > max_abs_change_bp``
          (yield; values are percent so Δ is multiplied by 100), ``|ΔP| > max_abs_change``
          (absolute, in the series' own units). Consecutive dates in the frame are compared;
          calendar gaps are not special-cased.
    """
    raise NotImplementedError
