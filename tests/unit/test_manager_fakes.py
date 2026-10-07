"""Fakes and a harness for the DownloadManager tests (no tests in here).

Every collaborator of the manager is faked; the Database, SettingsStore and
EventBus are the real ones, living in ``tmp_path``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.db import Database
from anker_client.core.errors import OperationCancelled
from anker_client.core.events import Event, EventBus, JobUpdated
from anker_client.core.models import (
    DownloadJob,
    DownloadOption,
    InstalledGame,
    InstallRequest,
    InstallResult,
    JobState,
    ResolvedLink,
)
from anker_client.core.paths import AppPaths
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.engine import DownloadProgress
from anker_client.services.downloads.manager import DownloadManager
from anker_client.services.install import diskspace

DEFAULT_SIZE = 4096
FULL = DownloadOption(101, "Direct", size_text="4 KB")


def wait_until(predicate: Callable[[], Any], timeout: float = 5.0, message: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s {message}")
        time.sleep(0.005)


class FakeClock:
    """Manually advanced clock (used for both epoch and monotonic time)."""

    def __init__(self, start: float | None = None) -> None:
        self._lock = threading.Lock()
        self._now = time.time() if start is None else start

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class EventRecorder:
    def __init__(self, bus: EventBus) -> None:
        self._lock = threading.Lock()
        self.events: list[Event] = []
        bus.subscribe(Event, self._on_event)

    def _on_event(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def snapshot(self) -> list[Event]:
        with self._lock:
            return list(self.events)

    def of_type(self, cls: type) -> list[Any]:
        return [e for e in self.snapshot() if isinstance(e, cls)]

    def job_states(self, job_id: str) -> list[JobState]:
        """States of successive JobUpdated events for ``job_id`` with repeats collapsed."""
        states: list[JobState] = []
        for event in self.of_type(JobUpdated):
            if event.job.id == job_id and (not states or states[-1] is not event.job.state):
                states.append(event.job.state)
        return states

    def clear(self) -> None:
        with self._lock:
            self.events.clear()


# --- resolver ------------------------------------------------------------------------------


class FakeResolver:
    """Stands in for ``LinkResolver``; ``script`` items are consumed one per call:
    a ``ResolvedLink``, an exception to raise, or ``"block"`` (stay in VERIFYING until
    ``release`` is set or the token fires)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.script: list[Any] = []
        self.release = threading.Event()
        self.link = ResolvedLink(
            url="https://cdn.example.test/files/game.zip",
            filename="game.zip",
            size=DEFAULT_SIZE,
            etag='"v1"',
            accept_ranges=True,
        )

    def resolve(
        self,
        option: DownloadOption,
        *,
        slug: str,
        title: str,
        job_id: str = "",
        token: CancelToken,
        on_state: Callable[[JobState, str], None] | None = None,
    ) -> ResolvedLink:
        with self.lock:
            self.calls.append({"option": option, "slug": slug, "title": title, "job_id": job_id})
            item = self.script.pop(0) if self.script else self.link
        if on_state:
            on_state(JobState.RESOLVING, "Requesting download link…")
        if item == "block":
            if on_state:
                on_state(JobState.VERIFYING, "Waiting for browser verification…")
            while not self.release.is_set():
                if token.wait(0.005):
                    raise OperationCancelled()
            item = self.link
        token.raise_if_cancelled()
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        with self.lock:
            return len(self.calls)


# --- downloader ----------------------------------------------------------------------------


@dataclass
class DownloadCall:
    url: str
    dest: str
    connections: int
    start_offset: int


