"""Types shared by every ETL stage. No I/O, no pandas logic beyond the column contract.

The stages are deliberately kept apart by these types (design notes 00 and 03):

* :class:`Source.fetch` is the only place that touches the network and returns
  :class:`RawFile` objects (bytes as received, secrets already scrubbed).
* :class:`Source.parse` turns one RawFile into a long-format frame (:data:`PRICE_COLUMNS`).
* :func:`risk_engine.data.etl.validate.validate` reads a frame and returns a :class:`Report`.
  It has no way to reach the database: it never receives a connection.
* Only the sync orchestrator writes ``etl_runs`` rows, using :class:`Report` and
  :class:`LoadResult` as inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

import pandas as pd

#: Column contract of every parsed frame (long format, one row per series-date).
#: ``volume`` uses the nullable integer dtype because all three v1 sources have none.
PRICE_COLUMNS: dict[str, str] = {
    "source_id": "string",
    "price_date": "datetime64[ns]",
    "close": "float64",
    "adj_close": "float64",
    "volume": "Int64",
}

FindingLevel = Literal["error", "warning", "info"]

#: Closed vocabulary of finding codes. ``etl_runs.findings`` stores these strings, so a
#: new code is added here first and documented in design note 03 §12.
FindingCode = Literal[
    "missing_column",
    "bad_dtype",
    "duplicate_date",
    "future_date",
    "nonpositive_price",
    "jump",
    "unknown_source_id",
    "series_mismatch",
    "row_count_mismatch",
    "http_error",
    "empty_frame",
]


@dataclass(frozen=True, slots=True)
class RawFile:
    """One vendor payload exactly as received, plus where it came from.

    Attributes:
        source: ``instruments.source`` value (``ecb`` / ``ustreasury`` / ``eia``).
        scope: What the file covers: ``_all`` (ECB, every currency), a year (Treasury) or a
            ``source_id`` (EIA). Becomes the cache directory.
        url: Request URL **without** its query string (EIA keys live in the query string).
        content: Bytes as received, after secret scrubbing (EIA echoes the API key).
        fetched_at: UTC time of the request.
        page: 1-based page number for paginated sources; ``1`` otherwise.
        pages: Total pages of this scope in this fetch; ``1`` otherwise.
    """

    source: str
    scope: str
    url: str
    content: bytes
    fetched_at: datetime
    page: int = 1
    pages: int = 1


class Source(Protocol):
    """A vendor. Implementations do not inherit from this; matching signatures suffice.

    Two methods, two responsibilities. ``fetch`` may use the network and must not parse;
    ``parse`` must not use the network. This lets the cache store bytes, lets fixtures test
    ``parse`` offline, and keeps every side effect in one function.

    HTTP client lifetime: a source accepts an optional ``httpx.Client`` at construction. An
    injected client is borrowed for every ``fetch`` and never closed by the source; without
    one, ``fetch`` opens a client for the duration of that single call and closes it on
    exit (``risk_engine.data.etl.http.client_for``). Retries follow
    ``risk_engine.data.etl.http.fetch_with_retry``; sources do not write their own loops.
    """

    name: str

    def fetch(
        self,
        source_ids: Sequence[str],
        start: date | None,
        end: date | None,
    ) -> Sequence[RawFile]:
        """Download what is needed to cover ``source_ids`` between ``start`` and ``end``.

        ``start=None`` means the full available history (design note 02 §4-2). A source that
        serves several series per file returns one RawFile for all of them (ECB: one file;
        Treasury: one file per year). Secrets are removed from ``content`` before returning.
        """
        ...

    def parse(self, raw: RawFile) -> pd.DataFrame:
        """Turn one RawFile into a long frame with exactly :data:`PRICE_COLUMNS`.

        Returns every series present in the file (the orchestrator selects the universe).
        Rows whose value is missing are dropped, never filled. ``close == adj_close`` for
        all v1 sources.

        Ordering contract: the frame is sorted by ``(source_id, price_date)`` ascending and
        its index is a fresh ``RangeIndex`` (0..n-1). A frame that keeps the vendor's
        descending order or a filtered index would break positional joins downstream, so
        ``tests/test_etl_parse.py::_assert_contract`` checks exactly this.
        """
        ...


@dataclass(frozen=True, slots=True)
class Finding:
    """One observation from :func:`validate`. Serialised 1:1 into ``etl_runs.findings``."""

    level: FindingLevel
    code: FindingCode
    detail: str
    price_date: date | None = None
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, slots=True)
class Report:
    """Result of validating one series. Carries findings; changes nothing.

    ``ok`` is False when any finding is an error, in which case the orchestrator skips the
    whole series (no partial loads) and records the report in ``etl_runs`` with
    ``status = 'skipped'``.
    """

    source: str
    source_id: str
    rows: int
    first: date | None
    last: date | None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no finding has level ``error``."""
        return not any(f.level == "error" for f in self.findings)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Counts from one ``prices`` upsert. ``updated`` = vendor restatements of past values."""

    instrument_id: int
    fetched: int
    inserted: int
    updated: int
    unchanged: int


@dataclass(frozen=True, slots=True)
class JumpThresholds:
    """Day-over-day change limits above which :func:`validate` emits a ``jump`` warning.

    Chosen per ``return_type`` (design note 03 §5): a percentage is meaningless for a series
    that can be negative or near zero (WTI 2020-04-20), so absolute series use units.

    Attributes:
        max_abs_return: ``|P_t / P_{t-1} - 1|`` for ``return_type = 'log'`` series.
        max_abs_change_bp: ``|Δy|`` in basis points for yield series.
        max_abs_change: ``|ΔP|`` in the series' own units for absolute price series.
    """

    max_abs_return: float
    max_abs_change_bp: float
    max_abs_change: float


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """The subset of an ``instruments`` row that validation and loading need."""

    instrument_id: int
    source: str
    source_id: str
    ticker: str
    quote_type: Literal["price", "yield"]
    return_type: Literal["log", "absolute"]
