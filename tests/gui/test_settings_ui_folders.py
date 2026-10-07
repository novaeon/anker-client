"""Library/download folder validation (``ui.widgets.settings_folders``) — pure, blocking helpers."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from anker_client.ui.widgets import settings_folders as folders
from anker_client.ui.widgets.settings_folders import (
    FolderCheck,
    check_download_folder,
    check_library_folder,
    overlapping_folder,
)


def _within(seconds: float, fn, *args, **kwargs) -> FolderCheck:
    """Run a check on a thread and fail (instead of hanging the suite) when it does not return."""
    box: list[FolderCheck] = []
    worker = threading.Thread(target=lambda: box.append(fn(*args, **kwargs)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, f"{fn.__name__} did not return within {seconds} s"
    return box[0]


@pytest.fixture
def denied_dir(tmp_path: Path) -> Iterator[Path]:
    """A folder the current user may not create files in (an ACL deny entry; os.access ignores ACLs)."""
    if os.name != "nt":
        pytest.skip("Windows ACL test")
    target = tmp_path / "locked"
    target.mkdir()
    user = os.environ.get("USERNAME", "")
    result = subprocess.run(["icacls", str(target), "/deny", f"{user}:(OI)(CI)(W,AD)"],
                            capture_output=True, text=True, timeout=30, check=False)
    if not user or result.returncode != 0:
        pytest.skip(f"icacls could not deny write access: {result.stdout} {result.stderr}")
    try:
        yield target
    finally:
        subprocess.run(["icacls", str(target), "/remove:d", user], capture_output=True, timeout=30, check=False)


def test_unwritable_folder_is_refused_although_os_access_says_writable(denied_dir: Path) -> None:
    assert os.access(denied_dir, os.W_OK)  # the reason os.access cannot be used on Windows
    for check in (check_library_folder, check_download_folder):
        result = _within(10, check, str(denied_dir / "Games"))
        assert not result.ok and "cannot write" in result.message


def test_write_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    assert folders._is_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_valid_new_library_folder(tmp_path: Path) -> None:
    result = check_library_folder(str(tmp_path / "Games"))
    assert result.ok and result.path == str(tmp_path / "Games") and not result.exists
    assert "free on" in result.message and "will be created" in result.message
    assert result.free_bytes is not None and result.free_bytes > 0


@pytest.mark.parametrize(("text", "fragment"), [
    ("", "Choose a folder"),
    ("relative\\Games", "full path"),
    ("\\Games", "full path"),
])
def test_library_folder_needs_a_full_path(text: str, fragment: str) -> None:
    result = check_library_folder(text)
    assert not result.ok and fragment in result.message


def test_library_folder_refuses_drive_roots_and_home(tmp_path: Path) -> None:
    for path in (Path(tmp_path.anchor), Path.home(), Path.home() / "Downloads"):
        result = check_library_folder(str(path))
        assert not result.ok and "dedicated folder" in result.message, path


def test_library_folder_refuses_a_file(tmp_path: Path) -> None:
    file = tmp_path / "game.zip"
    file.write_bytes(b"x")
    assert "is a file" in check_library_folder(str(file)).message


def test_library_folder_refuses_overlapping_roots(tmp_path: Path) -> None:
    root = tmp_path / "Games"
    assert "already one of" in check_library_folder(str(root), existing=[str(root)]).message
    assert "overlaps" in check_library_folder(str(root / "Sub"), existing=[str(root)]).message
    assert "overlaps" in check_library_folder(str(tmp_path), existing=[str(root)]).message
    assert check_library_folder(str(tmp_path / "Other"), existing=[str(root)]).ok


def test_overlapping_folder_helper(tmp_path: Path) -> None:
    root = str(tmp_path / "Games")
    assert overlapping_folder(str(tmp_path / "Games" / "A"), ["", root]) == root
    assert overlapping_folder(str(tmp_path), [root]) == root
    assert overlapping_folder(str(tmp_path / "GamesToo"), [root]) is None  # a prefix is not a parent


def test_protected_system_folders_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = tmp_path / "Windows"
    windows.mkdir()
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    assert "administrator" in check_library_folder(str(windows / "Games")).message
    assert "administrator" in check_download_folder(str(windows / "Temp")).message


def test_download_folder_may_be_a_personal_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert not check_library_folder(str(downloads)).ok  # too risky as a library root
    result = check_download_folder(str(downloads))
    assert result.ok and result.exists and result.path == str(downloads)


def test_missing_drive_is_reported() -> None:
    if os.name != "nt":
        pytest.skip("drive letters are a Windows concept")
    free = next((f"{c}:\\" for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\")), None)
    if free is None:
        pytest.skip("no unused drive letter")
    result = check_library_folder(free + "Games")
    assert not result.ok and "not available" in result.message


def test_folder_free_space(tmp_path: Path) -> None:
    assert (folders.folder_free_space(str(tmp_path / "not" / "yet")) or 0) > 0
