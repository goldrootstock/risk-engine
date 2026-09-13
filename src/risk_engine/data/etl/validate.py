"""Read-only validation of a parsed series (design note 03 §5, note 00 #1).

:func:`validate` receives a frame and returns a :class:`Report`. It has no database
connection parameter on purpose: the function cannot persist anything, so it cannot be the
place where checks turn into corrections. The orchestrator decides what to do with the
report and writes ``etl_runs``.
"""

from __future__ import annotations

import hashlib
import tomllib
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from risk_engine.data.etl.contract import (
    PRICE_COLUMNS,
    Finding,
    FindingLevel,
    InstrumentSpec,
    JumpThresholds,
    Report,
)

DEFAULT_THRESHOLDS_PATH = Path("config/validation.toml")


def load_thresholds(ticker: str, path: Path = DEFAULT_THRESHOLDS_PATH) -> JumpThresholds:
    """Thresholds for ``ticker``: ``[tickers.<ticker>]`` overrides ``[defaults]`` key by key.

    Reads with :mod:`tomllib`. Missing ticker section means defaults only.
    """
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    merged = {**cfg["defaults"], **cfg.get("tickers", {}).get(ticker, {})}
    return JumpThresholds(
        max_abs_return=float(merged["max_abs_return"]),
        max_abs_change_bp=float(merged["max_abs_change_bp"]),
        max_abs_change=float(merged["max_abs_change"]),
    )


def thresholds_sha256(path: Path = DEFAULT_THRESHOLDS_PATH) -> str:
    """SHA-256 of the thresholds file, recorded in ``etl_runs.params`` for every run."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _structural(frame: pd.DataFrame, today: date) -> list[Finding]:
    findings: list[Finding] = []
    missing = [c for c in PRICE_COLUMNS if c not in frame.columns]
    if missing:
        findings.append(Finding("error", "missing_column", f"missing columns: {missing}"))
        return findings
    if not pd.api.types.is_datetime64_any_dtype(frame["price_date"]):
        findings.append(Finding("error", "bad_dtype", "price_date is not datetime64"))
        return findings
    for col in ("close", "adj_close"):
        if not pd.api.types.is_float_dtype(frame[col]):
            findings.append(Finding("error", "bad_dtype", f"{col} is not float"))
            return findings
    if frame.empty:
        findings.append(Finding("error", "empty_frame", "no rows"))
        return findings
    dupes = frame.loc[frame["price_date"].duplicated(), "price_date"]
    for d in dupes.drop_duplicates():
        findings.append(
            Finding("error", "duplicate_date", "duplicate price_date", price_date=d.date())
        )
    future = frame.loc[frame["price_date"].dt.date > today, "price_date"]
    for d in future:
        findings.append(
            Finding("error", "future_date", f"price_date after {today}", price_date=d.date())
        )
    return findings


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
          (absolute, in the series' own units). Consecutive observations are compared; the
          finding records ``prev_date`` and ``gap_days`` so that a change measured across a
          holiday or a hole is visible as a multi-day change. Thresholds are not scaled by
          the gap.
    """
    today = today or date.today()
    findings = _structural(frame, today)
    if any(f.code in {"missing_column", "bad_dtype", "empty_frame"} for f in findings):
        return Report(spec.source, spec.source_id, len(frame), None, None, tuple(findings))

    ordered = frame.sort_values("price_date")  # a sorted copy; the input is not touched
    dates = ordered["price_date"].dt.date.to_numpy()
    close = ordered["close"].to_numpy(dtype="float64")

    nonpositive_level: FindingLevel = "error" if spec.return_type == "log" else "warning"
    for d, p in zip(dates[close <= 0], close[close <= 0], strict=True):
        findings.append(
            Finding(
                nonpositive_level,
                "nonpositive_price",
                f"close <= 0 on {spec.return_type} series",
                d,
                float(p),
            )
        )

    if len(close) > 1:
        prev, curr, when, before = close[:-1], close[1:], dates[1:], dates[:-1]
        if spec.quote_type == "yield":
            delta = (curr - prev) * 100.0  # percent -> basis points
            mask = abs(delta) > thresholds.max_abs_change_bp
            unit, limit = "bp", thresholds.max_abs_change_bp
        elif spec.return_type == "absolute":
            delta = curr - prev
            mask = abs(delta) > thresholds.max_abs_change
            unit, limit = "units", thresholds.max_abs_change
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                delta = curr / prev - 1.0  # inf/nan on a zero base: already an error above
            mask = np.isfinite(delta) & (abs(delta) > thresholds.max_abs_return)
            unit, limit = "return", thresholds.max_abs_return
        for d, b, v in zip(when[mask], before[mask], delta[mask], strict=True):
            gap = (d - b).days
            findings.append(
                Finding(
                    "warning",
                    "jump",
                    f"|d| {abs(v):.4g} {unit} > {limit:g} over {gap} day(s) since {b}",
                    d,
                    float(v),
                    float(limit),
                    prev_date=b,
                    gap_days=gap,
                )
            )

    return Report(
        source=spec.source,
        source_id=spec.source_id,
        rows=len(frame),
        first=dates[0],
        last=dates[-1],
        findings=tuple(findings),
    )
