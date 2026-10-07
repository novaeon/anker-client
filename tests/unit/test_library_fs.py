"""Low-level helpers in services._library_fs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from anker_client.core.errors import InstallError
from anker_client.services import _library_fs as fs

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-specific file system behaviour")


def test_containment(tmp_path: Path) -> None:
    root = tmp_path / "Games"
    assert fs.is_strictly_inside(root / "A", root)
    assert fs.is_strictly_inside(root / "A" / "b", root)
    assert not fs.is_strictly_inside(root, root)
    assert not fs.is_strictly_inside(tmp_path / "Games2", root)  # prefix, not a child
    assert not fs.is_strictly_inside(root / ".." / "x", root)
    if os.name == "nt":
        assert fs.is_strictly_inside(str(root / "A").upper(), root)
        assert fs.same_path(str(root).lower(), str(root) + "\\")


@windows_only
def test_safely_inside_rejects_links_out_of_the_tree(tmp_path: Path) -> None:
    import _winapi

    root = tmp_path / "Games"
    outside = tmp_path / "Outside"
    root.mkdir()
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "Link"))
    assert fs.is_strictly_inside(root / "Link", root)
    assert not fs.is_safely_inside(root / "Link", root)
    assert fs.is_link(root / "Link")
    assert not fs.is_link(outside)


def test_delete_tree_counts_and_keep_last(tmp_path: Path) -> None:
    top = tmp_path / "game"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "f1").write_bytes(b"1")
    (top / "f2").write_bytes(b"2")
    (top / "keep.json").write_bytes(b"{}")
    progress = fs.delete_tree(str(top), keep_last="keep.json", retry_delay=0.0)
    assert not top.exists()
    assert (progress.files, progress.dirs) == (3, 3)


@windows_only
def test_delete_tree_refuses_link_top(tmp_path: Path) -> None:
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    (target / "x").write_text("x")
    _winapi.CreateJunction(str(target), str(tmp_path / "link"))
    with pytest.raises(InstallError):
        fs.delete_tree(str(tmp_path / "link"))
    assert (target / "x").exists()


def test_retry_gives_up_on_non_transient_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tmp_path / "game"
    top.mkdir()
    (top / "f").write_text("x")
    calls: list[str] = []

    def broken_remove(path: str) -> None:
        calls.append(path)
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(fs.os, "remove", broken_remove)
    with pytest.raises(InstallError):
        fs.delete_tree(str(top), retry_delay=0.0)
    assert len(calls) == 1


def test_transient_errors_are_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tmp_path / "game"
    top.mkdir()
    (top / "f").write_text("x")
    real_remove = os.remove
    failures = {"left": 2}

    def flaky_remove(path: str) -> None:
        if failures["left"]:
            failures["left"] -= 1
            error = PermissionError(13, "in use")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        real_remove(path)

    monkeypatch.setattr(fs.os, "remove", flaky_remove)
    fs.delete_tree(str(top), retry_delay=0.0)
    assert not top.exists()


@windows_only
def test_delete_tree_removes_names_win32_would_rewrite_and_deep_paths(tmp_path: Path) -> None:
    """7-Zip extracts names ending in a dot/space and trees deeper than MAX_PATH; both must go."""
    top = tmp_path / "game"
    prefix = "\\\\?\\"
    deep = str(top) + "".join(f"\\{'d' * 40}{i}" for i in range(7))  # > 260 characters
    assert len(deep) > 260
    os.makedirs(prefix + deep)
    for name in (deep + "\\deep.bin", str(top / "trailing."), str(top / "space "), str(top / "keep.json")):
        with open(prefix + name, "wb") as handle:
            handle.write(b"x")
    os.mkdir(prefix + str(top / "folder."))
    with open(prefix + str(top / "folder.") + "\\inner.txt", "wb") as handle:
        handle.write(b"x")

    progress = fs.delete_tree(str(top), keep_last="keep.json", retry_delay=0.0)

    assert not os.path.lexists(prefix + str(top))
    assert progress.files == 5 and progress.dirs == 1 + 7 + 1


@windows_only
def test_delete_tree_names_the_file_that_failed(tmp_path: Path) -> None:
    top = tmp_path / "game"
    (top / "data").mkdir(parents=True)
    (top / "data" / "locked.pak").write_bytes(b"x")
    with open(top / "data" / "locked.pak", "rb"), pytest.raises(InstallError) as info:
        fs.delete_tree(str(top), attempts=2, retry_delay=0.0)
    assert '"data\\locked.pak"' in info.value.user_message
    assert "\\\\?\\" not in info.value.user_message


def test_extended_path() -> None:
    if os.name == "nt":
        assert fs.extended_path("C:\\Games\\X\\..\\Y") == "\\\\?\\C:\\Games\\Y"
        assert fs.extended_path("\\\\server\\share\\Games") == "\\\\?\\UNC\\server\\share\\Games"
        assert fs.extended_path("\\\\?\\C:\\Games") == "\\\\?\\C:\\Games"
    else:
        assert fs.extended_path("/games/x/../y") == "/games/y"


def test_mtime_iso(tmp_path: Path) -> None:
    os.utime(tmp_path, (0, 86400))
    assert fs.mtime_iso(tmp_path) == "1970-01-02T00:00:00+00:00"
    assert fs.mtime_iso(tmp_path / "missing") == ""


def test_long_path_passthrough(tmp_path: Path) -> None:
    assert fs.long_path(str(tmp_path)) == str(tmp_path)
