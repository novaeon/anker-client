"""Tests for main-executable detection scoring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from anker_client.services.install import executables
from anker_client.services.install.executables import _pick_best, executable_candidates, find_executable

KB = 1024
MB = 1024 * 1024


def make(root: Path, files: dict[str, int]) -> Path:
    """Create files of the given sizes (sparse: fast) and folders for names ending in '/'."""
    for name, size in files.items():
        path = root / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)
    return root


def rel(path: str) -> str:
    return path.replace("/", os.sep)


def test_unity_game(tmp_path: Path) -> None:
    make(tmp_path, {
        "hollow_knight.exe": 650 * KB,
        "hollow_knight_Data/": 0,
        "UnityCrashHandler64.exe": 1 * MB,
        "UnityPlayer.dll": 10 * MB,
        "Settings.exe": 2 * MB,
    })
    candidates = executable_candidates(str(tmp_path), "Hollow Knight")
    names = [path for path, _ in candidates]
    assert names[0] == "hollow_knight.exe"
    assert "UnityCrashHandler64.exe" not in names
    assert find_executable(str(tmp_path), "Hollow Knight") == "hollow_knight.exe"


def test_unity_data_folder_wins_without_title_match(tmp_path: Path) -> None:
    make(tmp_path, {"Game.exe": 650 * KB, "Game_Data/": 0, "Tool.exe": 650 * KB})
    assert find_executable(str(tmp_path), "Completely Different Name") == "Game.exe"


def test_unreal_prefers_root_stub(tmp_path: Path) -> None:
    make(tmp_path, {
        "Stray.exe": 260 * KB,
        "Engine/Binaries/ThirdParty/CEF3/Win64/UnrealCEFSubProcess.exe": 1 * MB,
        "Engine/Binaries/Win64/CrashReportClient.exe": 10 * MB,
        "Engine/Extras/Redist/en-us/UE4PrereqSetup_x64.exe": 10 * MB,
        "Hk_project/Binaries/Win64/Hk_project-Win64-Shipping.exe": 10 * MB,
        "Hk_project/Content/Paks/Hk_project-WindowsNoEditor.pak": 1 * MB,
    })
    candidates = executable_candidates(str(tmp_path), "Stray")
    names = [path for path, _ in candidates]
    assert names == ["Stray.exe", rel("Hk_project/Binaries/Win64/Hk_project-Win64-Shipping.exe")]
    assert find_executable(str(tmp_path), "Stray") == "Stray.exe"


def test_unreal_stub_wins_even_when_shipping_matches_title(tmp_path: Path) -> None:
    make(tmp_path, {
        "Hades.exe": 300 * KB,
        "Engine/Binaries/Win64/x.dll": 1,
        "Hades/Binaries/Win64/Hades-Win64-Shipping.exe": 10 * MB,
    })
    assert find_executable(str(tmp_path), "Hades") == "Hades.exe"


def test_unreal_without_stub_uses_shipping(tmp_path: Path) -> None:
    make(tmp_path, {
        "Engine/Binaries/Win64/CrashReportClient.exe": 10 * MB,
        "Engine/Plugins/x.dll": 1,
        "MyGame/Binaries/Win64/MyGame-Win64-Shipping.exe": 10 * MB,
        "MyGame/Binaries/Win64/MyGameServer.exe": 10 * MB,
    })
    assert find_executable(str(tmp_path), "My Game") == rel("MyGame/Binaries/Win64/MyGame-Win64-Shipping.exe")


def test_godot_pck(tmp_path: Path) -> None:
    make(tmp_path, {"Brotato.exe": 10 * MB, "Brotato.pck": 1, "Brotato_Launcher.exe": 10 * MB})
    assert find_executable(str(tmp_path), "Some Other Title") == "Brotato.exe"


def test_utilities_and_redist_excluded(tmp_path: Path) -> None:
    make(tmp_path, {
        "vc_redist.x64.exe": 10 * MB,
        "UnityCrashHandler64.exe": 1 * MB,
        "unins000.exe": 3 * MB,
        "DXSETUP.exe": 500 * KB,
        "_CommonRedist/DirectX/Jun2010/DXSETUP.exe": 500 * KB,
        "_CommonRedist/vcredist/2019/game_helper.exe": 5 * MB,
        "Redist/oalinst.exe": 1 * MB,
        "Prerequisites/thing.exe": 1 * MB,
        "__MACOSX/Game.exe": 5 * MB,
        "Support/crashpad_handler.exe": 1 * MB,
    })
    assert executable_candidates(str(tmp_path), "Game") == []
    assert find_executable(str(tmp_path), "Game") == ""


def test_ambiguous_returns_empty(tmp_path: Path) -> None:
    make(tmp_path, {"Alpha.exe": 10 * MB, "Beta.exe": 10 * MB})
    assert len(executable_candidates(str(tmp_path), "Gamma")) == 2
    assert find_executable(str(tmp_path), "Gamma") == ""


def test_lone_candidate_returned_despite_low_score(tmp_path: Path) -> None:
    make(tmp_path, {"sub/dir/launcher_config.exe": 20 * KB})
    candidates = executable_candidates(str(tmp_path), "Title")
    assert candidates[0][1] < 0
    assert find_executable(str(tmp_path), "Title") == rel("sub/dir/launcher_config.exe")


def test_title_match_beats_launcher(tmp_path: Path) -> None:
    make(tmp_path, {"HollowKnight.exe": 5 * MB, "Launcher.exe": 5 * MB})
    assert find_executable(str(tmp_path), "Hollow Knight") == "HollowKnight.exe"


def test_platform_suffixes_ignored_for_title_match(tmp_path: Path) -> None:
    make(tmp_path, {"Celeste_x64.exe": 5 * MB, "Celeste_dx12.exe": 5 * MB, "Other.exe": 5 * MB})
    names = [path for path, score in executable_candidates(str(tmp_path), "Celeste") if score >= 50]
    assert sorted(names) == ["Celeste_dx12.exe", "Celeste_x64.exe"]


def test_depth_limit(tmp_path: Path) -> None:
    make(tmp_path, {"a/b/c/Game.exe": 5 * MB, "a/b/c/d/Deep.exe": 5 * MB})
    names = [path for path, _ in executable_candidates(str(tmp_path), "Game")]
    assert names == [rel("a/b/c/Game.exe")]


def test_bin_dir_bonus_and_depth_bonus(tmp_path: Path) -> None:
    make(tmp_path, {"bin/x64/Game.exe": 5 * MB, "tools/x/Game.exe": 5 * MB})
    candidates = dict(executable_candidates(str(tmp_path), "Unrelated"))
    assert candidates[rel("bin/x64/Game.exe")] == candidates[rel("tools/x/Game.exe")] + 10


def test_small_file_penalty(tmp_path: Path) -> None:
    make(tmp_path, {"Game.exe": 50 * KB, "Real.exe": 10 * MB})
    candidates = dict(executable_candidates(str(tmp_path), "Unrelated"))
    assert candidates["Real.exe"] - candidates["Game.exe"] == 25  # -20 small vs +5 large


def test_paths_are_relative_with_os_separators(tmp_path: Path) -> None:
    make(tmp_path, {"Game/bin/Game.exe": 5 * MB})
    (path, _score), = executable_candidates(str(tmp_path), "Game")
    assert path == os.path.join("Game", "bin", "Game.exe")
    assert not os.path.isabs(path)


def test_missing_directory(tmp_path: Path) -> None:
    assert executable_candidates(str(tmp_path / "missing"), "Game") == []
    assert find_executable(str(tmp_path / "missing"), "Game") == ""
    assert find_executable("", "Game") == ""


def test_deterministic_order_for_ties(tmp_path: Path) -> None:
    make(tmp_path, {"b.exe": 5 * MB, "A.exe": 5 * MB, "sub/c.exe": 5 * MB})
    first = executable_candidates(str(tmp_path), "Unrelated")
    assert [path for path, _ in first] == ["A.exe", "b.exe", rel("sub/c.exe")]
    assert executable_candidates(str(tmp_path), "Unrelated") == first


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junctions_not_followed(tmp_path: Path) -> None:
    outside = make(tmp_path / "outside", {"Game.exe": 5 * MB})
    game = make(tmp_path / "game", {"Real.exe": 5 * MB})
    subprocess.run(["cmd", "/c", "mklink", "/J", str(game / "linked"), str(outside)], check=True,
                   capture_output=True)
    assert [path for path, _ in executable_candidates(str(game), "Game")] == ["Real.exe"]


def test_pick_best_rules() -> None:
    assert _pick_best([]) == ""
    assert _pick_best([("only.exe", -30)]) == "only.exe"
    assert _pick_best([("a.exe", 55), ("b.exe", 40)]) == "a.exe"
    assert _pick_best([("a.exe", 55), ("b.exe", 41)]) == ""
    assert _pick_best([("a.exe", 39), ("b.exe", 0)]) == ""


@pytest.mark.parametrize(
    ("stem", "title", "minimum"),
    [
        ("HollowKnight", "Hollow Knight", 50),
        ("hollow_knight", "Hollow Knight", 50),
        ("Hollow-Knight-Win64-Shipping", "Hollow Knight", 50),
        ("DOOMEternalx64vk", "DOOM Eternal", 0),
        ("Witcher3", "The Witcher 3: Wild Hunt", 30),
        ("ACValhalla", "Assassin's Creed Valhalla", 10),
    ],
)
def test_name_score(stem: str, title: str, minimum: int) -> None:
    assert executables._name_score(stem, title) >= minimum


def test_name_score_unrelated_is_zero() -> None:
    assert executables._name_score("Launcher", "Hollow Knight") == 0
    assert executables._name_score("Game", "") == 0


def test_penalty_word_that_is_part_of_the_title_is_not_penalised(tmp_path: Path) -> None:
    # ">observer_" contains "server"; "Map Editor" contains "editor".
    make(tmp_path, {"Observer.exe": 5 * MB, "Other.exe": 5 * MB})
    scores = dict(executable_candidates(str(tmp_path), ">observer_"))
    expected = executables._name_score("Observer", ">observer_") + 15 + executables._size_score(5 * MB)
    assert scores["Observer.exe"] == expected
    assert find_executable(str(tmp_path), ">observer_") == "Observer.exe"

    editor = make(tmp_path / "editor", {"MapEditor.exe": 5 * MB})
    (_path, score), = executable_candidates(str(editor), "Map Editor")
    assert score == executables._name_score("MapEditor", "Map Editor") + 15 + executables._size_score(5 * MB)


def test_penalty_still_applies_to_words_outside_the_title(tmp_path: Path) -> None:
    make(tmp_path, {"ServerTool.exe": 5 * MB})
    (_path, score), = executable_candidates(str(tmp_path), "Map Editor")
    assert score == 15 + executables._size_score(5 * MB) - 15
