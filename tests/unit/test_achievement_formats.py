"""Unlock-file parsers, game-folder detection and the API-name prettifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anker_client.core.models import Achievement, prettify_api_name
from anker_client.services.achievements import formats, sources
from anker_client.services.achievements.formats import UnlockState

# --- parsers -------------------------------------------------------------------------------


def test_goldberg_json_reads_earned_entries_and_progress() -> None:
    data = json.dumps({
        "STEAM_A1": {"earned": True, "earned_time": 1780897608},
        "STEAM_A2": {"earned": False, "earned_time": 0},
        "STEAM_A3": {"earned": False, "earned_time": 0, "progress": 7, "max_progress": 20},
        "broken": "not an object",
    }).encode()
    states = formats.parse_goldberg_json(data)
    assert states["STEAM_A1"] == UnlockState(True, 1780897608)
    assert states["STEAM_A2"] == UnlockState(False, 0)
    assert states["STEAM_A3"] == UnlockState(False, 0, 7, 20)
    assert "broken" not in states


def test_codex_ini_reads_rune_files() -> None:
    text = (
        "[SteamAchievements]\nCount=2\n00000=ACH_10_SOUP\n\n"
        "[ACH_10_SOUP]\nAchieved=1\nCurProgress=10\nMaxProgress=10\nUnlockTime=1788291593\n\n"
        "[ACH_10_WATER]\nAchieved=0\nCurProgress=9\nMaxProgress=10\nUnlockTime=0\n"
    )
    states = formats.parse_codex_ini(text.encode())
    assert set(states) == {"ACH_10_SOUP", "ACH_10_WATER"}  # the index section is not an achievement
    assert states["ACH_10_SOUP"] == UnlockState(True, 1788291593, 10, 10)
    assert states["ACH_10_WATER"] == UnlockState(False, 0, 9, 10)


def test_onlinefix_ini_keeps_case_and_text_names() -> None:
    text = ("[BuildHouse]\nachieved=true\ntimestamp=1791319843\n\n\n"
            "[Attempt to throw something in Overcooked!]\nachieved=false\ntimestamp=0\n")
    states = formats.parse_onlinefix_ini(text.encode())
    assert states["BuildHouse"] == UnlockState(True, 1791319843)
    assert states["Attempt to throw something in Overcooked!"] == UnlockState(False, 0)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1252"])
def test_text_encodings_are_decoded(encoding: str) -> None:
    text = "[Café_Ünlock]\nAchieved=1\nUnlockTime=5\n"
    states = formats.parse_codex_ini(text.encode(encoding))
    assert states == {"Café_Ünlock": UnlockState(True, 5)}


def test_half_written_json_raises_so_the_watcher_retries() -> None:
    with pytest.raises(ValueError):
        formats.parse_goldberg_json(b'{"STEAM_A1": {"earned": tr')
    with pytest.raises(ValueError):
        formats.parse_goldberg_json(b"[1, 2]")


def test_ini_parser_tolerates_duplicates_comments_and_odd_names() -> None:
    sections = formats.parse_ini("; comment\nkey=top\n[A b%c]\nX=1\n# also comment\n[A b%c]\nx=2\n")
    assert sections[""] == {"key": "top"}
    assert sections["A b%c"] == {"x": "2"}


def test_goldberg_schema_reads_localised_texts() -> None:
    data = json.dumps([
        {"name": "boss", "displayName": {"english": "Boss", "german": "Chef"}, "description": "Beat it",
         "hidden": "1", "icon": "", "icongray": ""},
        {"name": "plain", "displayName": "Plain", "description": {"french": "Simple"}, "hidden": 0},
        {"displayName": "no api name"},
    ]).encode()
    defs = formats.parse_goldberg_schema(data, "german")
    assert [(a.api_name, a.name, a.description, a.hidden) for a in defs] == [
        ("boss", "Chef", "Beat it", True), ("plain", "Plain", "Simple", False)]


@pytest.mark.parametrize(("api_name", "expected"), [
    ("ACH_WIN_10_GAMES", "Win 10 Games"),
    ("buildHouse", "Build House"),
    ("unlockDLCItems", "Unlock DLC Items"),
    ("NEW_ACHIEVEMENT_1_12", "New Achievement 1 12"),
    ("Attempt to throw something in Overcooked!", "Attempt to throw something in Overcooked!"),
])
def test_prettify_api_name(api_name: str, expected: str) -> None:
    assert prettify_api_name(api_name) == expected
    assert Achievement(api_name=api_name).display_name == expected


def test_rare_threshold_matches_steam() -> None:
    assert Achievement(api_name="a", global_percent=9.9).rare
    assert not Achievement(api_name="a", global_percent=10.0).rare
    assert not Achievement(api_name="a").rare


# --- detection -----------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_detects_rune_from_steam_emu_ini(tmp_path: Path) -> None:
    game = tmp_path / "Dressmaker"
    _write(game / "Dressmaker_Data" / "Plugins" / "x86_64" / "steam_emu.ini",
           "###      =-RUNE- ==\n### Game data is stored at %SystemDrive%\\Users\\Public\\Documents\\Steam\\RUNE\\4019220\n"
           "[Settings]\nAppId=4019220\nUserName=Someone\n")
    info = sources.detect_game(str(game))
    assert info is not None
    assert (info.app_id, info.emulators) == (4019220, ("RUNE",))


def test_detects_codex_when_the_banner_is_not_rune(tmp_path: Path) -> None:
    _write(tmp_path / "g" / "steam_emu.ini", "### CODEX\n[Settings]\nAppId=12345\n")
    info = sources.detect_game(str(tmp_path / "g"))
    assert info is not None and info.emulators == ("CODEX",)


def test_onlinefix_real_app_id_wins_over_spacewar(tmp_path: Path) -> None:
    game = tmp_path / "Valheim"
    _write(game / "OnlineFix.ini", "[Main]\nRealAppId=892970\nFakeAppId=480\n")
    _write(game / "steam_appid.txt", "480")
    info = sources.detect_game(str(game))
    assert info is not None and (info.app_id, info.emulators) == (892970, ("OnlineFix",))


def test_gbe_fork_deep_in_an_unreal_game_with_its_schema(tmp_path: Path) -> None:
    settings = tmp_path / "Manor Lords" / "Engine" / "Binaries" / "ThirdParty" / "Steamworks" / "Steamv157" / "Win64" \
        / "steam_settings"
    _write(settings / "steam_appid.txt", "1363080\n")
    _write(settings / "achievements.json", "[]")
    info = sources.detect_game(str(tmp_path / "Manor Lords"))
    assert info is not None
    assert (info.app_id, info.emulators) == (1363080, ("Goldberg (gbe_fork)",))
    assert info.schema_file.endswith("achievements.json")


def test_gbe_custom_save_locations(tmp_path: Path) -> None:
    game = tmp_path / "Game"
    settings = game / "bin" / "steam_settings"
    _write(settings / "steam_appid.txt", "777")
    _write(settings / "configs.user.ini", "[user::saves]\nlocal_save_path=./my saves\nsaves_folder_name=My Saves\n")
    info = sources.detect_game(str(game), {"APPDATA": str(tmp_path / "appdata")})
    assert info is not None
    paths = [Path(s.path) for s in info.extra_saves]
    assert game / "bin" / "my saves" / "777" / "achievements.json" in paths
    assert [r.root for r in info.extra_roots] == ["%APPDATA%\\My Saves"]


def test_folder_without_emulator_or_only_spacewar(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "game.exe", "")
    _write(tmp_path / "b" / "steam_appid.txt", "480")
    assert sources.detect_game(str(tmp_path / "a")) is None
    assert sources.detect_game(str(tmp_path / "b")) is None
    assert sources.detect_game(str(tmp_path / "missing")) is None


def test_save_files_in_lists_numeric_app_folders(tmp_path: Path) -> None:
    root = tmp_path / "public" / "Documents" / "Steam" / "RUNE"
    (root / "4019220").mkdir(parents=True)
    (root / "480").mkdir()
    (root / "notes").mkdir()
    location = next(loc for loc in sources.KNOWN_LOCATIONS if loc.key == "rune")
    files = sources.save_files_in(location, {"PUBLIC": str(tmp_path / "public")})
    assert [(f.app_id, f.label, Path(f.path).name) for f in files] == [(4019220, "RUNE", "achievements.ini")]
    assert sources.save_files_in(location, {}) == []  # %PUBLIC% unset: location does not apply
