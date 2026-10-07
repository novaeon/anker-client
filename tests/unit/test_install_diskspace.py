"""Tests for free-space math and volume detection."""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import pytest

from anker_client.constants import DISK_SPACE_FACTOR, DISK_SPACE_HEADROOM_BYTES
from anker_client.core.errors import DiskSpaceError
from anker_client.services.install import diskspace
from anker_client.services.install.diskspace import ensure_space, free_bytes, required_bytes, same_volume

GB = 1024**3
HEADROOM = DISK_SPACE_HEADROOM_BYTES


@pytest.fixture
def two_volumes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Paths under ``D:\\`` are on one volume, everything else on ``C:\\``; free bytes are configurable."""
    free = {"C:\\": 100 * GB, "D:\\": 100 * GB}

    def volume_root(path: str) -> str:
        return "D:\\" if str(path).upper().startswith("D:") else "C:\\"

    monkeypatch.setattr(diskspace, "_volume_root", volume_root)
    monkeypatch.setattr(diskspace, "same_volume", lambda a, b: volume_root(a) == volume_root(b))
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: free[volume_root(path)])
    return free


# ---------------------------------------------------------------------------
# real filesystem
# ---------------------------------------------------------------------------


def test_free_bytes_walks_up_to_existing_parent(tmp_path: Path) -> None:
    actual = shutil.disk_usage(tmp_path).free
    measured = free_bytes(str(tmp_path / "does" / "not" / "exist"))
    assert abs(measured - actual) < 512 * 1024 * 1024  # other processes may write meanwhile
    assert measured > 0


def test_free_bytes_without_existing_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diskspace, "_existing_ancestor", lambda _path: None)
    assert free_bytes(r"Q:\Games") == 0


def test_same_volume_real(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert same_volume(str(tmp_path / "a"), str(tmp_path / "b" / "c"))


def test_volume_root_real(tmp_path: Path) -> None:
    root = diskspace._volume_root(str(tmp_path / "missing" / "dir"))
    assert root.endswith(os.sep)
    assert os.path.normcase(str(tmp_path)).startswith(os.path.normcase(root))
    if os.name == "nt":
        assert root[0].isupper() and root[1:] == ":\\"


# ---------------------------------------------------------------------------
# required_bytes
# ---------------------------------------------------------------------------


def test_required_bytes_unknown_size(two_volumes: dict[str, int]) -> None:
    assert required_bytes(None, download_dir="C:\\dl", library_root="C:\\Games") == {}
    assert required_bytes(-1, download_dir="C:\\dl", library_root="C:\\Games") == {}


def test_required_bytes_same_volume_is_summed(two_volumes: dict[str, int]) -> None:
    size = 10 * GB
    expected = size + math.ceil(size * (DISK_SPACE_FACTOR - 1)) + HEADROOM
    assert required_bytes(size, download_dir="C:\\dl", library_root="C:\\Games") == {"C:\\": expected}


def test_required_bytes_different_volumes(two_volumes: dict[str, int]) -> None:
    size = 10 * GB
    assert required_bytes(size, download_dir="D:\\dl", library_root="C:\\Games") == {
        "D:\\": size + HEADROOM,
        "C:\\": math.ceil(size * (DISK_SPACE_FACTOR - 1)) + HEADROOM,
    }


def test_required_bytes_empty_download_dir_means_library(two_volumes: dict[str, int]) -> None:
    size = 1 * GB
    assert list(required_bytes(size, download_dir="", library_root="D:\\Games")) == ["D:\\"]


def test_required_bytes_zero_size(two_volumes: dict[str, int]) -> None:
    assert required_bytes(0, download_dir="C:\\dl", library_root="C:\\Games") == {"C:\\": HEADROOM}


def test_required_bytes_real_same_volume(tmp_path: Path) -> None:
    needs = required_bytes(GB, download_dir=str(tmp_path / "dl"), library_root=str(tmp_path / "lib"))
    assert len(needs) == 1
    assert next(iter(needs.values())) == GB + math.ceil(GB * (DISK_SPACE_FACTOR - 1)) + HEADROOM


# ---------------------------------------------------------------------------
# ensure_space
# ---------------------------------------------------------------------------


def test_ensure_space_ok(two_volumes: dict[str, int]) -> None:
    ensure_space(10 * GB, download_dir="C:\\dl", library_root="C:\\Games")
    ensure_space(None, download_dir="C:\\dl", library_root="C:\\Games")


def test_ensure_space_unknown_size_never_raises(two_volumes: dict[str, int]) -> None:
    two_volumes["C:\\"] = 0
    ensure_space(None, download_dir="C:\\dl", library_root="C:\\Games")


def test_ensure_space_raises_with_details(two_volumes: dict[str, int]) -> None:
    size = 10 * GB
    need = size + math.ceil(size * (DISK_SPACE_FACTOR - 1)) + HEADROOM
    two_volumes["C:\\"] = need - 1
    with pytest.raises(DiskSpaceError) as info:
        ensure_space(size, download_dir="C:\\dl", library_root="C:\\Games")
    assert info.value.required == need
    assert info.value.available == need - 1
    assert info.value.path == "C:\\"
    assert "C:\\" in info.value.user_message


def test_ensure_space_credits_partial_download(two_volumes: dict[str, int]) -> None:
    size = 10 * GB
    need = size + math.ceil(size * (DISK_SPACE_FACTOR - 1)) + HEADROOM
    two_volumes["C:\\"] = need - 4 * GB
    with pytest.raises(DiskSpaceError):
        ensure_space(size, download_dir="C:\\dl", library_root="C:\\Games", already_downloaded=3 * GB)
    ensure_space(size, download_dir="C:\\dl", library_root="C:\\Games", already_downloaded=4 * GB)


def test_ensure_space_credit_is_clamped_to_archive_size(two_volumes: dict[str, int]) -> None:
    size = 1 * GB
    extract = math.ceil(size * (DISK_SPACE_FACTOR - 1))
    two_volumes["C:\\"] = extract + HEADROOM - 1  # even a fully downloaded archive leaves this short
    with pytest.raises(DiskSpaceError) as info:
        ensure_space(size, download_dir="C:\\dl", library_root="C:\\Games", already_downloaded=50 * GB)
    assert info.value.required == extract + HEADROOM


def test_ensure_space_credit_only_on_download_volume(two_volumes: dict[str, int]) -> None:
    size = 10 * GB
    extract_need = math.ceil(size * (DISK_SPACE_FACTOR - 1)) + HEADROOM
    two_volumes["C:\\"] = extract_need - 1  # library volume short
    with pytest.raises(DiskSpaceError) as info:
        ensure_space(size, download_dir="D:\\dl", library_root="C:\\Games", already_downloaded=size)
    assert info.value.path == "C:\\"
    assert info.value.required == extract_need

    two_volumes["C:\\"] = 100 * GB
    two_volumes["D:\\"] = HEADROOM + 1 * GB  # 9 GB of the archive already there
    ensure_space(size, download_dir="D:\\dl", library_root="C:\\Games", already_downloaded=9 * GB)
    with pytest.raises(DiskSpaceError) as info:
        ensure_space(size, download_dir="D:\\dl", library_root="C:\\Games", already_downloaded=8 * GB)
    assert info.value.path == "D:\\"
