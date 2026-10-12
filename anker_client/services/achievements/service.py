"""Achievements from Steam emulators: live unlock detection and per-game lists. Qt-free.

The watcher (one daemon thread, ``start``/``shutdown``) polls the unlock files of
every known Steam emulator (``sources.KNOWN_LOCATIONS``, plus custom save paths
found in installed games) once per ``poll_interval``. It only ``stat``s files;
a file is parsed again when its size or modification time changes.

* **Baseline.** Everything present when the watcher starts is read silently:
  unlocks earned while AnkerClient was closed never pop up (Steam does not show
  them either).
* **New unlocks.** When a watched file changes, entries that turned unlocked
  are announced with ``AchievementUnlocked`` (oldest first), followed by one
  ``AchievementsChanged``. A file that did not exist at the baseline counts as
  empty, so a game's very first unlock is announced too.
* **Files found later** (a new ``<appid>`` folder under a save root, a newly
  installed game) may hold old unlocks from an earlier install: only unlocks
  stamped within ``recent_window`` seconds are announced.
* A file that cannot be parsed (caught mid-write) keeps its previous state and
  is read again on the next poll. At most ``MAX_ANNOUNCE`` pop-ups are raised
  per app per poll (unlock-all tools).
* Each announcement carries Steam's name, description, icons and rarity
  (``SteamAchievementSchemas``; prefetched together with the icons when a game
  launches, so the pop-up does not wait for the network).

Installed games are mapped to Steam app ids with ``sources.detect_game`` (cached;
redone for games named by ``LibraryChanged``). Games outside the library still
get pop-ups; they just have no ``install_id``.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from anker_client.core.events import AchievementsChanged, AchievementUnlocked, EventBus, GameLaunched, LibraryChanged
from anker_client.core.models import Achievement, GameAchievements, InstalledGame
from anker_client.core.tasks import CancelToken, TaskRunner
from anker_client.services.achievements import formats
from anker_client.services.achievements.formats import UnlockState
from anker_client.services.achievements.sources import (
    KNOWN_LOCATIONS,
    GameEmulator,
    SaveFile,
    SaveLocation,
    detect_game,
    save_files_in,
)
from anker_client.services.achievements.steam import DEFAULT_LANGUAGE, SteamAchievementSchemas

log = logging.getLogger(__name__)

MAX_ANNOUNCE = 10
_Signature = tuple[int, int] | None  # (mtime_ns, size); None = missing
_Diff = tuple["_Watched", dict[str, UnlockState], dict[str, UnlockState]]  # file, before, after


class _Library(Protocol):  # the part of LibraryService used here (tests pass a fake)
    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]: ...

    def get(self, install_id: str) -> InstalledGame | None: ...


class _Images(Protocol):  # the part of ImageCache used for prefetching
    def cached_path(self, url: str) -> object | None: ...

    def fetch(self, url: str, *, token: CancelToken | None = None) -> object: ...


@dataclass(slots=True)
class _Watched:
    file: SaveFile
    signature: _Signature = None
    states: dict[str, UnlockState] = field(default_factory=dict)
    late: bool = False  # first seen after the baseline


def merge_unlocks(states: Iterable[Mapping[str, UnlockState]]) -> dict[str, UnlockState]:
    """Combine several files' views of one app: unlocked if any says so, earliest known time, max progress."""
    merged: dict[str, UnlockState] = {}
    for source in states:
        for name, state in source.items():
            old = merged.get(name)
            if old is None:
                merged[name] = state
                continue
            times = [t for t in (old.unlocked_at, state.unlocked_at) if t > 0 and (old.unlocked or state.unlocked)]
            merged[name] = UnlockState(
                unlocked=old.unlocked or state.unlocked,
                unlocked_at=min(times) if times else 0,
                progress=max((p for p in (old.progress, state.progress) if p is not None), default=None),
                max_progress=max((p for p in (old.max_progress, state.max_progress) if p is not None), default=None),
            )
    return merged


