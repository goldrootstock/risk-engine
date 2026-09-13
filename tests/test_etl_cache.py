"""RawCache tests on tmp_path. xfail (strict) until the bodies are written."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from risk_engine.data.etl.cache import RawCache, file_name
from risk_engine.data.etl.contract import RawFile

pytestmark = pytest.mark.xfail(
    raises=NotImplementedError, strict=True, reason="skeleton: cache bodies not written yet"
)

T1 = datetime(2026, 9, 13, 10, 15, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 14, 10, 15, 0, tzinfo=UTC)


def _raw(content: bytes, at: datetime = T1, page: int = 1, pages: int = 1) -> RawFile:
    return RawFile(
        "eia", "RWTC", "https://api.eia.gov/v2/petroleum/pri/spt/data/", content, at, page, pages
    )


def test_file_name_format() -> None:
    sha = "ab12cd34ef56" + "0" * 52
    assert file_name(_raw(b"x"), sha) == "20260913T101500Z_ab12cd34ef56_p1.json"
    assert file_name(_raw(b"x", page=2, pages=2), sha) == "20260913T101500Z_ab12cd34ef56_p2.json"
    assert file_name(RawFile("ecb", "_all", "u", b"x", T1), sha).endswith("_p1.zip")


def test_store_never_overwrites_and_dedups_identical_bytes(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    first = cache.store(_raw(b"payload-1", T1))
    assert first.path is not None and first.same_as is None
    stored = tmp_path / "eia" / "RWTC" / first.path
    assert stored.read_bytes() == b"payload-1"

    same = cache.store(_raw(b"payload-1", T2))  # identical bytes a day later
    assert same.path is None and same.same_as == first.sha256
    assert len(list((tmp_path / "eia" / "RWTC").glob("*.json"))) == 1

    other = cache.store(_raw(b"payload-2", T2))
    assert other.path is not None and other.path != first.path
    assert stored.read_bytes() == b"payload-1"  # earlier file untouched
    assert len(cache.entries("eia", "RWTC")) == 3  # every store() leaves a manifest line


def test_latest_returns_all_pages_of_most_recent_fetch(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    cache.store(_raw(b"old", T1))
    cache.store(_raw(b"p1", T2, page=1, pages=2))
    cache.store(_raw(b"p2", T2, page=2, pages=2))
    latest = cache.latest("eia", "RWTC")
    assert [(r.page, r.content) for r in latest] == [(1, b"p1"), (2, b"p2")]
    assert cache.latest("eia", "NOPE") == []


def test_prune_records_what_it_deleted(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    e1 = cache.store(_raw(b"a", T1))
    e2 = cache.store(_raw(b"b", T2))
    pruned = cache.prune("eia", "RWTC", keep_last=1)
    assert [p.sha256 for p in pruned] == [e1.sha256]
    assert pruned[0].pruned_at is not None and pruned[0].path is None
    assert not (tmp_path / "eia" / "RWTC" / str(e1.path)).exists()
    assert (tmp_path / "eia" / "RWTC" / str(e2.path)).exists()
    assert cache.entries("eia", "RWTC")[0].path == e1.path  # original record untouched
