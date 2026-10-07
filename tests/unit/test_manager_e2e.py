"""DownloadManager end to end with the real collaborators.

Real: HttpDownloader (against a local Range-capable server), RateLimiter,
Extractor (7-Zip when installed, else the zip fallback), Installer,
LibraryService, disk-space checks, Database, SettingsStore, EventBus.
Only the LinkResolver is a stub (it would need ankergames.net). Shortcut
locations point into ``tmp_path`` (and shortcuts are switched off anyway).
"""

from __future__ import annotations

import io
import os
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.db import Database
from anker_client.core.events import EventBus, GameInstalled
from anker_client.core.models import DownloadOption, JobState, ResolvedLink
from anker_client.core.paths import AppPaths
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.manager import DownloadManager
from anker_client.services.downloads.ratelimit import RateLimiter
from anker_client.services.install import shortcuts as shortcuts_module
from anker_client.services.install.extractor import Extractor
from anker_client.services.install.installer import Installer
from anker_client.services.install.shortcuts import ShortcutService
from anker_client.services.library import LibraryService
from tests.unit.test_engine_support import KIB, FileServer, SessionHttp, make_downloader, random_bytes
from tests.unit.test_manager_fakes import EventRecorder, Harness, wait_until

OPTION = DownloadOption(77, "Direct", size_text="1 MB")


def _game_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("My Game/MyGame.exe", b"MZ" + random_bytes(64 * KIB, seed=3))
        archive.writestr("My Game/data/level1.pak", random_bytes(600 * KIB, seed=4))
        archive.writestr("My Game/Read Me.txt", b"junk")
    return buffer.getvalue()


class _StubResolver:
    def __init__(self, link: ResolvedLink) -> None:
        self.link = link
        self.calls = 0

    def resolve(self, option, *, slug, title, job_id="", token: CancelToken, on_state=None) -> ResolvedLink:
        self.calls += 1
        token.raise_if_cancelled()
        if on_state:
            on_state(JobState.RESOLVING, "Requesting download link…")
        return self.link


@dataclass
class _World:
    tmp: Path
    server: FileServer
    http: SessionHttp
    db: Database
    events: EventBus
    recorder: EventRecorder
    settings: SettingsStore
    library: LibraryService
    installer: Installer
    extractor: Extractor
    limiter: RateLimiter
    resolver: _StubResolver
    library_root: Path
    managers: list[DownloadManager]

    def manager(self) -> DownloadManager:
        manager = DownloadManager(
            db=self.db,
            settings=self.settings,
            events=self.events,
            paths=AppPaths.under(self.tmp / "home"),
            resolver=self.resolver,  # type: ignore[arg-type]
            downloader_factory=lambda connections: make_downloader(self.http, self.limiter, connections=connections),
            rate_limiter=self.limiter,
            installer=self.installer,
            library=self.library,
            verify_archive=lambda path, token: self.extractor.test(path, token=token),
            poll_interval=0.05,
        )
        self.managers.append(manager)
        return manager

    def state(self, manager: DownloadManager, job_id: str, *states: JobState, timeout: float = 15.0):
        def reached():
            job = manager.get(job_id)
            return job if job is not None and job.state in states else None

        job = wait_until(reached, timeout, f"({states}; now {manager.get(job_id)})")
        Harness.settle(manager)
        return job


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    monkeypatch.setattr(shortcuts_module, "_desktop_dir", lambda: tmp_path / "Desktop")
    monkeypatch.setattr(shortcuts_module, "_start_menu_dir", lambda: tmp_path / "StartMenu")
    library_root = tmp_path / "Games"
    library_root.mkdir()
    events = EventBus()
    recorder = EventRecorder(events)
    settings = SettingsStore(tmp_path / "config.json", events)
    settings.update(
        library_dirs=[str(library_root)],
        default_library=str(library_root),
        create_desktop_shortcut=False,
        create_start_menu_shortcut=False,
        verify_archive_before_install=True,
    )
    db = Database(tmp_path / "anker.db")
    server = FileServer(_game_zip(), name="My%20Game.zip")
    http = SessionHttp()
    shortcuts = ShortcutService()
    extractor = Extractor(lambda: settings.get().seven_zip_path)
    world = _World(
        tmp=tmp_path,
        server=server,
        http=http,
        db=db,
        events=events,
        recorder=recorder,
        settings=settings,
        library=LibraryService(db, settings, events, shortcuts, None),
        installer=Installer(settings, extractor, shortcuts),
        extractor=extractor,
        limiter=RateLimiter(0),
        resolver=_StubResolver(server.link(filename="My Game.zip")),
        library_root=library_root,
        managers=[],
    )
    yield world
    server.release.set()
    for manager in world.managers:
        manager.shutdown(timeout=10)
    server.close()
    http.close()
    db.close()


