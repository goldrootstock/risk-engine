"""Named sample sets from ``config/universes.toml`` (design note 04 §3)."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_UNIVERSES_PATH = Path("config/universes.toml")


@dataclass(frozen=True, slots=True)
class UniverseSet:
    """One named sample set: a start date and either an include list or an exclude list.

    Attributes:
        name: Section name in the TOML file.
        start: First sample date.
        include: Tickers to use (``None`` = every ticker in the universe minus ``exclude``).
        exclude: Tickers to drop when ``include`` is ``None``.
        sha256: Digest of the TOML file, recorded in ``risk_runs.params``.
    """

    name: str
    start: date
    include: tuple[str, ...] | None
    exclude: tuple[str, ...]
    sha256: str

    def select(self, tickers: list[str]) -> list[str]:
        """Apply include/exclude to ``tickers`` preserving their order."""
        if self.include is not None:
            wanted = set(self.include)
            return [t for t in tickers if t in wanted]
        return [t for t in tickers if t not in set(self.exclude)]


def load_set(name: str, path: Path = DEFAULT_UNIVERSES_PATH) -> UniverseSet:
    """Read section ``name`` from the universes file."""
    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode("utf-8"))
    if name not in cfg:
        raise KeyError(f"universe set {name!r} not in {path}; available: {sorted(cfg)}")
    section = cfg[name]
    include = section.get("include")
    return UniverseSet(
        name=name,
        start=section["start"],
        include=tuple(include) if include is not None else None,
        exclude=tuple(section.get("exclude", ())),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
