"""Vendor sources. One module per vendor; each exposes a class satisfying ``contract.Source``."""

from __future__ import annotations

from risk_engine.data.etl.contract import Source
from risk_engine.data.etl.sources.ecb import EcbSource
from risk_engine.data.etl.sources.eia import EiaSource
from risk_engine.data.etl.sources.fred import FredSource

SOURCE_NAMES = ("ecb", "fred", "eia")


def build_sources(eia_api_key: str | None, fred_api_key: str | None) -> dict[str, Source]:
    """Instantiate every source keyed by ``instruments.source``.

    Keyed sources are included only when their key is configured, so ``sync --source eia``
    fails with a clear message instead of a 403 from the vendor.
    """
    sources: dict[str, Source] = {"ecb": EcbSource()}
    if fred_api_key:
        sources["fred"] = FredSource(api_key=fred_api_key)
    if eia_api_key:
        sources["eia"] = EiaSource(api_key=eia_api_key)
    return sources


__all__ = ["SOURCE_NAMES", "EcbSource", "EiaSource", "FredSource", "build_sources"]