class DownloadScript:
    """Shared behaviour of every ``FakeDownloader`` built by ``factory``.

    ``actions`` items are consumed one per ``download`` call: ``"ok"``, ``"block"``
    (write half, then wait for ``release``/cancel), ``"stall"`` (write half, report
    progress, then wait like block) or an exception (raised after writing a few bytes).
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[DownloadCall] = []
        self.actions: list[Any] = []
        self.default_action: Any = "ok"
        self.release = threading.Event()
        self.steps = 8
        self.active = 0
        self.max_active = 0
        self.blocked = 0

    def factory(self, connections: int) -> FakeDownloader:
        return FakeDownloader(self, connections)

    def next_action(self) -> Any:
        with self.lock:
            return self.actions.pop(0) if self.actions else self.default_action

    @property
    def call_count(self) -> int:
        with self.lock:
            return len(self.calls)


class FakeDownloader:
    def __init__(self, script: DownloadScript, connections: int) -> None:
        self.script = script
        self.connections = connections

    def download(
        self,
        link: ResolvedLink,
        dest_path: str,
        *,
        token: CancelToken,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> str:
        script = self.script
        size = link.size or DEFAULT_SIZE
        part = dest_path + ".part"
        start = os.path.getsize(part) if os.path.exists(part) else 0
        action = script.next_action()
        with script.lock:
            script.calls.append(DownloadCall(link.url, dest_path, self.connections, start))
            script.active += 1
            script.max_active = max(script.max_active, script.active)
        try:
            with open(part, "ab") as handle:
                done = start
                if isinstance(action, BaseException):
                    handle.write(b"x" * min(16, size - done))
                    raise action
                chunk = max(1, (size - start) // script.steps)
                while done < size:
                    token.raise_if_cancelled()
                    if action in ("block", "stall") and done >= size // 2:
                        handle.flush()
                        self._wait_for_release(token)
                        action = "ok"
                    n = min(chunk, size - done)
                    handle.write(b"x" * n)
                    done += n
                    if on_progress:
                        on_progress(DownloadProgress(done, size, 1000.0, (size - done) / 1000.0, self.connections))
            os.replace(part, dest_path)
            return dest_path
        finally:
            with script.lock:
                script.active -= 1

    def _wait_for_release(self, token: CancelToken) -> None:
        with self.script.lock:
            self.script.blocked += 1
        try:
            while not self.script.release.is_set():
                if token.wait(0.005):
                    raise OperationCancelled()
        finally:
            with self.script.lock:
                self.script.blocked -= 1

    @staticmethod
    def partial_size(dest_path: str) -> int:
        part = dest_path + ".part"
        return os.path.getsize(part) if os.path.exists(part) else 0

    @staticmethod
    def discard_partial(dest_path: str) -> None:
        for path in (dest_path + ".part", dest_path + ".part.json"):
            if os.path.exists(path):
                os.remove(path)


# --- installer / library / limiter / disk ---------------------------------------------------


@dataclass
class InstallCall:
    request: InstallRequest
    keep_archive: bool


class FakeInstaller:
    """``actions``: ``"ok"`` or an exception raised during extraction. ``gate`` (if set)
    blocks extraction until set. Deletes the archive like the real installer unless
    ``keep_archive`` or ``delete_archive`` is False."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[InstallCall] = []
        self.actions: list[Any] = []
        self.gate: threading.Event | None = None
        self.delete_archive = True
        self.executable = "game.exe"
        self.active = 0
        self.max_active = 0

    def install(
        self,
        request: InstallRequest,
        *,
        token: CancelToken,
        on_progress: Callable[[str, float], None] | None = None,
        keep_archive: bool = False,
    ) -> InstallResult:
        with self.lock:
            self.calls.append(InstallCall(request, keep_archive))
            action = self.actions.pop(0) if self.actions else "ok"
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            assert os.path.isfile(request.archive_path), request.archive_path
            report = on_progress or (lambda _phase, _fraction: None)
            report("extracting", 0.0)
            if self.gate is not None:
                while not self.gate.is_set():
                    if token.wait(0.005):
                        raise OperationCancelled()
            report("extracting", 0.5)
            if isinstance(action, BaseException):
                raise action
            report("extracting", 1.0)
            token.raise_if_cancelled()
            report("installing", 0.5)
            dest = request.existing_install_path or os.path.join(request.library_root, request.title)
            os.makedirs(dest, exist_ok=True)
            report("installing", 1.0)
            if not keep_archive and self.delete_archive:
                os.remove(request.archive_path)
            return InstallResult(install_path=dest, executable=self.executable, size_bytes=10)
        finally:
            with self.lock:
                self.active -= 1

    @property
    def call_count(self) -> int:
        with self.lock:
            return len(self.calls)


class FakeLibrary:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.games: dict[str, InstalledGame] = {}
        self.registered: list[tuple[InstallResult, InstallRequest]] = []

    def add(self, slug: str, path: str, *, managed: bool = True, library_root: str = "") -> InstalledGame:
        game = InstalledGame(
            install_id=slug,
            title=slug.title(),
            path=path,
            library_root=library_root or os.path.dirname(path),
            slug=slug,
            managed=managed,
        )
        with self.lock:
            self.games[slug] = game
        return game

    def find_by_slug(self, slug: str) -> InstalledGame | None:
        with self.lock:
            game = self.games.get(slug)
            return game.copy() if game else None

    def register_install(self, result: InstallResult, request: InstallRequest) -> InstalledGame:
        game = InstalledGame(
            install_id=request.slug or "local:x",
            title=request.title,
            path=result.install_path,
            library_root=request.library_root,
            slug=request.slug,
            executable=result.executable,
        )
        with self.lock:
            self.registered.append((result, request))
            if request.slug:
                self.games[request.slug] = game
        return game


