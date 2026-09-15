"""Frozen margin parameters read from ``config/margin_params.toml`` (design note 09 §9).

The file is the only source of floors, blend weights, add-on rates and SPAN settings
(note 00 §3 #8). Its sha256 travels with every run so that "which settings produced this
number" can never be separated from the number. Values are pinned by
``tests/test_config_pins.py``.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_PARAMS_PATH = Path("config/margin_params.toml")

ASSET_CLASSES: tuple[str, ...] = ("rates", "fx", "commodity")


@dataclass(frozen=True, slots=True)
class MarginParams:
    """Every number the margin engine uses, plus the digest of the file they came from.

    Attributes:
        confidence: Tail confidence of the core measure (0.99).
        measure: ``"es"`` or ``"var"`` — which tail measure at ``confidence`` is the core.
        mpor_days: Margin period of risk in business days; the block-bootstrap horizon.
        floor_lookback_days: Observations of unfiltered volatility that floor today's sigma.
        stress_weight: Weight of the stressed-window measure in the blend.
        stress_window_days: Length of the stressed window scanned for the blend.
        liquidity_bp: Basis points of gross notional charged per asset class.
        concentration_rate: Charge on gross notional above the threshold.
        concentration_threshold_usd: Threshold per asset class.
        span_scan_confidence: Quantile of |h-day change| used as the SPAN price scan range.
        span_extreme_multiple: Extreme scenarios move price by this multiple of the range.
        span_extreme_weight: Fraction of the extreme-scenario loss counted.
        span_vol_scan_pct: Volatility scan range (no effect on a linear book).
        span_spread_credits: ``(ticker_a, ticker_b) -> credit rate`` on the smaller leg.
        coverage_target: Coverage the backtest tests against (0.99).
        coverage_window_days: Backtest window length.
        cover: Number of members the default fund covers (2).
        allocation: Default fund allocation rule (informational).
        sha256: Digest of the TOML file.
    """

    confidence: float
    measure: str
    mpor_days: int
    floor_lookback_days: int
    stress_weight: float
    stress_window_days: int
    liquidity_bp: MappingProxyType[str, float]
    concentration_rate: float
    concentration_threshold_usd: MappingProxyType[str, float]
    span_scan_confidence: float
    span_extreme_multiple: float
    span_extreme_weight: float
    span_vol_scan_pct: float
    span_spread_credits: MappingProxyType[tuple[str, str], float]
    coverage_target: float
    coverage_window_days: int
    cover: int
    allocation: str
    sha256: str

    @classmethod
    def load(cls, path: Path = DEFAULT_PARAMS_PATH) -> MarginParams:
        """Read and validate ``config/margin_params.toml``."""
        raw = path.read_bytes()
        cfg: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
        core, floor, blend = cfg["core"], cfg["floor"], cfg["stress_blend"]
        liq, conc, span = cfg["liquidity"], cfg["concentration"], cfg["span"]
        measure = str(core["measure"])
        if measure not in ("es", "var"):
            raise ValueError(f"[core].measure must be 'es' or 'var', got {measure!r}")
        if not 0.0 < float(core["confidence"]) < 1.0:
            raise ValueError("[core].confidence must lie in (0, 1)")
        if int(core["mpor_days"]) < 1:
            raise ValueError("[core].mpor_days must be >= 1")
        if not 0.0 <= float(blend["weight"]) <= 1.0:
            raise ValueError("[stress_blend].weight must lie in [0, 1]")
        credits = {}
        for key, rate in span.get("spread_credits", {}).items():
            a, _, b = key.partition("/")
            if not a or not b:
                raise ValueError(f"spread credit key must be 'A/B', got {key!r}")
            credits[(a, b)] = float(rate)
        liquidity = {ac: float(liq[f"{ac}_bp"]) for ac in ASSET_CLASSES}
        thresholds = {ac: float(conc["threshold_usd"][ac]) for ac in ASSET_CLASSES}
        return cls(
            confidence=float(core["confidence"]),
            measure=measure,
            mpor_days=int(core["mpor_days"]),
            floor_lookback_days=int(floor["lookback_days"]),
            stress_weight=float(blend["weight"]),
            stress_window_days=int(blend["window_days"]),
            liquidity_bp=MappingProxyType(liquidity),
            concentration_rate=float(conc["rate"]),
            concentration_threshold_usd=MappingProxyType(thresholds),
            span_scan_confidence=float(span["scan_confidence"]),
            span_extreme_multiple=float(span["extreme_multiple"]),
            span_extreme_weight=float(span["extreme_weight"]),
            span_vol_scan_pct=float(span["vol_scan_pct"]),
            span_spread_credits=MappingProxyType(credits),
            coverage_target=float(cfg["coverage"]["target"]),
            coverage_window_days=int(cfg["coverage"]["window_days"]),
            cover=int(cfg["default_fund"]["cover"]),
            allocation=str(cfg["default_fund"]["allocation"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def as_record(self) -> dict[str, Any]:
        """JSON-ready copy for ``risk_runs.params`` (mappings become plain dicts)."""
        return {
            "confidence": self.confidence,
            "measure": self.measure,
            "mpor_days": self.mpor_days,
            "floor_lookback_days": self.floor_lookback_days,
            "stress_weight": self.stress_weight,
            "stress_window_days": self.stress_window_days,
            "liquidity_bp": dict(self.liquidity_bp),
            "concentration_rate": self.concentration_rate,
            "concentration_threshold_usd": dict(self.concentration_threshold_usd),
            "span_scan_confidence": self.span_scan_confidence,
            "span_extreme_multiple": self.span_extreme_multiple,
            "span_extreme_weight": self.span_extreme_weight,
            "span_spread_credits": {
                f"{a}/{b}": r for (a, b), r in self.span_spread_credits.items()
            },
            "margin_params_sha256": self.sha256,
        }
