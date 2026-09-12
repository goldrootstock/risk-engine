from pathlib import Path

import pytest

from risk_engine.data.migrate import discover


def _touch(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;")


def test_sorted_by_version_not_filename(tmp_path: Path) -> None:
    _touch(tmp_path, "0010_z.sql", "0002_b.sql", "0001_a.sql")
    assert [m.label for m in discover(tmp_path)] == ["0001_a", "0002_b", "0010_z"]


def test_rejects_bad_filename(tmp_path: Path) -> None:
    _touch(tmp_path, "init.sql")
    with pytest.raises(ValueError, match=r"NNNN_name\.sql"):
        discover(tmp_path)


def test_rejects_duplicate_version(tmp_path: Path) -> None:
    _touch(tmp_path, "0001_a.sql", "0001_b.sql")
    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover(tmp_path / "nope")