class FakeRateLimiter:
    def __init__(self) -> None:
        self.rates: list[int] = []

    def set_rate(self, rate_bps: int) -> None:
        self.rates.append(rate_bps)


class SpaceChecker:
    """Replaces ``diskspace.ensure_space``; raises ``error`` when set."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    def __call__(
        self, archive_size: int | None, *, download_dir: str, library_root: str, already_downloaded: int = 0
    ) -> None:
        self.calls.append(
            {"size": archive_size, "download_dir": download_dir, "library_root": library_root,
             "already": already_downloaded}
        )
        if self.error is not None:
            raise self.error


# --- harness -------------------------------------------------------------------------------


@dataclass
class Harness:
    tmp: Path
    db: Database
    events: EventBus
    settings: SettingsStore
    recorder: EventRecorder
    resolver: FakeResolver
    script: DownloadScript
    installer: FakeInstaller
    library: FakeLibrary
    limiter: FakeRateLimiter
    space: SpaceChecker
    clock: FakeClock
    library_root: str
    managers: list[DownloadManager] = field(default_factory=list)
    verify_calls: list[str] = field(default_factory=list)

    def make_manager(self, **overrides: Any) -> DownloadManager:
        kwargs: dict[str, Any] = {
            "db": self.db,
            "settings": self.settings,
            "events": self.events,
            "paths": AppPaths.under(self.tmp / "home"),
            "resolver": self.resolver,
            "downloader_factory": self.script.factory,
            "rate_limiter": self.limiter,
            "installer": self.installer,
            "library": self.library,
            "verify_archive": self._verify,
            "clock": self.clock,
            "poll_interval": 0.02,
        }
        kwargs.update(overrides)
        manager = DownloadManager(**kwargs)
        self.managers.append(manager)
        return manager

    def _verify(self, path: str, token: CancelToken) -> None:
        self.verify_calls.append(path)

    def wait_state(self, manager: DownloadManager, job_id: str, *states: JobState, timeout: float = 5.0) -> DownloadJob:
        def check() -> DownloadJob | None:
            job = manager.get(job_id)
            return job if job is not None and job.state in states else None

        try:
            wait_until(check, timeout)
        except AssertionError:
            job = manager.get(job_id)
            raise AssertionError(
                f"job {job_id} never reached {states}; now {job.state if job else None}: "
                f"{job.error if job else ''} {job.status_text if job else ''}"
            ) from None
        self.settle(manager)
        job = manager.get(job_id)
        assert job is not None
        return job

    @staticmethod
    def settle(manager: DownloadManager, timeout: float = 5.0) -> None:
        """Wait until every queued DB write/event of ``manager`` has been applied.

        (A state is visible in memory a moment before it is published and written —
        by the mutating thread or, while it runs, the scheduler.)"""

        def drained() -> bool:
            with manager._lock:
                idle = not manager._outbox and not manager._db_queue and manager._draining_thread is None
            return idle and not manager._db_lock.locked()

        wait_until(drained, timeout, "(outbox not drained)")

    def db_job(self, job_id: str) -> dict[str, Any] | None:
        """The stored JSON of ``job_id`` once every manager has persisted what it queued."""
        import json

        for manager in self.managers:
            self.settle(manager)
        row = self.db.query_one("SELECT json FROM jobs WHERE id = ?", (job_id,))
        return json.loads(row["json"]) if row is not None else None

    def close(self) -> None:
        self.script.release.set()
        self.resolver.release.set()
        if self.installer.gate is not None:
            self.installer.gate.set()
        for manager in self.managers:
            manager.shutdown(timeout=5)
        self.db.close()


def make_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> Harness:
    events = EventBus()
    recorder = EventRecorder(events)
    library_root = tmp_path / "Games"
    library_root.mkdir()
    store = SettingsStore(tmp_path / "config.json", events)
    store.update(library_dirs=[str(library_root)], default_library=str(library_root), **settings)
    recorder.clear()
    space = SpaceChecker()
    monkeypatch.setattr(diskspace, "ensure_space", space)
    return Harness(
        tmp=tmp_path,
        db=Database(tmp_path / "anker.db"),
        events=events,
        settings=store,
        recorder=recorder,
        resolver=FakeResolver(),
        script=DownloadScript(),
        installer=FakeInstaller(),
        library=FakeLibrary(),
        limiter=FakeRateLimiter(),
        space=space,
        clock=FakeClock(),
        library_root=str(library_root),
    )


def enqueue(manager: DownloadManager, slug: str = "hollow-knight", option: DownloadOption = FULL, **kw: Any) -> DownloadJob:
    return manager.enqueue(slug=slug, title=kw.pop("title", slug.replace("-", " ").title()), option=option, **kw)
