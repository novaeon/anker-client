"""LibraryService: scanning, ids, DB merge, register/adopt, setters, sizes."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.errors import InstallError, LaunchError, NotFoundError, OperationCancelled
from anker_client.core.events import LibraryChanged
from anker_client.core.models import (
    DownloadKind,
    DownloadOption,
    GameSummary,
    InstallManifest,
    InstallRequest,
    InstallResult,
)
from anker_client.core.tasks import CancelToken
from tests.unit.test_library_support import (
    FakeCatalog,
    LibraryEnv,
    fake_read_manifest,
    make_env,
    make_game_dir,
    set_windows_attributes,
)

HOLLOW = GameSummary(slug="hollow-knight", title="Hollow Knight", cover_url="https://img/hk.jpg",
                     primary_genre="Metroidvania")
CELESTE = GameSummary(slug="celeste", title="Celeste", cover_url="https://img/celeste.jpg", primary_genre="Platformer")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryEnv]:
    environment = make_env(tmp_path, monkeypatch, catalog=FakeCatalog([HOLLOW, CELESTE]))
    yield environment
    environment.db.close()


def ids(env: LibraryEnv) -> list[str]:
    return sorted(g.install_id for g in env.library.games())


# --- scanning ----------------------------------------------------------------------------------


def test_scan_finds_managed_and_unmanaged(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2", version="v1.0",
                                                                  executable="portal2.exe", has_redist=True,
                                                                  cover_url="https://img/p2.jpg", genres=["Puzzle"]))
    make_game_dir(env.root, "Hollow Knight")

    games = env.library.scan()

    assert [g.install_id for g in games] == ["local:hollow knight", "portal-2"]
    portal = env.library.get("portal-2")
    assert portal is not None and portal.managed
    assert (portal.title, portal.version, portal.executable, portal.has_redist) == ("Portal 2", "v1.0",
                                                                                     "portal2.exe", True)
    assert portal.path == str(env.root / "Portal 2")
    assert portal.library_root == str(env.root)
    assert portal.genres == ["Puzzle"]
    hollow = env.library.get("local:hollow knight")
    assert hollow is not None and not hollow.managed
    assert hollow.title == "Hollow Knight"
    assert (hollow.slug, hollow.cover_url, hollow.genres) == ("hollow-knight", HOLLOW.cover_url, ["Metroidvania"])
    assert hollow.executable == "game.exe"  # detected, not persisted
    assert not (env.root / "Hollow Knight" / MANIFEST_FILENAME).exists()
    assert hollow.installed_at  # folder mtime


def test_scan_ignores_special_folders_and_files(env: LibraryEnv, tmp_path: Path) -> None:
    for name in (".ankerclient", "_temp", "$RECYCLE.BIN", "System Volume Information", ".hidden-dot",
                 "Old Game.ankerclient-old-1a2b3c"):
        make_game_dir(env.root, name)
    (env.root / "readme.txt").write_text("not a game")
    (env.root / "Empty").mkdir()
    hidden = make_game_dir(env.root, "Hidden Attr")
    nested_root = make_game_dir(env.root, "More Games")
    downloads = make_game_dir(env.root, "Downloads")
    make_game_dir(env.root, "Real Game")
    if os.name == "nt":
        set_windows_attributes(hidden, 0x2)
    env.settings.update(library_dirs=[str(env.root), str(nested_root)], download_dir=str(downloads))

    games = env.library.scan()

    names = sorted(g.folder_name for g in games)
    expected = ["Real Game"] if os.name == "nt" else ["Hidden Attr", "Real Game"]
    assert names == expected


def test_scan_skips_folders_holding_the_client(env: LibraryEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    portable = make_game_dir(env.root, "AnkerClient", files={"AnkerClient.exe": b"MZ", "_internal/x.dll": b"x"})
    make_game_dir(env.root, "Portable Data", files={"data/config/config.json": b"{}"})
    make_game_dir(env.root, "Real Game")
    monkeypatch.setattr(sys, "executable", str(portable / "AnkerClient.exe"))
    monkeypatch.setenv("ANKERCLIENT_HOME", str(env.root / "Portable Data" / "data"))

    env.library.scan()

    assert ids(env) == ["local:real game"]


def test_scan_handles_missing_and_multiple_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, root_count=2)
    try:
        make_game_dir(env.roots[0], "A")
        make_game_dir(env.roots[1], "B")
        env.settings.update(library_dirs=[str(env.roots[0]), str(tmp_path / "missing"), str(env.roots[1])])
        assert ids(env) == []
        env.library.scan()
        assert ids(env) == ["local:a", "local:b"]
        assert env.library.get("local:b").library_root == str(env.roots[1])  # type: ignore[union-attr]
    finally:
        env.db.close()


def test_unicode_folder_names(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Ünïcødé ゲーム 🎮")
    env.library.scan()
    game = env.library.get("local:ünïcødé ゲーム 🎮")
    assert game is not None and game.title == "Ünïcødé ゲーム 🎮" and game.path == str(folder)
    env.library.set_favorite(game.install_id, True)
    env.library.scan()
    assert env.library.get(game.install_id).favorite  # type: ignore[union-attr]
    env.library.uninstall(game.install_id)
    assert not folder.exists()


def test_manifest_without_slug_uses_local_id_and_is_enriched(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Celeste", manifest=InstallManifest(title="Celeste", executable="game.exe"))
    env.library.scan()
    game = env.library.get("local:celeste")
    assert game is not None and game.managed
    assert game.slug == "celeste" and game.cover_url == CELESTE.cover_url  # display enrichment only


def test_duplicate_slugs_get_folder_suffix(env: LibraryEnv) -> None:
    manifest = InstallManifest(slug="portal-2", title="Portal 2")
    make_game_dir(env.root, "Portal 2", manifest=manifest)
    make_game_dir(env.root, "Portal 2 (2)", manifest=manifest)
    env.library.scan()
    assert ids(env) == ["portal-2", "portal-2#portal 2 (2)"]
    assert env.library.get("portal-2").folder_name == "Portal 2"  # type: ignore[union-attr]


def test_duplicate_slug_prefers_folder_known_to_the_db(env: LibraryEnv) -> None:
    manifest = InstallManifest(slug="portal-2", title="Portal 2")
    make_game_dir(env.root, "A Portal", manifest=manifest)
    second = make_game_dir(env.root, "B Portal", manifest=manifest)
    env.db.execute("INSERT INTO installs(install_id, path, playtime_seconds) VALUES('portal-2', ?, 99)",
                   (str(second),))
    env.library.scan()
    primary = env.library.get("portal-2")
    assert primary is not None and primary.folder_name == "B Portal" and primary.playtime_seconds == 99
    assert env.library.get("portal-2#a portal") is not None


def test_same_folder_name_in_two_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, root_count=2)
    try:
        make_game_dir(env.roots[0], "Doom")
        make_game_dir(env.roots[1], "doom")
        env.library.scan()
        assert ids(env) == ["local:doom", "local:doom#2"]
        # Ids are stable across scans.
        env.library.scan()
        assert env.library.get("local:doom#2").library_root == str(env.roots[1])  # type: ignore[union-attr]
    finally:
        env.db.close()


def test_scan_merges_user_data_and_keeps_rows_of_vanished_games(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.db.execute(
        "INSERT INTO installs(install_id, path, favorite, hidden, playtime_seconds, last_played, size_bytes, "
        "latest_version, update_available) VALUES('portal-2', 'x', 1, 1, 3600, '2026-01-01T00:00:00+00:00', 1234, "
        "'v2', 1)"
    )
    env.library.scan()
    game = env.library.get("portal-2")
    assert game is not None
    assert (game.favorite, game.hidden, game.playtime_seconds, game.last_played, game.size_bytes,
            game.latest_version, game.update_available) == (True, True, 3600, "2026-01-01T00:00:00+00:00", 1234,
                                                            "v2", True)
    assert env.rows()["portal-2"]["path"] == str(folder)  # identity refreshed

    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    env.library.scan()
    assert env.library.get("portal-2") is None
    assert env.rows()["portal-2"]["playtime_seconds"] == 3600  # kept for a re-install

    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.library.scan()
    assert env.library.get("portal-2").playtime_seconds == 3600  # type: ignore[union-attr]


def test_games_filters_hidden_and_sorts_by_title(env: LibraryEnv) -> None:
    make_game_dir(env.root, "b-folder", manifest=InstallManifest(slug="zeta", title="zeta"))
    make_game_dir(env.root, "a-folder", manifest=InstallManifest(slug="alpha", title="Alpha"))
    make_game_dir(env.root, "c-folder", manifest=InstallManifest(slug="mid", title="Mid"))
    env.library.scan()
    env.library.set_hidden("mid", True)
    assert [g.title for g in env.library.games()] == ["Alpha", "Mid", "zeta"]
    assert [g.title for g in env.library.games(include_hidden=False)] == ["Alpha", "zeta"]


def test_games_returns_copies(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.library.scan()
    env.library.games()[0].title = "mutated"
    env.library.get("portal-2").genres.append("x")  # type: ignore[union-attr]
    game = env.library.get("portal-2")
    assert game is not None and game.title == "Portal 2" and game.genres == []


def test_scan_publishes_library_changed(env: LibraryEnv) -> None:
    env.library.scan()
    assert env.recorder.of(LibraryChanged) == [LibraryChanged()]


def test_scan_is_cancellable_and_keeps_previous_cache(env: LibraryEnv) -> None:
    make_game_dir(env.root, "A")
    env.library.scan()
    make_game_dir(env.root, "B")
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        env.library.scan(token=token)
    assert ids(env) == ["local:a"]


def test_broken_catalog_does_not_break_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, catalog=FakeCatalog(broken=True))
    try:
        make_game_dir(env.root, "Hollow Knight")
        env.library.scan()
        game = env.library.get("local:hollow knight")
        assert game is not None and game.slug == "" and game.cover_url == ""
    finally:
        env.db.close()


def test_unmanaged_executable_detection_is_cached_per_folder_mtime(env: LibraryEnv,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services.install import executables

    calls: list[str] = []

    def counting(install_dir: str, title: str) -> str:
        calls.append(install_dir)
        return "game.exe"

    monkeypatch.setattr(executables, "find_executable", counting)
    make_game_dir(env.root, "Hollow Knight")
    env.library.scan()
    env.library.scan()
    assert len(calls) == 1


# --- lookups ----------------------------------------------------------------------------------------


def test_find_by_slug_prefers_managed(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Hollow Knight")  # unmanaged, enriched with slug hollow-knight
    make_game_dir(env.root, "HK Managed", manifest=InstallManifest(slug="hollow-knight", title="Hollow Knight"))
    env.library.scan()
    found = env.library.find_by_slug("hollow-knight")
    assert found is not None and found.install_id == "hollow-knight" and found.managed
    assert env.library.find_by_slug("") is None
    assert env.library.find_by_slug("nope") is None


def test_find_by_path_exact_and_inside(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.library.scan()
    assert env.library.find_by_path(str(folder)).install_id == "portal-2"  # type: ignore[union-attr]
    assert env.library.find_by_path(str(folder).upper() + os.sep).install_id == "portal-2"  # type: ignore[union-attr]
    assert env.library.find_by_path(str(folder / "bin" / "x.exe")).install_id == "portal-2"  # type: ignore[union-attr]
    assert env.library.find_by_path(str(env.root)) is None
    assert env.library.find_by_path("") is None


def test_executable_candidates(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"),
                  files={"portal2.exe": b"x", "bin/launcher.exe": b"x"})
    env.library.scan()
    assert env.library.executable_candidates("portal-2") == ["bin\\launcher.exe" if os.name == "nt"
                                                             else "bin/launcher.exe", "portal2.exe"]
    with pytest.raises(NotFoundError):
        env.library.executable_candidates("missing")


# --- register_install ----------------------------------------------------------------------------------


def _request(env: LibraryEnv, *, kind: DownloadKind = DownloadKind.FULL, version: str = "v2") -> InstallRequest:
    option = DownloadOption(7, "Direct", kind, to_version=version)
    return InstallRequest(archive_path="a.7z", slug="portal-2", title="Portal 2", option=option,
                          library_root=str(env.root), version=version, cover_url="https://img/p2.jpg",
                          genres=["Puzzle"])


def test_register_install_adds_game_and_row(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2",
                                                                          version="v2", executable="game.exe"))
    env.library.scan()
    env.recorder.clear()
    result = InstallResult(install_path=str(folder), executable="game.exe", size_bytes=4096)

    game = env.library.register_install(result, _request(env))

    assert game.install_id == "portal-2" and game.size_bytes == 4096 and game.managed
    assert env.library.get("portal-2").size_bytes == 4096  # type: ignore[union-attr]
    row = env.rows()["portal-2"]
    assert (row["path"], row["size_bytes"], row["title"]) == (str(folder), 4096, "Portal 2")
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"portal-2"}))]


def test_register_install_writes_missing_manifest(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2")
    result = InstallResult(install_path=str(folder), executable="game.exe", has_redist=True, size_bytes=10)
    game = env.library.register_install(result, _request(env))
    manifest = fake_read_manifest(str(folder))
    assert manifest is not None
    assert (manifest.slug, manifest.version, manifest.executable, manifest.has_redist) == ("portal-2", "v2",
                                                                                          "game.exe", True)
    assert manifest.applied_options == ["Direct"] and manifest.cover_url == "https://img/p2.jpg"
    assert game.install_id == "portal-2"


def test_register_install_replaces_unmanaged_entry_and_moves_playtime(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2")
    env.library.scan()
    env.library.record_play_session("local:portal 2", "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", 3600)
    env.library.set_favorite("local:portal 2", True)
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.recorder.clear()

    game = env.library.register_install(InstallResult(install_path=str(folder)), _request(env))

    assert game.playtime_seconds == 3600 and game.favorite
    assert ids(env) == ["portal-2"]
    assert "local:portal 2" not in env.rows()
    assert [s["install_id"] for s in env.sessions()] == ["portal-2"]
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"portal-2", "local:portal 2"}))]


def test_register_install_update_flags(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2",
                                                                          version="v1"))
    env.library.scan()
    env.library.set_update_state("portal-2", latest_version="v3", available=True)

    # A patch to v2 while v3 is out: still outdated.
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2", version="v2"))
    game = env.library.register_install(InstallResult(install_path=str(folder)),
                                        _request(env, kind=DownloadKind.PATCH, version="v2"))
    assert game.update_available

    # An add-on never changes the flag.
    game = env.library.register_install(InstallResult(install_path=str(folder)),
                                        _request(env, kind=DownloadKind.ADDON))
    assert game.update_available

    # Full install of the latest version clears it.
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2", version="V 3"))
    game = env.library.register_install(InstallResult(install_path=str(folder)), _request(env, version="v3"))
    assert not game.update_available
    assert env.rows()["portal-2"]["update_available"] == 0


def test_register_install_second_copy_gets_suffixed_id(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.library.scan()
    second = make_game_dir(env.root, "Portal 2 (2)", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    game = env.library.register_install(InstallResult(install_path=str(second)), _request(env))
    assert game.install_id == "portal-2#portal 2 (2)"
    env.library.scan()
    assert ids(env) == ["portal-2", "portal-2#portal 2 (2)"]


def test_register_install_before_first_scan_does_not_take_over_another_copy(env: LibraryEnv) -> None:
    from anker_client.services.library import LibraryService

    manifest = InstallManifest(slug="portal-2", title="Portal 2")
    first = make_game_dir(env.root, "Portal 2", manifest=manifest)
    env.library.scan()
    env.library.record_play_session("portal-2", "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", 3600)
    env.library.set_favorite("portal-2", True)
    # Next start: an install finishes before the startup scan has filled the cache.
    restarted = LibraryService(env.db, env.settings, env.events, env.shortcuts, None)  # type: ignore[arg-type]
    second = make_game_dir(env.root, "Portal 2 (2)", manifest=manifest)

    game = restarted.register_install(InstallResult(install_path=str(second)), _request(env))

    assert game.install_id == "portal-2#portal 2 (2)" and game.playtime_seconds == 0 and not game.favorite
    assert env.rows()["portal-2"]["path"] == str(first)
    restarted.scan()
    original = restarted.get("portal-2")
    assert original is not None and original.path == str(first)
    assert original.playtime_seconds == 3600 and original.favorite
    assert restarted.get("portal-2#portal 2 (2)").path == str(second)  # type: ignore[union-attr]


def test_register_install_reuses_the_id_of_a_copy_that_is_gone(env: LibraryEnv) -> None:
    env.db.execute("INSERT INTO installs(install_id, path, playtime_seconds) VALUES('portal-2', ?, 500)",
                   (str(env.root / "Old Location"),))
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    game = env.library.register_install(InstallResult(install_path=str(folder)), _request(env))
    assert game.install_id == "portal-2" and game.playtime_seconds == 500


def test_register_install_keeps_a_suffixed_id_across_restarts(env: LibraryEnv) -> None:
    from anker_client.services.library import LibraryService

    manifest = InstallManifest(slug="portal-2", title="Portal 2")
    make_game_dir(env.root, "Portal 2", manifest=manifest)
    second = make_game_dir(env.root, "Portal 2 (2)", manifest=manifest)
    env.library.scan()
    env.library.record_play_session("portal-2#portal 2 (2)", "2026-01-01T00:00:00+00:00",
                                    "2026-01-01T00:10:00+00:00", 600)
    restarted = LibraryService(env.db, env.settings, env.events, env.shortcuts, None)  # type: ignore[arg-type]
    patch = _request(env, kind=DownloadKind.PATCH, version="v3")
    game = restarted.register_install(InstallResult(install_path=str(second)), patch)
    assert game.install_id == "portal-2#portal 2 (2)" and game.playtime_seconds == 600


def test_register_install_in_a_nested_library_root(env: LibraryEnv) -> None:
    nested = env.root / "More Games"
    nested.mkdir()
    env.settings.update(library_dirs=[str(env.root), str(nested)])
    folder = make_game_dir(nested, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    game = env.library.register_install(InstallResult(install_path=str(folder)), _request(env))
    assert game.library_root == str(nested)
    env.library.scan()
    assert env.library.get("portal-2").library_root == str(nested)  # type: ignore[union-attr]


def test_register_install_missing_folder(env: LibraryEnv) -> None:
    with pytest.raises(InstallError):
        env.library.register_install(InstallResult(install_path=str(env.root / "nope")), _request(env))


# --- adopt ---------------------------------------------------------------------------------------------


def test_adopt_links_unmanaged_folder(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "HK")
    os.utime(folder, (1_700_000_000, 1_700_000_000))
    env.library.scan()
    env.library.record_play_session("local:hk", "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00", 600)
    env.recorder.clear()

    game = env.library.adopt("local:hk", slug="hollow-knight")

    assert game.install_id == "hollow-knight" and game.managed and game.slug == "hollow-knight"
    assert game.title == "Hollow Knight" and game.cover_url == HOLLOW.cover_url and game.genres == ["Metroidvania"]
    assert game.playtime_seconds == 600 and game.executable == "game.exe"
    manifest = fake_read_manifest(str(folder))
    assert manifest is not None and manifest.slug == "hollow-knight" and manifest.version == ""
    assert manifest.installed_at == "2023-11-14T22:13:20+00:00"
    assert ids(env) == ["hollow-knight"]
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"local:hk", "hollow-knight"}))]
    assert [s["install_id"] for s in env.sessions()] == ["hollow-knight"]
    env.library.scan()
    assert ids(env) == ["hollow-knight"]


def test_adopt_without_slug_keeps_local_id(env: LibraryEnv) -> None:
    make_game_dir(env.root, "Mystery")
    env.library.scan()
    game = env.library.adopt("local:mystery", title="Mystery Game")
    assert game.install_id == "local:mystery" and game.managed and game.title == "Mystery Game"


def test_adopt_with_explicit_artwork_merges_into_existing_row(env: LibraryEnv) -> None:
    make_game_dir(env.root, "HK")
    env.db.execute("INSERT INTO installs(install_id, path, playtime_seconds, last_played) "
                   "VALUES('hollow-knight', 'old', 100, '2025-01-01T00:00:00+00:00')")
    env.library.scan()
    env.library.record_play_session("local:hk", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", 60)
    game = env.library.adopt("local:hk", slug="hollow-knight", title="HK Legacy", cover_url="https://legacy/c.png",
                             genres=["Action", ""])
    assert (game.title, game.cover_url, game.genres) == ("HK Legacy", "https://legacy/c.png", ["Action"])
    assert game.playtime_seconds == 160 and game.last_played == "2026-01-01T00:01:00+00:00"


def test_adopt_unknown_id(env: LibraryEnv) -> None:
    with pytest.raises(NotFoundError):
        env.library.adopt("local:nothing", slug="x")


# --- setters -------------------------------------------------------------------------------------------


@pytest.fixture
def portal(env: LibraryEnv) -> Path:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"),
                           files={"portal2.exe": b"x", "bin/real.exe": b"x"})
    env.library.scan()
    env.recorder.clear()
    return folder


def test_set_executable_relative_and_absolute(env: LibraryEnv, portal: Path) -> None:
    env.library.set_executable("portal-2", "portal2.exe")
    assert fake_read_manifest(str(portal)).executable == "portal2.exe"  # type: ignore[union-attr]
    env.library.set_executable("portal-2", str(portal / "bin" / "real.exe"))
    expected = os.path.join("bin", "real.exe")
    assert env.library.get("portal-2").executable == expected  # type: ignore[union-attr]
    assert fake_read_manifest(str(portal)).executable == expected  # type: ignore[union-attr]
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"portal-2"}))] * 2
    env.library.set_executable("portal-2", "")
    assert env.library.get("portal-2").executable == ""  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", ["missing.exe", "..\\outside.exe", "../outside.exe"])
def test_set_executable_rejects_bad_paths(env: LibraryEnv, portal: Path, bad: str) -> None:
    (portal.parent / "outside.exe").write_bytes(b"x")
    with pytest.raises(LaunchError):
        env.library.set_executable("portal-2", bad)
    with pytest.raises(LaunchError):
        env.library.set_executable("portal-2", str(portal.parent / "outside.exe"))


def test_setters_on_unmanaged_game_write_minimal_manifest(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Hollow Knight", files={"hk.exe": b"x", "other.exe": b"y"})
    env.library.scan()
    env.library.set_executable("local:hollow knight", "other.exe")
    game = env.library.get("local:hollow knight")
    assert game is not None and game.managed and game.executable == "other.exe"
    assert game.slug == "hollow-knight" and game.cover_url == HOLLOW.cover_url  # still enriched for display
    manifest = fake_read_manifest(str(folder))
    assert manifest is not None and manifest.slug == "" and manifest.title == "Hollow Knight"
    env.library.scan()
    assert ids(env) == ["local:hollow knight"]


def test_launch_options_rename_and_redist(env: LibraryEnv, portal: Path) -> None:
    env.library.set_launch_options("portal-2", args=" -novid ", run_as_admin=True)
    env.library.rename("portal-2", "  Portal   Two ")
    env.library.mark_redist_installed("portal-2")
    game = env.library.get("portal-2")
    assert game is not None
    assert (game.launch_args, game.run_as_admin, game.title, game.redist_installed) == ("-novid", True,
                                                                                         "Portal Two", True)
    manifest = json.loads((portal / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["title"] == "Portal Two" and manifest["redist_installed"] is True
    assert env.rows()["portal-2"]["title"] == "Portal Two"
    assert portal.name == "Portal 2"  # folder never renamed
    with pytest.raises(InstallError):
        env.library.rename("portal-2", "   ")


def test_favorite_and_hidden_publish_only_on_change(env: LibraryEnv, portal: Path) -> None:
    env.library.set_favorite("portal-2", True)
    env.library.set_favorite("portal-2", True)
    env.library.set_hidden("portal-2", True)
    game = env.library.get("portal-2")
    assert game is not None and game.favorite and game.hidden
    assert len(env.recorder.of(LibraryChanged)) == 2
    assert (env.rows()["portal-2"]["favorite"], env.rows()["portal-2"]["hidden"]) == (1, 1)


def test_setters_unknown_id(env: LibraryEnv) -> None:
    for call in (lambda: env.library.set_favorite("x", True), lambda: env.library.set_hidden("x", True),
                 lambda: env.library.rename("x", "y"), lambda: env.library.set_executable("x", ""),
                 lambda: env.library.set_launch_options("x"), lambda: env.library.mark_redist_installed("x"),
                 lambda: env.library.compute_size("x"), lambda: env.library.uninstall("x")):
        with pytest.raises(NotFoundError):
            call()


def test_record_play_session_accumulates(env: LibraryEnv, portal: Path) -> None:
    env.library.record_play_session("portal-2", "2026-01-02T00:00:00+00:00", "2026-01-02T01:00:00+00:00", 3600)
    env.library.record_play_session("portal-2", "2026-01-01T00:00:00+00:00", "2026-01-01T00:30:00+00:00", 1800)
    game = env.library.get("portal-2")
    assert game is not None and game.playtime_seconds == 5400
    assert game.last_played == "2026-01-02T01:00:00+00:00"  # the most recent end, not the last recorded
    assert [s["seconds"] for s in env.sessions()] == [3600, 1800]
    assert len(env.recorder.of(LibraryChanged)) == 2


def test_record_play_session_for_vanished_game_keeps_row(env: LibraryEnv) -> None:
    env.library.record_play_session("gone", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", 60)
    assert env.rows()["gone"]["playtime_seconds"] == 60


def test_set_update_state(env: LibraryEnv, portal: Path) -> None:
    env.library.set_update_state("portal-2", latest_version="v9", available=True)
    env.library.set_update_state("portal-2", latest_version="v9", available=True)
    game = env.library.get("portal-2")
    assert game is not None and game.update_available and game.latest_version == "v9"
    assert len(env.recorder.of(LibraryChanged)) == 1
    row = env.rows()["portal-2"]
    assert row["update_available"] == 1 and row["update_checked_at"]
    env.library.set_update_state("unknown", latest_version="v1", available=True)  # no crash, no row
    assert "unknown" not in env.rows()


def test_compute_size_caches(env: LibraryEnv, portal: Path) -> None:
    size = env.library.compute_size("portal-2")
    assert size == sum(p.stat().st_size for p in portal.rglob("*") if p.is_file())
    assert env.library.get("portal-2").size_bytes == size  # type: ignore[union-attr]
    assert env.rows()["portal-2"]["size_bytes"] == size
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"portal-2"}))]
    env.library.scan()
    assert env.library.get("portal-2").size_bytes == size  # type: ignore[union-attr]


def test_compute_size_is_cancellable(env: LibraryEnv, portal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services.install import installer

    def cancellable_size(path: str, *, token: CancelToken | None = None) -> int:
        assert token is not None
        token.raise_if_cancelled()
        return 1

    monkeypatch.setattr(installer, "directory_size", cancellable_size)
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        env.library.compute_size("portal-2", token=token)
    assert env.rows()["portal-2"]["size_bytes"] is None
    assert env.recorder.of(LibraryChanged) == []


def test_compute_size_is_not_cached_for_a_game_uninstalled_meanwhile(env: LibraryEnv, portal: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services.install import installer

    def measure_while_user_uninstalls(path: str, *, token: CancelToken | None = None) -> int:
        env.library.uninstall("portal-2")  # finishes while the size is still being measured
        return 123_456

    monkeypatch.setattr(installer, "directory_size", measure_while_user_uninstalls)
    assert env.library.compute_size("portal-2") == 123_456
    assert env.rows()["portal-2"]["size_bytes"] is None
    assert env.library.get("portal-2") is None


def test_compute_size_during_uninstall_is_not_cached(env: LibraryEnv, portal: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services import _library_fs

    sizes: list[int] = []
    real_delete_tree = _library_fs.delete_tree

    def delete_while_measuring(*args: object, **kwargs: object) -> object:
        sizes.append(env.library.compute_size("portal-2"))  # e.g. the detail panel refreshing
        return real_delete_tree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_library_fs, "delete_tree", delete_while_measuring)
    env.library.uninstall("portal-2")
    assert sizes and env.rows()["portal-2"]["size_bytes"] is None


def test_concurrent_scans_and_mutations(env: LibraryEnv) -> None:
    for index in range(8):
        make_game_dir(env.root, f"Game {index}", manifest=InstallManifest(slug=f"g{index}", title=f"Game {index}"))
    env.library.scan()
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for _ in range(5):
                env.library.scan()
                env.library.set_favorite(f"g{index}", True)
                env.library.record_play_session(f"g{index}", "2026-01-01T00:00:00+00:00",
                                                "2026-01-01T00:00:10+00:00", 10)
                env.library.games()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    games = env.library.games()
    assert len(games) == 8 and all(g.favorite and g.playtime_seconds == 50 for g in games)
