"""Controlled vocabularies shared between the database and Python.

The database enforces the same lists (``risk_measure_types`` rows, ``CHECK`` constraints in
``db/migrations/0002_risk_runs.sql``); ``tests/test_schema_db.py`` asserts the two stay equal.
"""

from enum import StrEnum
from typing import Final


class Method(StrEnum):
    """How the P&L distribution was generated (``risk_runs.method``)."""

    FHS = "fhs"
    PARAMETRIC = "parametric"
    MC = "mc"


class ScopeType(StrEnum):
    """What a result row refers to (``risk_measures.scope_type``)."""

    PORTFOLIO = "portfolio"
    ASSET_CLASS = "asset_class"
    INSTRUMENT = "instrument"
    FACTOR = "factor"


class Unit(StrEnum):
    """Unit of a measure. ``CURRENCY`` means the run's base currency."""

    CURRENCY = "currency"
    CURRENCY_PER_PCT = "currency_per_pct"
    CURRENCY_PER_BP = "currency_per_bp"


class Measure(StrEnum):
    """Result measures (``risk_measure_types.measure``). Losses are positive."""

    VAR = "var"
    ES = "es"
    STRESSED_ES = "stressed_es"
    COMPONENT_ES = "component_es"
    INCREMENTAL_VAR = "incremental_var"
    FACTOR_EXPOSURE = "factor_exposure"
    FACTOR_DV01 = "factor_dv01"
    IM = "im"
    IM_CORE = "im_core"
    IM_FLOOR = "im_floor"
    IM_STRESS_BLEND = "im_stress_blend"
    IM_LIQUIDITY_ADDON = "im_liquidity_addon"
    IM_CONCENTRATION_ADDON = "im_concentration_addon"
    IM_SPAN_LEGACY = "im_span_legacy"


MEASURE_UNITS: Final[dict[Measure, Unit]] = {
    Measure.VAR: Unit.CURRENCY,
    Measure.ES: Unit.CURRENCY,
    Measure.STRESSED_ES: Unit.CURRENCY,
    Measure.COMPONENT_ES: Unit.CURRENCY,
    Measure.INCREMENTAL_VAR: Unit.CURRENCY,
    Measure.FACTOR_EXPOSURE: Unit.CURRENCY_PER_PCT,
    Measure.FACTOR_DV01: Unit.CURRENCY_PER_BP,
    Measure.IM: Unit.CURRENCY,
    Measure.IM_CORE: Unit.CURRENCY,
    Measure.IM_FLOOR: Unit.CURRENCY,
    Measure.IM_STRESS_BLEND: Unit.CURRENCY,
    Measure.IM_LIQUIDITY_ADDON: Unit.CURRENCY,
    Measure.IM_CONCENTRATION_ADDON: Unit.CURRENCY,
    Measure.IM_SPAN_LEGACY: Unit.CURRENCY,
}
"""Unit of every measure. Must list every :class:`Measure` member exactly once."""
