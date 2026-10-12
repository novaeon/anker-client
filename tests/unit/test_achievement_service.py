"""Steam achievement lists and the unlock watcher (real files in a temp folder, fake library/Steam)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.db import Database
from anker_client.core.errors import NetworkError
from anker_client.core.events import AchievementsChanged, AchievementUnlocked, EventBus, GameLaunched, LibraryChanged
from anker_client.core.models import Achievement, InstalledGame
from anker_client.core.tasks import TaskRunner
from anker_client.services.achievements.formats import UnlockState
from anker_client.services.achievements.service import (
    MAX_ANNOUNCE,
    AchievementService,
    build_achievements,
    merge_unlocks,
)
from anker_client.services.achievements.steam import SteamAchievementSchemas, icon_url, parse_schema

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "steam" / "get_game_achievements_4019220.json"
APP = 4019220


# --- Steam list ------------------------------------------------------------------------------


def test_parse_real_get_game_achievements_response() -> None:
    defs = parse_schema(APP, json.loads(FIXTURE.read_text(encoding="utf-8")))
    by_name = {a.api_name: a for a in defs}
    assert list(by_name)[:2] == ["FUNERAL_CAMPAIGN", "MAX_QUALITY"]
    laid = by_name["FUNERAL_CAMPAIGN"]
    assert (laid.name, laid.hidden, laid.global_percent) == ("Laid To Dress", True, 81.5)
    assert laid.description  # hidden achievements still come with their description
    assert laid.icon_url.startswith("https://shared.fastly.steamstatic.com/community_assets/images/apps/4019220/")
    assert laid.icon_gray_url.endswith(".jpg") and laid.icon_gray_url != laid.icon_url
    assert by_name["BERTY_UNLOCK"].name == "You’ve Got Mail"
    assert by_name["TRIM"].rare and not laid.rare


def test_parse_schema_edge_cases() -> None:
    assert parse_schema(1, {"response": {}}) == []  # unknown app / no achievements
    dupes = {"response": {"achievements": [{"internal_name": "A"}, {"internal_name": "A"}, {"x": 1}, "junk"]}}
    assert [a.api_name for a in parse_schema(1, dupes)] == ["A"]
    with pytest.raises(ValueError):
        parse_schema(1, {"error": "nope"})
    assert icon_url(5, "") == "" and icon_url(5, "https://x/y.jpg") == "https://x/y.jpg"


class StubHttp:
    def __init__(self, document: Any = None) -> None:
        self.document = document
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.document


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "anker.db")
    yield database
    database.close()


def test_schema_cache_fresh_stale_and_offline(db: Database) -> None:
    now = [1_800_000_000.0]
    http = StubHttp(json.loads(FIXTURE.read_text(encoding="utf-8")))
    schemas = SteamAchievementSchemas(http, db, max_age=3600, clock=lambda: now[0])  # type: ignore[arg-type]
    assert schemas.cached(APP) is None
    first = schemas.get(APP)
    assert first is not None and len(first) == 5 and len(http.calls) == 1
    assert http.calls[0]["params"] == {"appid": APP, "language": "english"}
    assert schemas.get(APP) == first and len(http.calls) == 1  # fresh: no request
    now[0] += 7200
    http.error = NetworkError("offline")
    assert schemas.get(APP) == first and len(http.calls) == 2  # stale + offline: the cached copy
    http.error = None
    http.document = {"response": {}}
    assert schemas.get(APP, refresh=True) == []  # the game dropped its achievements: [] is cached too
    assert schemas.cached(APP) == [] and schemas.is_fresh(APP)
    http.error = NetworkError("offline")
    assert schemas.get(999) is None  # never fetched and unreachable: nothing known


# --- merging -----------------------------------------------------------------------------------


def test_merge_unlocks_across_emulators() -> None:
    merged = merge_unlocks([
        {"A": UnlockState(True, 200), "B": UnlockState(False, 0, 3, 10)},
        {"A": UnlockState(True, 100), "B": UnlockState(False, 0, 5, 10), "C": UnlockState(True, 0)},
    ])
    assert merged["A"] == UnlockState(True, 100)
    assert merged["B"] == UnlockState(False, 0, 5, 10)
    assert merged["C"] == UnlockState(True, 0)


def test_build_achievements_matches_names_and_keeps_steam_order() -> None:
    defs = [Achievement(api_name="First", name="One"), Achievement(api_name="Second", name="Two")]
    built = build_achievements(defs, {"second": UnlockState(True, 9), "Junk_Stat": UnlockState(False),
                                      "Extra_Unlock": UnlockState(True, 4)})
    assert [a.api_name for a in built] == ["First", "Second", "Extra_Unlock"]
    assert built[1].unlocked and built[1].unlocked_at == 9  # case-insensitive match
    assert not built[0].unlocked and defs[0].unlocked is False  # inputs are not mutated
    unknown = build_achievements(None, {"ACH_X": UnlockState(True, 1), "ACH_Y": UnlockState(False)})
    assert [(a.display_name, a.unlocked) for a in unknown] == [("X", True), ("Y", False)]


# --- watcher -----------------------------------------------------------------------------------


class FakeLibrary:
    def __init__(self, games: list[InstalledGame]) -> None:
        self.items = {g.install_id: g for g in games}

    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]:
        return list(self.items.values())

    def get(self, install_id: str) -> InstalledGame | None:
        return self.items.get(install_id)


class StubSchemas:
    def __init__(self, definitions: dict[int, list[Achievement]] | None = None) -> None:
        self.definitions = definitions or {}
        self.calls: list[int] = []

    def get(self, app_id: int, language: str = "english", *, refresh: bool = False, token: Any = None) -> Any:
        self.calls.append(app_id)
        return [Achievement(**a.to_dict()) for a in self.definitions[app_id]] if app_id in self.definitions else None


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = {"PUBLIC": str(tmp_path / "public"), "APPDATA": str(tmp_path / "appdata")}
        self.events = EventBus()
        self.unlocked: list[AchievementUnlocked] = []
        self.changed: list[AchievementsChanged] = []
        self.events.subscribe(AchievementUnlocked, self.unlocked.append)
        self.events.subscribe(AchievementsChanged, self.changed.append)
        game_dir = tmp_path / "Games" / "Dressmaker"
        (game_dir / "plugins").mkdir(parents=True)
        (game_dir / "plugins" / "steam_emu.ini").write_text("### RUNE\n[Settings]\nAppId=4019220\n")
        self.game = InstalledGame(install_id="dressmaker", title="Dressmaker", path=str(game_dir),
                                  library_root=str(tmp_path / "Games"))
        self.library = FakeLibrary([self.game])
        self.schemas = StubSchemas({APP: parse_schema(APP, json.loads(FIXTURE.read_text(encoding="utf-8")))})
        self.service: AchievementService | None = None

    def rune_file(self, app_id: int = APP) -> Path:
        path = Path(self.env["PUBLIC"]) / "Documents" / "Steam" / "RUNE" / str(app_id) / "achievements.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def gbe_file(self, app_id: int) -> Path:
        path = Path(self.env["APPDATA"]) / "GSE Saves" / str(app_id) / "achievements.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def start(self, **kwargs: Any) -> AchievementService:
        options: dict[str, Any] = {"environ": self.env, "poll_interval": 0.05, "rescan_interval": 0.1}
        options.update(kwargs)
        self.service = AchievementService(self.library, self.schemas, self.events, **options)  # type: ignore[arg-type]
        self.service.start()
        assert self.service.baseline_done.wait(5)
        return self.service

    def stop(self) -> None:
        if self.service is not None:
            self.service.shutdown()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.stop()


def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def rune(*entries: tuple[str, int]) -> str:
    return "".join(f"[{name}]\nAchieved=1\nCurProgress=0\nMaxProgress=0\nUnlockTime={stamp}\n\n" for name, stamp in entries)


def test_baseline_is_silent_and_a_first_unlock_pops_up(world: World) -> None:
    world.rune_file().write_text("")  # Dressmaker today: an empty file
    old = world.gbe_file(1902940)
    old.write_text(json.dumps({"STEAM_A1": {"earned": True, "earned_time": 1780897608}}))
    world.start()
    time.sleep(0.3)
    assert world.unlocked == []  # nothing earned while AnkerClient was closed pops up
    world.rune_file().write_text(rune(("FUNERAL_CAMPAIGN", int(time.time()))))
    event = wait_for(lambda: world.unlocked and world.unlocked[0])
    assert (event.app_id, event.install_id, event.game_title) == (APP, "dressmaker", "Dressmaker")
    assert event.achievement.display_name == "Laid To Dress" and event.achievement.unlocked
    assert event.achievement.icon_url and (event.unlocked_count, event.total) == (1, 5)
    wait_for(lambda: world.changed)
    assert world.changed[-1].install_ids == frozenset({"dressmaker"})


def test_a_save_file_created_after_start_counts_as_new(world: World) -> None:
    world.rune_file()  # the app folder exists, the file does not yet
    world.start()
    world.rune_file().write_text(rune(("MAX_QUALITY", 0)))  # unknown unlock time still pops up
    wait_for(lambda: world.unlocked)
    assert [e.achievement.api_name for e in world.unlocked] == ["MAX_QUALITY"]


def test_newly_discovered_app_folder_only_announces_recent_unlocks(world: World) -> None:
    world.start(recent_window=600)
    now = int(time.time())
    world.gbe_file(555).write_text(json.dumps({
        "OLD": {"earned": True, "earned_time": now - 86400},
        "NEW": {"earned": True, "earned_time": now - 5},
    }))
    wait_for(lambda: world.unlocked)
    time.sleep(0.3)
    assert [e.achievement.api_name for e in world.unlocked] == ["NEW"]
    assert world.unlocked[0].install_id == "" and world.unlocked[0].total == 0  # not in the library, no list


def test_unreadable_write_is_retried_and_each_unlock_pops_once(world: World) -> None:
    path = world.gbe_file(321)
    path.write_text(json.dumps({"A": {"earned": False, "earned_time": 0}}))
    world.start()
    path.write_text('{"A": {"earned": tr')  # caught mid-write
    time.sleep(0.3)
    assert world.unlocked == []
    path.write_text(json.dumps({"A": {"earned": True, "earned_time": int(time.time())}}))
    wait_for(lambda: world.unlocked)
    path.write_text(json.dumps({"A": {"earned": True, "earned_time": int(time.time())}, "B": {"earned": False}}))
    time.sleep(0.3)
    assert [e.achievement.api_name for e in world.unlocked] == ["A"]


def test_unlocks_are_announced_oldest_first_and_capped(world: World) -> None:
    path = world.gbe_file(42)
    path.write_text("{}")
    world.start()
    base = int(time.time()) - 100
    path.write_text(json.dumps({f"ACH_{i:02d}": {"earned": True, "earned_time": base + (20 - i)} for i in range(20)}))
    wait_for(lambda: len(world.unlocked) >= MAX_ANNOUNCE)
    time.sleep(0.3)
    assert len(world.unlocked) == MAX_ANNOUNCE
    assert [e.achievement.api_name for e in world.unlocked][:2] == ["ACH_19", "ACH_18"]


def test_achievements_for_merges_steam_list_with_unlocks(world: World) -> None:
    world.rune_file().write_text(rune(("TRIM", 1790000000)))
    service = world.start()
    data = service.achievements_for("dressmaker")
    assert data is not None and data.schema_available
    assert (data.app_id, data.emulators, data.unlocked_count, data.total) == (APP, ["RUNE"], 1, 5)
    assert data.get("TRIM").unlocked_at == 1790000000  # type: ignore[union-attr]
    assert service.achievements_for("unknown") is None


def test_offline_falls_back_to_the_games_own_schema(world: World, tmp_path: Path) -> None:
    settings = tmp_path / "Games" / "Indie" / "steam_settings"
    settings.mkdir(parents=True)
    (settings / "steam_appid.txt").write_text("888")
    (settings / "achievements.json").write_text(json.dumps([{"name": "WIN", "displayName": "Winner"},
                                                            {"name": "LOSE", "displayName": "Loser"}]))
    world.library.items["indie"] = InstalledGame(install_id="indie", title="Indie", path=str(settings.parent),
                                                 library_root=str(tmp_path / "Games"))
    world.gbe_file(888).write_text(json.dumps({"WIN": {"earned": True, "earned_time": 5}}))
    service = world.start()
    data = service.achievements_for("indie")
    assert data is not None and not data.schema_available
    assert [(a.display_name, a.unlocked) for a in data.achievements] == [("Winner", True), ("Loser", False)]


def test_library_changes_trigger_new_detection(world: World, tmp_path: Path) -> None:
    calls: list[str] = []
    from anker_client.services.achievements.sources import detect_game

    def counting(folder: str) -> Any:
        calls.append(folder)
        return detect_game(folder, world.env)

    world.start(detect=counting)
    wait_for(lambda: calls)
    before = len(calls)
    world.events.publish(LibraryChanged(install_ids=frozenset({"dressmaker"})))
    wait_for(lambda: len(calls) > before)


def test_game_launch_prefetches_list_and_icons(world: World) -> None:
    fetched: list[str] = []
    done = threading.Event()

    class Images:
        def cached_path(self, url: str) -> None:
            return None

        def fetch(self, url: str, *, token: Any = None) -> str:
            fetched.append(url)
            if len(fetched) == 5:
                done.set()
            return url

    runner = TaskRunner(max_workers=2, name="test-ach")
    try:
        world.start(images=Images(), runner=runner)
        world.events.publish(GameLaunched(install_id="dressmaker", title="Dressmaker"))
        assert done.wait(5)
        assert APP in world.schemas.calls
        assert all(url.startswith("https://shared.fastly.steamstatic.com/") for url in fetched)
    finally:
        runner.shutdown(wait=True)
