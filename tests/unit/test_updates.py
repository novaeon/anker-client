"""UpdateService (game updates) and AppUpdateChecker (GitHub releases)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from anker_client.constants import LATEST_RELEASE_API, RELEASES_URL
from anker_client.core import events as ev
from anker_client.core.db import Database
from anker_client.core.errors import (
    NetworkError,
    NotFoundError,
    OperationCancelled,
    RateLimitedError,
    SiteChangedError,
)
from anker_client.core.models import DownloadKind, DownloadOption, GameDetails, InstalledGame
from anker_client.core.tasks import CancelToken
from anker_client.services.updates import AppUpdateChecker, UpdateService, _is_newer_date

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
FULL = DownloadOption(100, "Direct", DownloadKind.FULL)


def installed(slug: str, version: str = "", *, source_updated_date: str = "", managed: bool = True,
              install_id: str | None = None, update_available: bool = False, latest_version: str = "") -> InstalledGame:
    return InstalledGame(
        install_id=install_id or slug or "local:x",
        title=slug.replace("-", " ").title() or "Local",
        path=f"C:/Games/{slug}",
        library_root="C:/Games",
        slug=slug,
        managed=managed,
        version=version,
        source_updated_date=source_updated_date,
        update_available=update_available,
        latest_version=latest_version,
    )


def details(slug: str, version: str = "", *, updated_date: str = "", options: list[DownloadOption] | None = None,
            fetched_at: str = "") -> GameDetails:
    return GameDetails(slug=slug, title=slug.title(), version=version, updated_date=updated_date,
                       download_options=options if options is not None else [FULL], fetched_at=fetched_at)


class FakeLibrary:
    def __init__(self, games: list[InstalledGame]) -> None:
        self._games = games
        self.update_states: list[tuple[str, str, bool]] = []
        self.fail_for: set[str] = set()

    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]:
        return [g.copy() for g in self._games]

    def set_update_state(self, install_id: str, *, latest_version: str, available: bool) -> None:
        if install_id in self.fail_for:
            raise NotFoundError()
        self.update_states.append((install_id, latest_version, available))
        for game in self._games:
            if game.install_id == install_id:
                game.latest_version = latest_version
                game.update_available = available


class FakeCatalog:
    def __init__(self, data: dict[str, GameDetails | Exception]) -> None:
        self.data = data
        self.cache: dict[str, GameDetails] = {}
        self.calls: list[tuple[str, timedelta]] = []
        self.on_call: Any = None

    def details(self, slug: str, *, max_age: timedelta = timedelta(hours=6), token: CancelToken | None = None
                ) -> GameDetails:
        self.calls.append((slug, max_age))
        if self.on_call:
            self.on_call(slug)
        value = self.data[slug]
        if isinstance(value, Exception):
            raise value
        self.cache[slug] = value
        return value.copy()

    def cached_details(self, slug: str) -> GameDetails | None:
        value = self.cache.get(slug)
        return value.copy() if value else None


class RecordingToken(CancelToken):
    __slots__ = ("sleeps",)

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        super().sleep(0)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "anker.db")
    yield database
    database.close()


@pytest.fixture
def bus() -> ev.EventBus:
    return ev.EventBus()


@pytest.fixture
def found(bus: ev.EventBus) -> list[ev.UpdatesFound]:
    seen: list[ev.UpdatesFound] = []
    bus.subscribe(ev.UpdatesFound, seen.append)
    return seen


def make(db: Database, bus: ev.EventBus, games: list[InstalledGame], data: dict[str, Any], *, pause: float = 0.0
         ) -> tuple[UpdateService, FakeLibrary, FakeCatalog]:
    library = FakeLibrary(games)
    catalog = FakeCatalog(data)
    service = UpdateService(db, library, catalog, bus, pause_seconds=pause, clock=lambda: NOW)  # type: ignore[arg-type]
    return service, library, catalog


# --- version decisions -----------------------------------------------------------------------------


class TestDecision:
    @pytest.mark.parametrize(("installed_version", "latest", "expected"), [
        ("v1.5.12620", "V 1.5.12620", False),
        ("v1.5.12620", "v1.5.12620", False),
        ("4.1.1.7398727", "4.1.1.7631656", True),
        ("v4.1.1.7398727", "V 4.1.1.7631656", True),
        ("1.0.9", "1.0.10", True),
        ("2.0", "1.9", False),  # installed is newer than the site
        ("1.0a", "1.0b", True),
        ("build 15", "release 3", True),  # incomparable but different → update
    ])
    def test_versions(self, db, bus, found, installed_version, latest, expected):
        service, library, _ = make(db, bus, [installed("g", installed_version)], {"g": details("g", latest)})
        updates = service.check(token=CancelToken())
        assert bool(updates) is expected
        assert library.update_states == [("g", latest, expected)]

    @pytest.mark.parametrize(("installed_version", "latest", "installed_date", "site_date", "expected"), [
        ("", "v2", "2026-09-01", "2026-10-01", True),
        ("v1", "", "2026-09-01", "2026-10-01", True),
        ("", "", "2026-10-01", "2026-10-01", False),
        ("", "", "2026-10-02", "2026-10-01", False),
        ("", "", "", "2026-10-01", False),
        ("", "", "2026-10-01", "", False),
        ("", "", "2026-10-01", "2026-10-01T15:30:00+00:00", False),  # same calendar day
        ("", "", "2026-10-01T10:00:00+00:00", "2026-10-01T15:30:00+00:00", True),
        ("", "", "2026-10-01", "not a date", False),
    ])
    def test_date_fallback(self, db, bus, installed_version, latest, installed_date, site_date, expected):
        game = installed("g", installed_version, source_updated_date=installed_date)
        service, _, _ = make(db, bus, [game], {"g": details("g", latest, updated_date=site_date)})
        assert bool(service.check(token=CancelToken())) is expected

    def test_newer_date_helper(self):
        assert _is_newer_date("2026-10-02", "2026-10-01")
        assert not _is_newer_date("2026-10-01Z", "2026-10-01")
        assert _is_newer_date("2026-10-01T00:00:01Z", "2026-10-01T00:00:00+00:00")


class TestPatchDetection:
    def test_patch_matching_installed_version(self, db, bus):
        patch = DownloadOption(7, "Update Only From V 4.1.1.7398727 To V 4.1.1.7631656 (124 MB)", DownloadKind.PATCH,
                               size_text="124 MB", from_version="4.1.1.7398727", to_version="4.1.1.7631656")
        other_patch = DownloadOption(8, "Update Only From V 4.0 To V 4.1", DownloadKind.PATCH,
                                     from_version="4.0", to_version="4.1")
        addon = DownloadOption(9, "Language Pack", DownloadKind.ADDON)
        direct = DownloadOption(6, "Direct V 4.1.1.7631656", DownloadKind.FULL, to_version="4.1.1.7631656")
        game = installed("gow", "v4.1.1.7398727")
        data = {"gow": details("gow", "v4.1.1.7631656", options=[addon, other_patch, patch, direct])}
        service, _, _ = make(db, bus, [game], data)

        [update] = service.check(token=CancelToken())

        assert update.patch_option == patch
        assert update.full_option == direct
        assert update.installed_version == "v4.1.1.7398727"
        assert update.latest_version == "v4.1.1.7631656"
        assert (update.install_id, update.slug, update.title) == ("gow", "gow", "Gow")

    def test_prefers_patch_to_latest(self, db, bus):
        stale = DownloadOption(1, "p1", DownloadKind.PATCH, from_version="1.0", to_version="1.1")
        best = DownloadOption(2, "p2", DownloadKind.PATCH, from_version="v1.0", to_version="1.2")
        service, _, _ = make(db, bus, [installed("g", "1.0")], {"g": details("g", "1.2", options=[stale, best, FULL])})
        assert service.check(token=CancelToken())[0].patch_option == best

    def test_no_patch_for_other_version(self, db, bus):
        patch = DownloadOption(1, "p", DownloadKind.PATCH, from_version="0.9", to_version="1.2")
        service, _, _ = make(db, bus, [installed("g", "1.0")], {"g": details("g", "1.2", options=[patch, FULL])})
        [update] = service.check(token=CancelToken())
        assert update.patch_option is None
        assert update.full_option == FULL

    def test_unknown_installed_version_never_gets_a_patch(self, db, bus):
        patch = DownloadOption(1, "p", DownloadKind.PATCH, from_version="", to_version="1.2")
        game = installed("g", "", source_updated_date="2026-01-01")
        service, _, _ = make(db, bus, [game], {"g": details("g", "1.2", updated_date="2026-02-01", options=[patch])})
        [update] = service.check(token=CancelToken())
        assert update.patch_option is None
        assert update.full_option == patch  # primary_option falls back to the first option


# --- check() behaviour -----------------------------------------------------------------------------------


class TestCheck:
    def test_persists_flags_and_clears_stale_ones(self, db, bus, found):
        games = [
            installed("a", "1.0"),
            installed("b", "2.0", update_available=True, latest_version="2.1"),  # stale flag
            installed("", "1.0", install_id="local:folder", managed=False),
            installed("c", "1.0", managed=False, install_id="local:c"),  # unmanaged with a match
        ]
        service, library, catalog = make(db, bus, games, {"a": details("a", "1.1"), "b": details("b", "2.0")})

        updates = service.check(token=CancelToken())

        assert [u.install_id for u in updates] == ["a"]
        assert library.update_states == [("a", "1.1", True), ("b", "2.0", False)]
        assert [slug for slug, _ in catalog.calls] == ["a", "b"]
        assert all(age == timedelta(hours=1) for _, age in catalog.calls)
        assert len(found) == 1 and [u.install_id for u in found[0].updates] == ["a"]

    def test_no_event_without_updates(self, db, bus, found):
        service, _, _ = make(db, bus, [installed("a", "1.0")], {"a": details("a", "1.0")})
        assert service.check(token=CancelToken()) == []
        assert found == []
        assert service.last_checked() == NOW.isoformat()

    def test_failures_are_skipped(self, db, bus, found):
        games = [installed("a", "1.0"), installed("gone", "1.0"), installed("b", "1.0"), installed("c", "1.0")]
        data = {"a": NetworkError(), "gone": NotFoundError(), "b": details("b", "1.5"), "c": RateLimitedError(30)}
        service, library, _ = make(db, bus, games, data)

        updates = service.check(token=CancelToken())

        assert [u.install_id for u in updates] == ["b"]
        assert library.update_states == [("b", "1.5", True)]
        assert len(found) == 1

    def test_unexpected_errors_for_one_game_are_skipped(self, db, bus):
        service, library, _ = make(db, bus, [installed("a", "1.0"), installed("b", "1.0"), installed("c", "1.0")],
                                   {"a": AttributeError("parser bug"), "b": details("b", "2.0"),
                                    "c": details("c", "2.0")})
        original = library.set_update_state

        def flaky(install_id: str, **kwargs: Any) -> None:
            if install_id == "b":
                raise KeyError(install_id)
            original(install_id, **kwargs)

        library.set_update_state = flaky  # type: ignore[method-assign]
        assert [u.install_id for u in service.check(token=CancelToken())] == ["b", "c"]
        assert library.update_states == [("c", "2.0", True)]

    def test_library_failure_for_one_game_is_skipped(self, db, bus):
        service, library, _ = make(db, bus, [installed("a", "1.0"), installed("b", "1.0")],
                                   {"a": details("a", "2.0"), "b": details("b", "2.0")})
        library.fail_for = {"a"}
        assert [u.install_id for u in service.check(token=CancelToken())] == ["a", "b"]

    def test_install_ids_filter(self, db, bus):
        service, _library, catalog = make(db, bus, [installed("a", "1.0"), installed("b", "1.0")],
                                          {"a": details("a", "2.0"), "b": details("b", "2.0")})
        updates = service.check(token=CancelToken(), install_ids=["b"])
        assert [u.install_id for u in updates] == ["b"]
        assert [slug for slug, _ in catalog.calls] == ["b"]
        assert service.last_checked() == ""  # only a full check counts as "checked"

    def test_progress(self, db, bus):
        service, _, _ = make(db, bus, [installed("a", "1"), installed("b", "1"), installed("c", "1")],
                             {"a": details("a", "1"), "b": NetworkError(), "c": details("c", "1")})
        calls: list[tuple[int, int]] = []
        service.check(token=CancelToken(), on_progress=lambda done, total: calls.append((done, total)))
        assert calls == [(1, 3), (2, 3), (3, 3)]

    def test_pauses_between_network_fetches_only(self, db, bus):
        games = [installed("a", "1"), installed("b", "1"), installed("c", "1"),
                 installed("a", "1", install_id="a#copy")]
        service, _, catalog = make(db, bus, games, {"a": details("a", "1"), "b": details("b", "1"),
                                                     "c": details("c", "1")}, pause=0.5)
        catalog.cache["b"] = details("b", "1", fetched_at=(NOW - timedelta(minutes=10)).isoformat())
        token = RecordingToken()

        service.check(token=token)

        # a: first fetch (no pause) · b: fresh cache (no pause) · c: fetch (pause) · a#copy: same slug (no call)
        assert token.sleeps == [0.5]
        assert [slug for slug, _ in catalog.calls] == ["a", "b", "c"]

    def test_stale_cache_counts_as_network(self, db, bus):
        service, _, catalog = make(db, bus, [installed("a", "1"), installed("b", "1")],
                                   {"a": details("a", "1"), "b": details("b", "1")}, pause=0.5)
        catalog.cache["b"] = details("b", "1", fetched_at=(NOW - timedelta(hours=2)).isoformat())
        token = RecordingToken()
        service.check(token=token)
        assert token.sleeps == [0.5]

    def test_cancellation(self, db, bus, found):
        token = CancelToken()
        service, _library, catalog = make(db, bus, [installed("a", "1"), installed("b", "1"), installed("c", "1")],
                                          {"a": details("a", "2"), "b": details("b", "2"), "c": details("c", "2")})
        catalog.on_call = lambda slug: token.cancel() if slug == "b" else None
        with pytest.raises(OperationCancelled):
            service.check(token=token)
        assert [s for s, _ in catalog.calls] == ["a", "b"]
        assert found == []
        assert service.last_checked() == ""

    def test_cancellation_during_pause_is_immediate(self, db, bus):
        service = UpdateService(db, FakeLibrary([installed("a", "1"), installed("b", "1")]),  # type: ignore[arg-type]
                                FakeCatalog({"a": details("a", "1"), "b": details("b", "1")}),  # type: ignore[arg-type]
                                bus, pause_seconds=30, clock=lambda: NOW)
        token = CancelToken()
        threading.Timer(0.05, token.cancel).start()
        with pytest.raises(OperationCancelled):
            service.check(token=token)

    def test_pending_from_flags_and_cache(self, db, bus):
        patch = DownloadOption(1, "p", DownloadKind.PATCH, from_version="1.0", to_version="2.0")
        games = [installed("a", "1.0"), installed("b", "1.0"), installed("c", "1.0", update_available=True,
                                                                        latest_version="3.0")]
        service, _, _ = make(db, bus, games, {"a": details("a", "2.0", options=[patch, FULL]),
                                              "b": details("b", "1.0")})
        assert [u.install_id for u in service.pending()] == ["c"]  # flagged by an earlier check
        service.check(token=CancelToken(), install_ids=["a", "b"])
        pending = {u.install_id: u for u in service.pending()}
        assert set(pending) == {"a", "c"}
        assert pending["a"].patch_option == patch and pending["a"].full_option == FULL
        assert pending["a"].latest_version == "2.0"
        assert pending["c"].latest_version == "3.0" and pending["c"].full_option is None

    def test_empty_library(self, db, bus, found):
        service, _, _ = make(db, bus, [], {})
        assert service.check(token=CancelToken()) == []
        assert found == []
        assert service.last_checked() == NOW.isoformat()


# --- app updates ------------------------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def release(tag: str, **extra: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/novaeon/anker-client/releases/tag/{tag}",
        "body": "Bug fixes",
        "published_at": "2026-10-01T10:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "AnkerClient-portable.zip", "browser_download_url": "https://dl/portable.zip"},
            {"name": "AnkerClient-Setup.EXE", "browser_download_url": "https://dl/setup.exe"},
            {"name": "other.exe", "browser_download_url": "https://dl/other.exe"},
        ],
        **extra,
    }


class TestAppUpdates:
    @pytest.fixture
    def app_events(self, bus: ev.EventBus) -> list[ev.AppUpdateAvailable]:
        seen: list[ev.AppUpdateAvailable] = []
        bus.subscribe(ev.AppUpdateAvailable, seen.append)
        return seen

    def test_newer_release(self, bus, app_events):
        http = FakeHttp(release("v1.2.0"))
        token = CancelToken()
        result = AppUpdateChecker(http, bus, "1.0.0").check(token=token)  # type: ignore[arg-type]

        assert result is not None
        assert result.version == "1.2.0"
        assert result.url.endswith("/v1.2.0")
        assert result.notes == "Bug fixes"
        assert result.published_at == "2026-10-01T10:00:00Z"
        assert result.download_url == "https://dl/setup.exe"
        assert [e.release for e in app_events] == [result]
        url, kwargs = http.calls[0]
        assert url == LATEST_RELEASE_API
        assert kwargs["token"] is token

    @pytest.mark.parametrize(("tag", "current", "newer"), [
        ("v1.0.0", "1.0.0", False),
        ("1.0", "1.0.0", False),
        ("v0.9.5", "1.0.0", False),
        ("V1.0.1", "1.0.0", True),
        ("v1.0.10", "1.0.9", True),
        ("v2", "1.9.9", True),
        ("v1.1.0-beta", "1.0.0", True),
        ("v1.0.0-rc1", "1.0.0", False),
    ])
    def test_version_comparison(self, bus, app_events, tag, current, newer):
        result = AppUpdateChecker(FakeHttp(release(tag)), bus, current).check()  # type: ignore[arg-type]
        assert (result is not None) is newer
        assert len(app_events) == int(newer)

    def test_release_without_exe_and_url(self, bus):
        data = release("v9.0.0", assets=[], html_url=None)
        result = AppUpdateChecker(FakeHttp(data), bus, "1.0.0").check()  # type: ignore[arg-type]
        assert result is not None
        assert result.download_url == ""
        assert result.url == RELEASES_URL

    @pytest.mark.parametrize("payload", [
        None, [], "text", {}, {"tag_name": None}, {"tag_name": "latest"}, {"tag_name": 5},
        release("v9.0.0", draft=True), release("v9.0.0", prerelease=True),
        {"tag_name": "v9.0.0", "assets": "nope"},
    ])
    def test_malformed_or_skipped(self, bus, app_events, payload):
        result = AppUpdateChecker(FakeHttp(payload), bus, "1.0.0").check()  # type: ignore[arg-type]
        if isinstance(payload, dict) and payload.get("assets") == "nope":
            assert result is not None and result.download_url == ""  # odd assets are tolerated
        else:
            assert result is None
            assert app_events == []

    @pytest.mark.parametrize("error", [NetworkError(status=403), NetworkError(status=500), RateLimitedError(60),
                                       SiteChangedError(), NotFoundError()])
    def test_http_errors(self, bus, app_events, error):
        assert AppUpdateChecker(FakeHttp(error), bus, "1.0.0").check() is None  # type: ignore[arg-type]
        assert app_events == []

    def test_cancellation_propagates(self, bus):
        with pytest.raises(OperationCancelled):
            AppUpdateChecker(FakeHttp(OperationCancelled()), bus, "1.0.0").check()  # type: ignore[arg-type]

    def test_unparseable_current_version(self, bus):
        result = AppUpdateChecker(FakeHttp(release("v0.0.1")), bus, "dev").check()  # type: ignore[arg-type]
        assert result is not None