def test_download_verify_install_register_end_to_end(world: _World) -> None:
    manager = world.manager()
    manager.start()
    job = manager.enqueue(slug="my-game", title="My Game", option=OPTION, version="v1.0")
    done = world.state(manager, job.id, JobState.COMPLETED, JobState.FAILED)
    assert done.state is JobState.COMPLETED, f"{done.error} ({done.error_kind})"

    install_dir = world.library_root / "My Game"
    assert done.install_path == str(install_dir)
    assert (install_dir / "MyGame.exe").is_file()
    assert (install_dir / "data" / "level1.pak").stat().st_size == 600 * KIB
    assert (install_dir / MANIFEST_FILENAME).is_file()
    assert not (install_dir / "Read Me.txt").exists()  # junk removed by the installer
    # archive deleted after install, and the job's download folder with it
    assert not os.path.exists(os.path.dirname(done.archive_path))
    game = world.library.find_by_slug("my-game")
    assert game is not None and game.path == str(install_dir) and game.executable == "MyGame.exe"
    wait_until(lambda: world.recorder.of_type(GameInstalled))
    event = world.recorder.of_type(GameInstalled)[0]
    assert event.install_id == game.install_id and event.needs_executable is False
    assert not (world.tmp / "Desktop").exists() and not (world.tmp / "StartMenu").exists()


def test_pause_resume_and_restart_continue_the_real_partial_file(world: _World) -> None:
    world.server.throttle_bps = 256 * KIB  # ~3 s for the whole archive
    world.settings.update(connections_per_download=1)  # one segment: any ranged start > 0 is a resume
    total = len(world.server.data)
    first = world.manager()
    first.start()
    job = first.enqueue(slug="my-game", title="My Game", option=OPTION)
    wait_until(lambda: (j := first.get(job.id)) and j.bytes_done >= 128 * KIB, 15, "(first bytes)")
    first.pause(job.id)
    paused = world.state(first, job.id, JobState.PAUSED)
    assert 0 < paused.bytes_done < total
    assert os.path.isfile(paused.archive_path + ".part")

    first.resume(job.id)
    wait_until(lambda: (j := first.get(job.id)) and j.state is JobState.DOWNLOADING
               and j.bytes_done > paused.bytes_done, 15, "(resumed)")
    first.shutdown(timeout=10)  # app exit mid-download
    stored = first.get(job.id)
    assert stored.state is JobState.PAUSED and stored.bytes_done < total

    world.server.throttle_bps = None
    second = world.manager()
    second.start()  # auto-resume: paused by shutdown
    done = world.state(second, job.id, JobState.COMPLETED, JobState.FAILED)
    assert done.state is JobState.COMPLETED, f"{done.error} ({done.error_kind})"
    starts = world.server.ranged_starts()
    assert starts.count(0) <= 1, starts  # never restarted from scratch
    assert len([s for s in starts if s > 0]) >= 2, starts  # resumed after the pause and after the restart
    # the extracted payload matches the served bytes (7-Zip/zip CRCs checked the whole archive)
    assert (world.library_root / "My Game" / "data" / "level1.pak").read_bytes() == random_bytes(600 * KIB, seed=4)
    assert world.resolver.calls == 1  # the stored link was reused across pause and restart


def test_cancel_with_real_engine_deletes_partial_files(world: _World) -> None:
    world.server.throttle_bps = 128 * KIB
    manager = world.manager()
    manager.start()
    job = manager.enqueue(slug="my-game", title="My Game", option=OPTION)
    wait_until(lambda: (j := manager.get(job.id)) and j.bytes_done > 0 and j.archive_path, 15)
    folder = os.path.dirname(manager.get(job.id).archive_path)
    assert os.path.isdir(folder)
    threading.Event().wait(0.3)
    manager.cancel(job.id)
    world.state(manager, job.id, JobState.CANCELLED)
    assert not os.path.exists(folder)
    assert not (world.library_root / "My Game").exists()