def build_achievements(definitions: list[Achievement] | None, unlocks: Mapping[str, UnlockState]) -> list[Achievement]:
    """Steam's definitions (in Steam's order) with the local unlock state applied.

    Unlocked names Steam does not list (or every name, when no list is known) are
    appended with a name derived from the API name. Names match exactly, then
    case-insensitively (some emulators change the case).
    """
    result = [copy.copy(a) for a in definitions or []]
    by_name = {a.api_name: a for a in result}
    by_lower = {a.api_name.lower(): a for a in result}
    for name, state in unlocks.items():
        target = by_name.get(name) or by_lower.get(name.lower())
        if target is None:
            if definitions and not state.unlocked:
                continue  # emulators sometimes list stats or stale names; only real unlocks matter
            target = Achievement(api_name=name)
            result.append(target)
            by_name[name] = target
            by_lower[name.lower()] = target
        target.unlocked = target.unlocked or state.unlocked
        if state.unlocked and state.unlocked_at and (not target.unlocked_at or state.unlocked_at < target.unlocked_at):
            target.unlocked_at = state.unlocked_at
        if state.max_progress:
            target.progress = state.progress
            target.max_progress = state.max_progress
    return result


class AchievementService:
    def __init__(
        self,
        library: _Library,
        schemas: SteamAchievementSchemas,
        events: EventBus,
        *,
        images: _Images | None = None,
        runner: TaskRunner | None = None,
        locations: tuple[SaveLocation, ...] = KNOWN_LOCATIONS,
        environ: Mapping[str, str] | None = None,
        detect: Callable[[str], GameEmulator | None] | None = None,
        poll_interval: float = 1.0,
        rescan_interval: float = 5.0,
        recent_window: float = 300.0,
        language: str = DEFAULT_LANGUAGE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._library = library
        self._schemas = schemas
        self._events = events
        self._images = images
        self._runner = runner
        self._locations = locations
        self._environ = environ
        self._detect = detect or (lambda folder: detect_game(folder, environ))
        self._poll_interval = max(0.05, poll_interval)
        self._rescan_interval = max(self._poll_interval, rescan_interval)
        self._recent_window = recent_window
        self._language = language
        self._clock = clock
        self._lock = threading.RLock()
        self._detected: dict[str, tuple[str, GameEmulator | None]] = {}  # install id → (path, result)
        self._dirty: set[str] | None = None  # None = every game; ids to (re)detect
        self._files: dict[str, _Watched] = {}  # watcher thread only
        self._announced: set[tuple[int, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self.baseline_done = threading.Event()

    # --- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._unsubscribe = [
            self._events.subscribe(LibraryChanged, self._on_library_changed),
            self._events.subscribe(GameLaunched, self._on_game_launched),
        ]
        self._thread = threading.Thread(target=self._run, name="achievements", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    # --- public queries (any thread) ------------------------------------------------------
    def app_id_for(self, install_id: str) -> int | None:
        info = self._detection(install_id)
        return info.app_id if info is not None else None

    def achievements_for(self, install_id: str, *, refresh: bool = False,
                         token: CancelToken | None = None) -> GameAchievements | None:
        """The achievements of an installed game, or ``None`` when it has no Steam emulator."""
        game = self._library.get(install_id)
        info = self._detection(install_id)
        if game is None or info is None:
            return None
        return self.achievements_for_app(info.app_id, title=game.title, install_id=install_id,
                                         refresh=refresh, token=token)

    def achievements_for_app(self, app_id: int, *, title: str = "", install_id: str = "", refresh: bool = False,
                             token: CancelToken | None = None) -> GameAchievements:
        files = self._files_for_app(app_id)
        states, labels = [], []
        for save in files:
            parsed = _read(save)
            if parsed:
                states.append(parsed)
                labels.append(save.label)
        definitions = self._schemas.get(app_id, self._language, refresh=refresh, token=token)
        schema_available = definitions is not None
        if definitions is None:
            definitions = self._local_schema(app_id)
        return GameAchievements(
            app_id=app_id,
            title=title,
            install_id=install_id,
            emulators=list(dict.fromkeys(labels)),
            achievements=build_achievements(definitions, merge_unlocks(states)),
            schema_available=schema_available,
        )

    # --- detection --------------------------------------------------------------------------
    def _detection(self, install_id: str) -> GameEmulator | None:
        game = self._library.get(install_id)
        if game is None:
            return None
        with self._lock:
            cached = self._detected.get(install_id)
            dirty = self._dirty is None or install_id in self._dirty
        if cached is not None and cached[0] == game.path and not dirty:
            return cached[1]
        info = self._safe_detect(game)
        with self._lock:
            self._detected[install_id] = (game.path, info)
            if self._dirty is not None:
                self._dirty.discard(install_id)
        return info

    def _safe_detect(self, game: InstalledGame) -> GameEmulator | None:
        try:
            return self._detect(game.path)
        except Exception:
            log.exception("Could not inspect %s for a Steam emulator", game.path)
            return None

    def _detect_dirty(self) -> None:
        games = {g.install_id: g for g in self._library.games()}
        with self._lock:
            dirty = set(games) if self._dirty is None else set(self._dirty)
            self._dirty = set()
            for gone in set(self._detected) - set(games):
                del self._detected[gone]
        for install_id in dirty:
            if self._stop.is_set():
                return
            game = games.get(install_id)
            if game is None:
                continue
            info = self._safe_detect(game)
            with self._lock:
                self._detected[install_id] = (game.path, info)

    def _owner(self, app_id: int) -> tuple[str, str]:
        """(install id, title) of the library game emulating ``app_id`` ("", "" when none)."""
        with self._lock:
            matches = [iid for iid, (_path, info) in self._detected.items() if info and info.app_id == app_id]
        for install_id in sorted(matches):
            game = self._library.get(install_id)
            if game is not None:
                return install_id, game.title
        return "", ""

    def _local_schema(self, app_id: int) -> list[Achievement] | None:
        with self._lock:
            infos = [info for _path, info in self._detected.values() if info and info.app_id == app_id]
        for info in infos:
            if info.schema_file:
                try:
                    with open(info.schema_file, "rb") as handle:
                        return formats.parse_goldberg_schema(handle.read(), self._language)
                except (OSError, ValueError):
                    log.debug("Unreadable achievement schema %s", info.schema_file, exc_info=True)
        return None

    # --- files ------------------------------------------------------------------------------
    def _all_files(self) -> list[SaveFile]:
        with self._lock:
            infos = [info for _path, info in self._detected.values() if info is not None]
        locations = list(self._locations)
        seen_roots = {(loc.root.lower(), loc.relative.lower()) for loc in locations}
        for info in infos:
            for root in info.extra_roots:
                if (root.root.lower(), root.relative.lower()) not in seen_roots:
                    seen_roots.add((root.root.lower(), root.relative.lower()))
                    locations.append(root)
        files: dict[str, SaveFile] = {}
        for location in locations:
            for save in save_files_in(location, self._environ):
                files.setdefault(os.path.normcase(save.path), save)
        for info in infos:
            for save in info.extra_saves:
                files.setdefault(os.path.normcase(save.path), save)
        return list(files.values())

    def _files_for_app(self, app_id: int) -> list[SaveFile]:
        return [f for f in self._all_files() if f.app_id == app_id]

    # --- watcher ----------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._sync_files(baseline=True)  # standard roots first: unlocks right after start are not lost
            self._detect_dirty()
            self._sync_files(baseline=True)  # plus custom save paths of installed games
        except Exception:
            log.exception("Achievement watcher could not read its baseline")
        self.baseline_done.set()
        last_scan = time.monotonic()
        while not self._stop.wait(self._poll_interval):
            try:
                if time.monotonic() - last_scan >= self._rescan_interval:
                    last_scan = time.monotonic()
                    self._detect_dirty()
                    self._sync_files(baseline=False)
                self._poll()
            except Exception:
                log.exception("Achievement watcher poll failed")

    def _sync_files(self, *, baseline: bool) -> None:
        current = {os.path.normcase(f.path): f for f in self._all_files()}
        for key in set(self._files) - set(current):
            del self._files[key]
        for key, save in current.items():
            if key in self._files:
                continue
            watched = _Watched(save, late=not baseline)
            if baseline:
                watched.signature = _signature(save.path)
                watched.states = (_read(save) or {}) if watched.signature else {}
            self._files[key] = watched

    def _poll(self) -> None:
        changed: dict[int, list[_Diff]] = {}
        for watched in list(self._files.values()):
            signature = _signature(watched.file.path)
            if signature == watched.signature:
                continue
            if signature is None:  # deleted (e.g. the game reset its progress): forget, never announce
                watched.signature, watched.states = None, {}
                continue
            states = _read(watched.file)
            if states is None:
                continue  # unreadable right now: retry next poll with the old signature
            previous, watched.states, watched.signature = watched.states, states, signature
            changed.setdefault(watched.file.app_id, []).append((watched, previous, states))
        for app_id, diffs in changed.items():
            self._announce(app_id, diffs)

    def _announce(self, app_id: int, diffs: list[_Diff]) -> None:
        now = self._clock()
        fresh: dict[str, UnlockState] = {}
        for watched, previous, states in diffs:
            for name, state in states.items():
                before = previous.get(name)
                if not state.unlocked or (before is not None and before.unlocked):
                    continue
                recent = bool(state.unlocked_at) and now - state.unlocked_at <= self._recent_window
                if watched.late and not previous and not recent:
                    continue  # an old unlock in a file we only just discovered
                if (app_id, name) in self._announced:
                    continue
                fresh.setdefault(name, state)
            watched.late = False
        install_id, title = self._owner(app_id)
        if fresh:
            data = self.achievements_for_app(app_id, title=title, install_id=install_id)
            ordered = sorted(fresh.items(), key=lambda item: (item[1].unlocked_at or now, item[0]))
            if len(ordered) > MAX_ANNOUNCE:
                log.info("%d achievements unlocked at once for app %s; announcing %d",
                         len(ordered), app_id, MAX_ANNOUNCE)
            for name, state in ordered[:MAX_ANNOUNCE]:
                self._announced.add((app_id, name))
                achievement = data.get(name) or _case_insensitive(data, name) or Achievement(
                    api_name=name, unlocked=True, unlocked_at=state.unlocked_at)
                log.info("Achievement unlocked in app %s: %s", app_id, achievement.display_name)
                self._events.publish(AchievementUnlocked(
                    app_id=app_id,
                    achievement=copy.copy(achievement),
                    game_title=title,
                    install_id=install_id,
                    unlocked_count=data.unlocked_count if data.schema_available else 0,
                    total=data.total if data.schema_available else 0,
                ))
        self._events.publish(AchievementsChanged(
            app_ids=frozenset({app_id}), install_ids=frozenset({install_id}) if install_id else frozenset()
        ))

    # --- event handlers ---------------------------------------------------------------------
    def _on_library_changed(self, event: LibraryChanged) -> None:
        with self._lock:
            if not event.install_ids:
                self._dirty = None
            elif self._dirty is not None:
                self._dirty.update(event.install_ids)

    def _on_game_launched(self, event: GameLaunched) -> None:
        if self._runner is None:
            return
        try:
            self._runner.submit(self._prefetch, event.install_id, name="achievements-prefetch")
        except RuntimeError:
            pass  # runner already shut down

    def _prefetch(self, install_id: str, *, token: CancelToken) -> None:
        """Steam's list and every icon of a game that just started, so pop-ups are instant."""
        app_id = self.app_id_for(install_id)
        if app_id is None:
            return
        definitions = self._schemas.get(app_id, self._language, token=token) or []
        if self._images is None:
            return
        for achievement in definitions:
            token.raise_if_cancelled()
            url = achievement.icon_url
            if url and self._images.cached_path(url) is None:
                try:
                    self._images.fetch(url, token=token)
                except Exception:
                    log.debug("Could not prefetch achievement icon %s", url, exc_info=True)


def _case_insensitive(data: GameAchievements, name: str) -> Achievement | None:
    lower = name.lower()
    return next((a for a in data.achievements if a.api_name.lower() == lower), None)


def _signature(path: str) -> _Signature:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read(save: SaveFile) -> dict[str, UnlockState] | None:
    """Parse one file; ``None`` when it is missing or unreadable right now."""
    try:
        with open(save.path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if not data.strip():
        return {}
    try:
        return save.parse(data)
    except (ValueError, UnicodeError) as exc:
        log.debug("Could not parse %s: %s", save.path, exc)
        return None
