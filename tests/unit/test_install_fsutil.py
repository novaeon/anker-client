"""Tests for the Windows-tolerant filesystem helpers of the install package."""

from __future__ import annotations

import errno
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.errors import InstallError
from anker_client.services.install import _fsutil
from anker_client.services.install._fsutil import (
    long_path,
    move_file,
    remove_file,
    remove_tree,
    rename_with_retry,
    try_remove_tree,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(_fsutil.time, "sleep", sleeps.append)
    return sleeps


def test_long_path_formats(tmp_path: Path) -> None:
    if os.name != "nt":
        assert long_path(str(tmp_path)) == str(tmp_path)
        return
    assert long_path(r"C:\Games\x") == "\\\\?\\C:\\Games\\x"
    assert long_path("\\\\?\\C:\\Games") == "\\\\?\\C:\\Games"
    assert long_path(r"\\server\share\Games") == "\\\\?\\UNC\\server\\share\\Games"


def test_remove_tree_deletes_read_only_content(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    target = root / "Game"
    (target / "sub").mkdir(parents=True)
    for path in (target / "a.txt", target / "sub" / "b.txt"):
        path.write_text("x")
        os.chmod(path, stat.S_IREAD)
    os.chmod(target / "sub", stat.S_IREAD)
    remove_tree(str(target), within=str(root))
    assert not target.exists()
    assert root.exists()


def test_remove_tree_long_paths(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    deep = root / "Game"
    for index in range(12):
        deep = deep / f"nested_directory_level_{index:02d}"
    os.makedirs(long_path(str(deep)))
    with open(long_path(str(deep / "file.bin")), "wb") as handle:
        handle.write(b"x")
    assert len(str(deep)) > 260
    remove_tree(str(root / "Game"), within=str(root))
    assert not (root / "Game").exists()


@pytest.mark.parametrize("relation", ["outside", "root_itself", "sibling_prefix"])
def test_remove_tree_refuses_outside_root(tmp_path: Path, relation: str) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    target = {
        "outside": tmp_path / "elsewhere",
        "root_itself": root,
        "sibling_prefix": tmp_path / "lib-other",  # shares the textual prefix "lib"
    }[relation]
    target.mkdir(exist_ok=True)
    (target / "keep.txt").write_text("keep")
    with pytest.raises(InstallError, match="refused"):
        remove_tree(str(target), within=str(root))
    assert (target / "keep.txt").exists()


def test_remove_tree_refuses_dangerous_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Never point this test at a real protected folder: simulate one inside tmp_path.
    protected = tmp_path / "lib" / "Desktop"
    protected.mkdir(parents=True)
    (protected / "keep.txt").write_text("keep")
    monkeypatch.setattr(_fsutil, "is_dangerous_delete_target", lambda path: Path(path) == protected)
    with pytest.raises(InstallError, match="refused"):
        remove_tree(str(protected), within=str(tmp_path / "lib"))
    assert (protected / "keep.txt").exists()


def test_dangerous_target_check_is_wired_to_core() -> None:
    from anker_client.core.paths import is_dangerous_delete_target

    assert _fsutil.is_dangerous_delete_target is is_dangerous_delete_target
    assert is_dangerous_delete_target(str(Path.home() / "Desktop"))


def test_remove_tree_missing_is_noop(tmp_path: Path) -> None:
    remove_tree(str(tmp_path / "nothing"), within=str(tmp_path))


def test_remove_tree_waits_for_locked_file(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    target = root / "Game"
    target.mkdir(parents=True)
    locked = target / "locked.bin"
    handle = open(locked, "wb")  # an open handle blocks deletion on Windows  # noqa: SIM115
    (target / "other.bin").write_bytes(b"x")
    releaser = threading.Timer(0.1, handle.close)  # released before the first retry
    releaser.start()
    try:
        remove_tree(str(target), within=str(root))
    finally:
        releaser.cancel()
        handle.close()
    assert not target.exists()


def test_remove_tree_gives_up_after_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            no_sleep: list[float]) -> None:
    root = tmp_path / "lib"
    (root / "Game").mkdir(parents=True)

    def always_locked(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(_fsutil.shutil, "rmtree", always_locked)
    with pytest.raises(PermissionError):
        remove_tree(str(root / "Game"), within=str(root))
    assert len(no_sleep) == 4  # 5 attempts
    assert try_remove_tree(str(root / "Game"), within=str(root)) is False


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_remove_tree_does_not_follow_junctions(tmp_path: Path) -> None:
    import subprocess

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    root = tmp_path / "lib"
    (root / "Game").mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "Game" / "link"), str(outside)], check=True,
                   capture_output=True)
    remove_tree(str(root / "Game"), within=str(root))
    assert (outside / "precious.txt").read_text() == "keep"
    assert not (root / "Game").exists()


def test_rename_with_retry_retries_permission_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     no_sleep: list[float]) -> None:
    src, dst = tmp_path / "a", tmp_path / "b"
    src.mkdir()
    real_replace = os.replace
    calls: list[int] = []

    def flaky(a: Any, b: Any) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(errno.EACCES, "sharing violation")
        real_replace(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    rename_with_retry(str(src), str(dst))
    assert dst.is_dir() and len(calls) == 3 and len(no_sleep) == 2


def test_rename_with_retry_does_not_retry_other_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       no_sleep: list[float]) -> None:
    calls: list[int] = []

    def broken(_a: Any, _b: Any) -> None:
        calls.append(1)
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        rename_with_retry(str(tmp_path / "a"), str(tmp_path / "b"))
    assert calls == [1] and no_sleep == []


def test_move_file_cross_device_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"payload")

    def cross_device(_a: Any, _b: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", cross_device)
    move_file(str(src), str(dst))
    assert dst.read_bytes() == b"payload"
    assert not src.exists()


def test_remove_file(tmp_path: Path) -> None:
    path = tmp_path / "ro.txt"
    path.write_text("x")
    os.chmod(path, stat.S_IREAD)
    remove_file(str(path))
    assert not path.exists()
    remove_file(str(path))  # missing is fine


@pytest.mark.skipif(os.name != "nt", reason="hidden attribute is Windows-only")
def test_hidden_attribute(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{}")
    assert not _fsutil.is_hidden(str(path))
    _fsutil.set_hidden(str(path))
    assert _fsutil.is_hidden(str(path))
    _fsutil.set_hidden(str(tmp_path / "missing"))  # best effort, no error
