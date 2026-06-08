# tests/test_installer.py
import os
import tempfile
from pathlib import Path
import anker_client.core.installer as installer
from anker_client.core.installer import find_game_exe
from anker_client.core.paths import sanitize_windows_name

def _make_dir(tmp_path, files):
    """Create a temp directory with given (filename, size_bytes) tuples."""
    for name, size in files:
        p = Path(tmp_path) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return str(tmp_path)

def test_finds_exe_by_name_match(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("Iron Lung.exe", 200_000),
        ("UnityCrashHandler64.exe", 50_000),
        ("UnityPlayer.dll", 900_000),
    ])
    result = find_game_exe(game_dir, "Iron Lung")
    assert result == str(Path(game_dir) / "Iron Lung.exe")

def test_finds_exe_by_unity_companion_folder(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("MyGame.exe", 200_000),
        ("UnityCrashHandler64.exe", 50_000),
    ])
    (Path(tmp_path) / "MyGame_Data").mkdir()
    result = find_game_exe(game_dir, "Some Other Title")
    assert result == str(Path(game_dir) / "MyGame.exe")

def test_finds_exe_by_godot_pck(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("MyGame.exe", 200_000),
        ("SomeTool.exe", 10_000),
        ("MyGame.pck", 500_000),
    ])
    result = find_game_exe(game_dir, "Some Title")
    assert result == str(Path(game_dir) / "MyGame.exe")

def test_finds_exe_by_unreal_shipping(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("MyGame-Win64-Shipping.exe", 200_000),
        ("vcredist_x64.exe", 10_000),
    ])
    result = find_game_exe(game_dir, "Some Title")
    assert result == str(Path(game_dir) / "MyGame-Win64-Shipping.exe")

def test_blocklist_removes_utilities(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("ActualGame.exe", 200_000),
        ("vcredist_x64.exe", 10_000),
        ("UnityCrashHandler64.exe", 50_000),
        ("dxsetup.exe", 5_000),
    ])
    (Path(tmp_path) / "ActualGame_Data").mkdir()
    result = find_game_exe(game_dir, "Something Else")
    assert result == str(Path(game_dir) / "ActualGame.exe")

def test_returns_none_when_ambiguous(tmp_path):
    game_dir = _make_dir(tmp_path, [
        ("GameA.exe", 200_000),
        ("GameB.exe", 200_000),
    ])
    result = find_game_exe(game_dir, "Unknown")
    assert result is None  # Caller must show picker


def test_sanitize_windows_name_removes_invalid_path_chars():
    title = "Librarian: Tidy Up the Arcane Library!"
    assert sanitize_windows_name(title) == "Librarian Tidy Up the Arcane Library!"


def test_sanitize_windows_name_avoids_reserved_device_names():
    assert sanitize_windows_name("CON") == "_CON"


def test_install_game_sanitizes_temp_and_final_dirs(tmp_path, monkeypatch):
    def fake_extract_archive(_archive_path: str, dest_dir: str) -> None:
        assert ":" not in Path(dest_dir).name
        game_root = Path(dest_dir) / "Payload"
        game_root.mkdir(parents=True)
        (game_root / "Librarian.exe").write_bytes(b"x")

    monkeypatch.setattr(installer, "extract_archive", fake_extract_archive)

    result = installer.install_game(
        str(tmp_path / "archive.zip"),
        "Librarian: Tidy Up the Arcane Library!",
        str(tmp_path),
    )

    expected = tmp_path / "Librarian Tidy Up the Arcane Library!"
    assert Path(result) == expected
    assert expected.is_dir()
    assert not (tmp_path / "_temp").exists()
