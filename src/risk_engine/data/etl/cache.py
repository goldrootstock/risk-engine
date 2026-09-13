"""Append-only cache of vendor payloads (design note 03 §4, note 00 #2).

Layout::

    data/raw/<source>/<scope>/<fetched_at>_<sha12>_p<n>.<ext>
    data/raw/<source>/<scope>/manifest.jsonl

``fetched_at`` is UTC in the colon-free form ``YYYYMMDDTHHMMSSZ``; ``sha12`` is the first
12 hex digits of the SHA-256 of the content; ``p<n>`` is the page number (always present,
``p1`` for single-page sources). Nothing is ever overwritten: a payload whose SHA-256 equals
the most recent stored one for that scope is **not** written again, only recorded in the
manifest with ``same_as`` pointing at the earlier hash. Pruning is a separate, manual
command and also leaves a manifest record, so that "which dates can no longer be
reproduced offline" is answerable later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from risk_engine.data.etl.contract import RawFile

#: File extension per source: the format is a property of the vendor, not of the payload.
EXTENSIONS: dict[str, str] = {"ecb": "zip", "ustreasury": "csv", "eia": "json"}
MANIFEST_NAME = "manifest.jsonl"
FETCHED_AT_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One line of ``manifest.jsonl``. Byte-level facts only; row counts live in ``etl_runs``.

    Attributes:
        fetched_at: UTC time of the request.
        sha256: Full hex digest of the content.
        bytes: Content length.
        scope: Cache scope (``_all`` / year / source_id).
        url: Request URL without query string.
        page: Page number, 1-based.
        pages: Pages in this fetch of the scope.
        path: File name relative to the scope directory, or ``None`` when ``same_as`` is set
            (identical bytes were already stored) or after pruning.
        same_as: SHA-256 of the earlier identical payload, if this one was not written.
        pruned_at: Set on the record written by :meth:`RawCache.prune`.
    """

    fetched_at: datetime
    sha256: str
    bytes: int
    scope: str
    url: str
    page: int
    pages: int
    path: str | None
    same_as: str | None = None
    pruned_at: datetime | None = None


def file_name(raw: RawFile, sha256: str) -> str:
    """``<fetched_at>_<sha12>_p<n>.<ext>`` for ``raw`` (see module docstring)."""
    raise NotImplementedError


class RawCache:
    """Reads and writes the cache under ``root`` (normally ``data/raw``)."""

    def __init__(self, root: Path) -> None:
        """Bind the cache to ``root``; directories are created on first write."""
        self.root = root

    def store(self, raw: RawFile) -> ManifestEntry:
        """Record ``raw``: write a new file unless its SHA-256 equals the latest stored one.

        Never overwrites. Returns the manifest entry that was appended.
        """
        raise NotImplementedError

    def latest(self, source: str, scope: str) -> Sequence[RawFile]:
        """All pages of the most recent fetch of ``scope``, reconstructed from disk.

        Follows ``same_as`` to the stored file. Empty when nothing was ever fetched or the
        latest payload has been pruned. Used by ``sync --offline``.
        """
        raise NotImplementedError

    def entries(self, source: str, scope: str) -> Sequence[ManifestEntry]:
        """Manifest lines for ``scope`` in file order (read-only)."""
        raise NotImplementedError

    def prune(self, source: str, scope: str, keep_last: int) -> Sequence[ManifestEntry]:
        """Delete stored files older than the last ``keep_last`` distinct payloads.

        Manual command only (never called by ``sync``). For every deleted file a manifest
        record with ``pruned_at`` and ``path = None`` is appended; the original fetch
        records are left untouched. Returns the pruning records written.
        """
        raise NotImplementedError
