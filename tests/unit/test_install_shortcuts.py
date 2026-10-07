"""Tests for .lnk shortcut creation/removal (real COM / PowerShell, redirected into tmp_path)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from anker_client.constants import START_MENU_FOLDER
from anker_client.core import paths
from anker_client.core.errors import InstallError
from anker_client.services.install import shortcuts as shortcuts_module
from anker_client.services.install.shortcuts import ShortcutService

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows shortcuts")

if os.name == "nt":
    import pythoncom
    import win32com.client


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    desktop = tmp_path / "Desktop"
    programs = tmp_path / "Programs"
    desktop.mkdir()
    programs.mkdir()
    monkeypatch.setattr(paths, "desktop_dir", lambda: desktop)
    monkeypatch.setattr(paths, "start_menu_programs_dir", lambda: programs)
    return desktop, programs


@pytest.fixture
def game_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "Games" / "Hollow Knight" / "hollow_knight.exe"
    exe.parent.mkdir(parents=True)
    # Not "MZ…": the shell refuses to save links to malformed PE images, while non-PE files are fine.
    exe.write_bytes(b"\0" * 4096)
    return exe


def read_link(path: Path) -> dict[str, str]:
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortcut(str(path))
        result = {
            "target": link.TargetPath,
            "arguments": link.Arguments,
            "workdir": link.WorkingDirectory,
            "icon": link.IconLocation,
            "description": link.Description,
        }
        link = shell = None
        return result
    finally:
        pythoncom.CoUninitialize()


def test_create_both_with_pywin32(dirs: tuple[Path, Path], game_exe: Path) -> None:
    desktop, programs = dirs
    written = ShortcutService().create("Hollow Knight", str(game_exe), arguments='-windowed "x y"')
    expected = [desktop / "Hollow Knight.lnk", programs / START_MENU_FOLDER / "Hollow Knight.lnk"]
    assert written == [str(path) for path in expected]
    for path in expected:
        info = read_link(path)
        assert os.path.normcase(info["target"]) == os.path.normcase(str(game_exe))
        assert info["arguments"] == '-windowed "x y"'
        assert os.path.normcase(info["workdir"]) == os.path.normcase(str(game_exe.parent))
        assert os.path.normcase(info["icon"]) == os.path.normcase(f"{game_exe},0")
        assert "Hollow Knight" in info["description"]


def test_create_respects_flags(dirs: tuple[Path, Path], game_exe: Path) -> None:
    desktop, programs = dirs
    service = ShortcutService()
    assert service.create("Game", str(game_exe), desktop=True, start_menu=False) == [str(desktop / "Game.lnk")]
    assert not (programs / START_MENU_FOLDER).exists()
    assert service.create("Other", str(game_exe), desktop=False, start_menu=True) == [
        str(programs / START_MENU_FOLDER / "Other.lnk")
    ]
    assert service.create("None", str(game_exe), desktop=False, start_menu=False) == []


def test_title_is_sanitized(dirs: tuple[Path, Path], game_exe: Path) -> None:
    desktop, _ = dirs
    written = ShortcutService().create('Half-Life: Alyx? <VR> "Edition"', str(game_exe), start_menu=False)
    assert written == [str(desktop / "Half-Life Alyx VR Edition.lnk")]
    assert Path(written[0]).exists()


def test_module_level_patching_also_works(tmp_path: Path, game_exe: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = tmp_path / "OtherDesktop"
    programs = tmp_path / "OtherPrograms"
    monkeypatch.setattr(shortcuts_module, "desktop_dir", lambda: desktop)
    monkeypatch.setattr(shortcuts_module, "start_menu_programs_dir", lambda: programs)
    written = ShortcutService().create("Game", str(game_exe))
    assert written == [str(desktop / "Game.lnk"), str(programs / START_MENU_FOLDER / "Game.lnk")]


def test_missing_target_raises(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    desktop, _ = dirs
    with pytest.raises(InstallError):
        ShortcutService().create("Game", str(tmp_path / "missing.exe"))
    assert list(desktop.iterdir()) == []


def test_partial_failure_raises_install_error(dirs: tuple[Path, Path], game_exe: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    desktop, _ = dirs
    real_write = shortcuts_module._write_shortcut

    def flaky(link: Path, fields: dict[str, str]) -> None:
        if START_MENU_FOLDER in str(link):
            raise OSError("Unable to save shortcut")
        real_write(link, fields)

    monkeypatch.setattr(shortcuts_module, "_write_shortcut", flaky)
    with pytest.raises(InstallError) as info:
        ShortcutService().create("Game", str(game_exe))
    assert "Unable to save shortcut" in info.value.detail
    assert (desktop / "Game.lnk").exists()  # the other location was still written


def test_com_save_failure_is_readable(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.exe"
    bogus.write_bytes(b"MZ" + b"\x01" * 500)  # malformed PE image: WScript refuses to save
    with pytest.raises(InstallError) as info:
        ShortcutService().create("Bogus", str(bogus), start_menu=False)
    assert "Unable to save shortcut" in info.value.detail


def test_remove_deletes_new_and_legacy_locations(dirs: tuple[Path, Path], game_exe: Path) -> None:
    desktop, programs = dirs
    service = ShortcutService()
    service.create("Hollow Knight", str(game_exe))
    legacy = programs / "Hollow Knight.lnk"
    legacy.write_bytes(b"legacy")
    assert service.exists("Hollow Knight") == {"desktop": True, "start_menu": True}

    service.remove("Hollow Knight")
    assert not (desktop / "Hollow Knight.lnk").exists()
    assert not (programs / START_MENU_FOLDER / "Hollow Knight.lnk").exists()
    assert not legacy.exists()
    assert not (programs / START_MENU_FOLDER).exists(), "empty folder is removed"
    assert service.exists("Hollow Knight") == {"desktop": False, "start_menu": False}


def test_remove_keeps_folder_with_other_games(dirs: tuple[Path, Path], game_exe: Path) -> None:
    _, programs = dirs
    service = ShortcutService()
    service.create("A", str(game_exe), desktop=False)
    service.create("B", str(game_exe), desktop=False)
    service.remove("A")
    assert (programs / START_MENU_FOLDER / "B.lnk").exists()


def test_remove_missing_is_silent(dirs: tuple[Path, Path]) -> None:
    ShortcutService().remove("Never Installed")


def test_exists_counts_legacy_start_menu(dirs: tuple[Path, Path]) -> None:
    _, programs = dirs
    (programs / "Old Game.lnk").write_bytes(b"x")
    assert ShortcutService().exists("Old Game") == {"desktop": False, "start_menu": True}


def test_powershell_fallback_when_pywin32_missing(dirs: tuple[Path, Path], game_exe: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    desktop, _ = dirs
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    monkeypatch.setitem(sys.modules, "win32com.client", None)
    assert not shortcuts_module._pywin32_available()
    written = ShortcutService().create("Fallback 'Game' $x", str(game_exe), arguments="-a \"b\"", start_menu=False)
    assert written == [str(desktop / "Fallback 'Game' $x.lnk")]
    monkeypatch.undo()
    info = read_link(desktop / "Fallback 'Game' $x.lnk")
    assert os.path.normcase(info["target"]) == os.path.normcase(str(game_exe))
    assert info["arguments"] == '-a "b"'


def test_powershell_failure_raises(dirs: tuple[Path, Path], game_exe: Path, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts_module, "_pywin32_available", lambda: False)
    monkeypatch.setattr(shortcuts_module, "_powershell_exe", lambda: str(tmp_path / "no-powershell.exe"))
    with pytest.raises(InstallError):
        ShortcutService().create("Game", str(game_exe), start_menu=False)
