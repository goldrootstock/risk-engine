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

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from risk_engine.data.etl.contract import RawFile

#: File extension per source: the format is a property of the vendor, not of the payload.
EXTENSIONS: dict[str, str] = {"ecb": "zip", "fred": "json", "eia": "json"}
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

    def to_json(self) -> str:
        """Serialise as one JSON line (timestamps in ISO 8601 UTC)."""
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.astimezone(UTC).isoformat()
        d["pruned_at"] = self.pruned_at.astimezone(UTC).isoformat() if self.pruned_at else None
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> ManifestEntry:
        """Inverse of :meth:`to_json`."""
        d = json.loads(line)
        d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        d["pruned_at"] = datetime.fromisoformat(d["pruned_at"]) if d.get("pruned_at") else None
        return cls(**d)


def sha256_hex(content: bytes) -> str:
    """Hex SHA-256 of ``content``."""
    return hashlib.sha256(content).hexdigest()


def file_name(raw: RawFile, sha256: str) -> str:
    """``<fetched_at>_<sha12>_p<n>.<ext>`` for ``raw`` (see module docstring)."""
    stamp = raw.fetched_at.astimezone(UTC).strftime(FETCHED_AT_FORMAT)
    return f"{stamp}_{sha256[:12]}_p{raw.page}.{EXTENSIONS[raw.source]}"


class RawCache:
    """Reads and writes the cache under ``root`` (normally ``data/raw``)."""

    def __init__(self, root: Path) -> None:
        """Bind the cache to ``root``; directories are created on first write."""
        self.root = root

    def _dir(self, source: str, scope: str) -> Path:
        return self.root / source / scope

    def _append(self, source: str, scope: str, entry: ManifestEntry) -> None:
        directory = self._dir(source, scope)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / MANIFEST_NAME).open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")

    def entries(self, source: str, scope: str) -> Sequence[ManifestEntry]:
        """Manifest lines for ``scope`` in file order (read-only)."""
        manifest = self._dir(source, scope) / MANIFEST_NAME
        if not manifest.exists():
            return []
        lines = manifest.read_text(encoding="utf-8").splitlines()
        return [ManifestEntry.from_json(line) for line in lines if line.strip()]

    def _latest_distinct_sha(self, entries: Sequence[ManifestEntry], page: int) -> str | None:
        """SHA of the most recent non-pruned payload for ``page`` (resolving ``same_as``)."""
        pruned = {e.sha256 for e in entries if e.pruned_at is not None}
        for entry in reversed(entries):
            if entry.pruned_at is not None or entry.page != page:
                continue
            sha = entry.same_as or entry.sha256
            return None if sha in pruned else sha
        return None

    def store(self, raw: RawFile) -> ManifestEntry:
        """Record ``raw``: write a new file unless its SHA-256 equals the latest stored one.

        Never overwrites. Returns the manifest entry that was appended.
        """
        sha = sha256_hex(raw.content)
        existing = self.entries(raw.source, raw.scope)
        latest = self._latest_distinct_sha(existing, raw.page)
        path: str | None
        same_as: str | None
        if latest == sha:
            path, same_as = None, sha
        else:
            path, same_as = file_name(raw, sha), None
            target = self._dir(raw.source, raw.scope) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():  # same second, same bytes: keep the first, never overwrite
                raise FileExistsError(target)
            target.write_bytes(raw.content)
        entry = ManifestEntry(
            fetched_at=raw.fetched_at,
            sha256=sha,
            bytes=len(raw.content),
            scope=raw.scope,
            url=raw.url,
            page=raw.page,
            pages=raw.pages,
            path=path,
            same_as=same_as,
        )
        self._append(raw.source, raw.scope, entry)
        return entry

    def _path_for_sha(self, entries: Sequence[ManifestEntry], sha: str) -> str | None:
        for entry in entries:
            if entry.sha256 == sha and entry.path is not None and entry.pruned_at is None:
                return entry.path
        return None

    def latest(self, source: str, scope: str) -> Sequence[RawFile]:
        """All pages of the most recent fetch of ``scope``, reconstructed from disk.

        Follows ``same_as`` to the stored file. Empty when nothing was ever fetched or the
        latest payload has been pruned. Used by ``sync --offline``.
        """
        entries = [e for e in self.entries(source, scope) if e.pruned_at is None]
        if not entries:
            return []
        pruned = {e.sha256 for e in self.entries(source, scope) if e.pruned_at is not None}
        last_fetch = max(e.fetched_at for e in entries)
        pages = sorted((e for e in entries if e.fetched_at == last_fetch), key=lambda e: e.page)
        out: list[RawFile] = []
        for entry in pages:
            sha = entry.same_as or entry.sha256
            path = None if sha in pruned else self._path_for_sha(entries, sha)
            if path is None:
                return []
            out.append(
                RawFile(
                    source=source,
                    scope=scope,
                    url=entry.url,
                    content=(self._dir(source, scope) / path).read_bytes(),
                    fetched_at=entry.fetched_at,
                    page=entry.page,
                    pages=entry.pages,
                )
            )
        return out

    def prune(self, source: str, scope: str, keep_last: int) -> Sequence[ManifestEntry]:
        """Delete stored files older than the last ``keep_last`` distinct payloads.

        Manual command only (never called by ``sync``). For every deleted file a manifest
        record with ``pruned_at`` and ``path = None`` is appended; the original fetch
        records are left untouched. Returns the pruning records written.
        """
        entries = self.entries(source, scope)
        already = {e.sha256 for e in entries if e.pruned_at is not None}
        stored = [e for e in entries if e.path is not None and e.sha256 not in already]
        victims = stored[: max(0, len(stored) - keep_last)]
        now = datetime.now(UTC)
        records: list[ManifestEntry] = []
        for victim in victims:
            target = self._dir(source, scope) / str(victim.path)
            if target.exists():
                target.unlink()
            record = ManifestEntry(
                fetched_at=victim.fetched_at,
                sha256=victim.sha256,
                bytes=victim.bytes,
                scope=victim.scope,
                url=victim.url,
                page=victim.page,
                pages=victim.pages,
                path=None,
                same_as=None,
                pruned_at=now,
            )
            self._append(source, scope, record)
            records.append(record)
        return records
