"""DownloadManager: the download → install pipeline and its error policy."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from anker_client.core.errors import (
    AnkerError,
    CorruptArchiveError,
    DiskSpaceError,
    ExternalHostError,
    LinkExpiredError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    SevenZipNotFoundError,
    VerificationTimeout,
)
from anker_client.core.events import GameInstalled, JobAdded, JobUpdated, Notification
from anker_client.core.models import DownloadKind, DownloadOption, ErrorKind, JobState, ResolvedLink
from tests.unit.test_manager_fakes import (
    DEFAULT_SIZE,
    FakeClock,
    Harness,
    enqueue,
    make_harness,
    wait_until,
)


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    harness = make_harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def _started(h: Harness, **kw):
    manager = h.make_manager(**kw)
    manager.start()
    return manager


# --- happy path ------------------------------------------------------------------------------


def test_full_pipeline_downloads_installs_and_registers(h: Harness) -> None:
    manager = _started(h)
    job = enqueue(manager, cover_url="c.jpg", version="v1.5", source_updated_date="2026-01-02", genres=["RPG"])
    done = h.wait_state(manager, job.id, JobState.COMPLETED)

    expected_dir = os.path.join(h.library_root, ".ankerclient", "downloads", job.id)
    assert done.install_path == os.path.join(h.library_root, "Hollow Knight")
    assert done.archive_path == os.path.join(expected_dir, "game.zip")
    assert done.bytes_done == done.bytes_total == DEFAULT_SIZE
    assert done.progress == 1.0
    assert done.error == "" and done.error_kind is None
    assert done.completed_at and done.started_at
    # downloader got the configured connection count and the resolved link
    assert h.script.calls[0].connections == 4
    assert h.script.calls[0].url == h.resolver.link.url
    # installer request carries the job's metadata; archive was deleted → job dir cleaned
    request = h.installer.calls[0].request
    assert h.installer.calls[0].keep_archive is False
    assert (request.slug, request.title, request.version) == ("hollow-knight", "Hollow Knight", "v1.5")
    assert request.cover_url == "c.jpg" and request.genres == ["RPG"]
    assert request.existing_install_path == ""
    assert not os.path.exists(expected_dir)
    assert len(h.library.registered) == 1
    # disk-space pre-check ran with the link size
    assert any(c["size"] == DEFAULT_SIZE for c in h.space.calls)


def test_full_pipeline_event_order(h: Harness) -> None:
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    wait_until(lambda: h.recorder.of_type(Notification))

    events = h.recorder.snapshot()
    assert isinstance(next(e for e in events if type(e).__name__ != "QueueChanged"), JobAdded)
    states = h.recorder.job_states(job.id)
    expected = [JobState.RESOLVING, JobState.DOWNLOADING, JobState.EXTRACTING, JobState.INSTALLING, JobState.COMPLETED]
    assert states == expected
    installed_index = next(i for i, e in enumerate(events) if isinstance(e, GameInstalled))
    completed_index = next(
        i for i, e in enumerate(events) if isinstance(e, JobUpdated) and e.job.state is JobState.COMPLETED
    )
    assert completed_index < installed_index
    game_event = events[installed_index]
    assert game_event == GameInstalled(install_id="hollow-knight", title="Hollow Knight", needs_executable=False,
                                       is_update=False)
    note = h.recorder.of_type(Notification)[-1]
    assert note.level == "success" and note.tray and "Hollow Knight" in note.message


def test_needs_executable_when_installer_cannot_pick_one(h: Harness) -> None:
    h.installer.executable = ""
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    wait_until(lambda: h.recorder.of_type(GameInstalled))
    assert h.recorder.of_type(GameInstalled)[0].needs_executable is True


def test_custom_download_dir_and_filename_fallback(h: Harness) -> None:
    downloads = h.tmp / "dl"
    h.settings.update(download_dir=str(downloads))
    h.resolver.link = ResolvedLink(url="https://cdn.example.test/x", filename="", size=DEFAULT_SIZE)
    h.installer.delete_archive = False
    manager = _started(h)
    job = enqueue(manager, slug="dead-cells")
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.archive_path == os.path.join(str(downloads), job.id, "dead-cells.zip")
    assert done.filename == "dead-cells.zip"
    assert os.path.isfile(done.archive_path)  # kept: the installer did not delete it


def test_unsafe_server_filename_is_sanitised(h: Harness) -> None:
    h.resolver.link = ResolvedLink(url="https://cdn.example.test/x", filename='..\\evil:name?.zip', size=DEFAULT_SIZE)
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert os.path.dirname(done.archive_path).endswith(job.id)
    assert done.filename == "evil name .zip"


def test_auto_install_off_stops_after_download_then_install(h: Harness) -> None:
    h.settings.update(auto_install=False)
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.install_path == ""
    assert os.path.isfile(done.archive_path)
    assert h.installer.call_count == 0
    wait_until(lambda: any(n.title == "Download complete" for n in h.recorder.of_type(Notification)))

    manager.install(job.id)
    wait_until(lambda: (j := manager.get(job.id)) and j.state is JobState.COMPLETED and j.install_path)
    assert h.installer.call_count == 1
    assert h.script.call_count == 1  # not downloaded again
    assert h.resolver.call_count == 1


def test_install_with_missing_archive_fails_with_clear_message(h: Harness) -> None:
    h.settings.update(auto_install=False)
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    os.remove(done.archive_path)
    manager.install(job.id)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "no longer exists" in failed.error
    manager.retry(job.id)  # downloads again
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.call_count == 2


def test_import_archive_installs_and_never_deletes_source(h: Harness) -> None:
    archive = h.tmp / "My Game.zip"
    archive.write_bytes(b"PK" + b"x" * 100)
    manager = _started(h)
    job = manager.import_archive(str(archive), slug="my-game", title="My Game")
    assert job.imported_archive and job.option.label == "Imported archive"
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.install_path
    assert archive.exists()
    assert h.installer.calls[0].keep_archive is True
    assert h.installer.calls[0].request.archive_path == str(archive)
    assert h.resolver.call_count == 0 and h.script.call_count == 0
    manager.remove(job.id)
    assert archive.exists()


def test_import_archive_missing_file_raises(h: Harness) -> None:
    manager = h.make_manager()
    with pytest.raises(AnkerError):
        manager.import_archive(str(h.tmp / "nope.zip"), slug="x", title="X")


def test_cancel_imported_job_keeps_archive(h: Harness) -> None:
    archive = h.tmp / "a.7z"
    archive.write_bytes(b"7z" + b"x" * 10)
    h.installer.gate = threading.Event()
    manager = _started(h)
    job = manager.import_archive(str(archive), slug="a", title="A")
    h.wait_state(manager, job.id, JobState.EXTRACTING)
    wait_until(lambda: h.installer.call_count == 1)
    manager.cancel(job.id)
    h.wait_state(manager, job.id, JobState.CANCELLED)
    assert archive.exists()


# --- patches / add-ons / reinstalls -------------------------------------------------------------


def test_patch_targets_existing_install(h: Harness, tmp_path: Path) -> None:
    other_root = tmp_path / "OtherLib"
    base = other_root / "Hollow Knight"
    base.mkdir(parents=True)
    h.library.add("hollow-knight", str(base))
    patch = DownloadOption(202, "Update Only From V 1.0 To V 1.1", DownloadKind.PATCH, from_version="1.0",
                           to_version="1.1")
    manager = _started(h)
    job = enqueue(manager, option=patch, version="v1.0")
    assert job.library_root == str(other_root)  # follows the installed game's library
    h.wait_state(manager, job.id, JobState.COMPLETED)
    request = h.installer.calls[0].request
    assert request.existing_install_path == str(base)
    assert request.library_root == str(other_root)
    assert request.version == "1.1"
    wait_until(lambda: h.recorder.of_type(GameInstalled))
    assert h.recorder.of_type(GameInstalled)[0].is_update is True
    assert manager.get(job.id).status_text == "Updated"


def test_addon_without_base_game_fails_before_downloading(h: Harness) -> None:
    addon = DownloadOption(303, "Language Pack", DownloadKind.ADDON)
    manager = _started(h)
    job = enqueue(manager, option=addon)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "Install Hollow Knight first" in failed.error
    assert failed.error_kind is ErrorKind.INSTALL
    assert h.resolver.call_count == 0 and h.script.call_count == 0


def test_full_reinstall_of_managed_install_passes_existing_path(h: Harness) -> None:
    existing = Path(h.library_root) / "HK"
    existing.mkdir()
    h.library.add("hollow-knight", str(existing), library_root=h.library_root)
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.installer.calls[0].request.existing_install_path == str(existing)
    wait_until(lambda: h.recorder.of_type(GameInstalled))
    assert h.recorder.of_type(GameInstalled)[0].is_update is True


def test_full_install_never_targets_unmanaged_folder(h: Harness) -> None:
    folder = Path(h.library_root) / "Hollow Knight (old)"
    folder.mkdir()
    h.library.add("hollow-knight", str(folder), managed=False, library_root=h.library_root)
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.installer.calls[0].request.existing_install_path == ""


# --- link expiry ----------------------------------------------------------------------------------


def test_link_expired_mid_download_re_resolves_once_and_resumes_partial(h: Harness) -> None:
    h.script.actions = [LinkExpiredError(), "ok"]
    h.resolver.script = [h.resolver.link, ResolvedLink(url="https://cdn.example.test/new", filename="other.zip",
                                                      size=DEFAULT_SIZE)]
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.resolver.call_count == 2
    first, second = h.script.calls
    assert first.dest == second.dest  # same partial file, original name kept
    assert second.start_offset > 0
    assert second.url == "https://cdn.example.test/new"
    assert done.filename == "game.zip"


def test_link_expiring_again_fails(h: Harness) -> None:
    h.script.actions = [LinkExpiredError(), LinkExpiredError()]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.LINK_EXPIRED
    assert h.resolver.call_count == 2
    note = h.recorder.of_type(Notification)[-1]
    assert note.level == "error" and note.title == "Download failed"


# --- failures --------------------------------------------------------------------------------------


def test_disk_space_failure_fails_before_download(h: Harness) -> None:
    h.space.error = DiskSpaceError(10**12, 10, h.library_root)
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.DISK_SPACE
    assert "Not enough disk space" in failed.error
    assert h.resolver.call_count == 0  # option size "4 KB" was known: checked before resolving
    assert h.script.call_count == 0


def test_disk_space_checked_after_resolve_with_partial_credit(h: Harness) -> None:
    manager = _started(h)
    job = enqueue(manager, option=DownloadOption(9, "Direct"))  # no size known up front
    h.wait_state(manager, job.id, JobState.COMPLETED)
    sizes = [c["size"] for c in h.space.calls]
    assert DEFAULT_SIZE in sizes
    install_check = h.space.calls[-1]
    assert install_check["already"] == DEFAULT_SIZE  # archive already on disk when installing


def test_external_host_fails_with_error_url(h: Harness) -> None:
    h.resolver.script = [ExternalHostError("https://ankergames.net/download/abc", "MegaUp")]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.EXTERNAL_HOST
    assert failed.error_url == "https://ankergames.net/download/abc"
    assert "MegaUp" in failed.error
    assert failed.retry_at is None


def test_verification_error_fails_with_ticket_url_even_if_retryable(h: Harness) -> None:
    h.resolver.script = [VerificationTimeout(ticket_url="https://ankergames.net/download/t")]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.VERIFICATION
    assert failed.error_url == "https://ankergames.net/download/t"


def test_non_retryable_error_fails_immediately(h: Harness) -> None:
    h.resolver.script = [NotFoundError()]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.NOT_FOUND and failed.attempts == 0


def test_unexpected_exception_becomes_generic_failure(h: Harness) -> None:
    h.resolver.script = [RuntimeError("boom")]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.UNKNOWN
    assert "See the log" in failed.error


def test_rate_limited_waits_until_retry_at(h: Harness) -> None:
    h.resolver.script = [RateLimitedError(42)]
    manager = _started(h)
    job = enqueue(manager)
    waiting = h.wait_state(manager, job.id, JobState.WAITING)
    assert waiting.retry_at == pytest.approx(h.clock() + 42)
    assert waiting.error_kind is ErrorKind.RATE_LIMITED
    assert waiting.attempts == 0  # rate limits do not use up retries
    assert "Rate limited" in waiting.status_text
    # not started before retry_at, even though the scheduler keeps polling
    threading.Event().wait(0.15)
    assert manager.get(job.id).state is JobState.WAITING
    h.clock.advance(42)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.resolver.call_count == 2


def test_retryable_errors_back_off_then_fail(h: Harness) -> None:
    h.resolver.script = [NetworkError(), NetworkError(), NetworkError(), NetworkError()]
    manager = _started(h)
    job = enqueue(manager)
    for attempt, delay in enumerate((30, 120, 600), start=1):
        waiting = wait_until(
            lambda a=attempt: (j := manager.get(job.id)) and j.state is JobState.WAITING and j.attempts == a and j
        )
        assert waiting.retry_at == pytest.approx(h.clock() + delay)
        assert waiting.status_text.startswith(f"Retry {attempt} of 3")
        assert waiting.error_kind is ErrorKind.NETWORK
        threading.Event().wait(0.05)
        assert manager.get(job.id).state is JobState.WAITING
        h.clock.advance(delay)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.attempts == 3
    assert h.resolver.call_count == 4


def test_resume_of_waiting_job_skips_the_wait(h: Harness) -> None:
    h.resolver.script = [NetworkError()]
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.WAITING)
    manager.resume(job.id)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.attempts == 0


def test_corrupt_downloaded_archive_is_discarded_and_downloaded_again(h: Harness) -> None:
    h.installer.actions = [CorruptArchiveError()]
    manager = _started(h)
    job = enqueue(manager)
    waiting = h.wait_state(manager, job.id, JobState.WAITING)
    assert waiting.error_kind is ErrorKind.EXTRACTION
    assert waiting.archive_path == "" and waiting.bytes_done == 0
    assert not os.path.exists(os.path.join(h.library_root, ".ankerclient", "downloads", job.id))
    h.clock.advance(30)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.call_count == 2 and h.installer.call_count == 2


def test_install_failure_keeps_archive_and_retry_installs_only(h: Harness) -> None:
    h.installer.actions = [SevenZipNotFoundError()]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert os.path.isfile(failed.archive_path)
    assert h.recorder.of_type(Notification)[-1].title == "Installation failed"
    manager.retry(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.call_count == 1 and h.resolver.call_count == 1
    assert h.installer.call_count == 2


def test_verify_step_runs_only_when_enabled(h: Harness) -> None:
    manager = _started(h)
    job = enqueue(manager, slug="a")
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.verify_calls == []
    h.settings.update(verify_archive_before_install=True)
    job2 = enqueue(manager, slug="b")
    done = h.wait_state(manager, job2.id, JobState.COMPLETED)
    assert h.verify_calls == [os.path.join(os.path.dirname(h.installer.calls[1].request.archive_path), "game.zip")]
    assert done.state is JobState.COMPLETED


def test_verify_failure_on_corrupt_archive(h: Harness) -> None:
    def bad_verify(path: str, token) -> None:
        raise CorruptArchiveError()

    h.settings.update(verify_archive_before_install=True)
    manager = _started(h, verify_archive=bad_verify)
    job = enqueue(manager)
    waiting = h.wait_state(manager, job.id, JobState.WAITING)
    assert waiting.error_kind is ErrorKind.EXTRACTION
    assert h.installer.call_count == 0


def test_installs_are_serialised(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = _started(h)
    archives = []
    for name in ("a", "b", "c"):
        archive = h.tmp / f"{name}.zip"
        archive.write_bytes(b"PK")
        archives.append(manager.import_archive(str(archive), slug=name, title=name.upper()))
    wait_until(lambda: h.installer.call_count == 1)

    def others_waiting() -> bool:
        # Wait for the state instead of sleeping a fixed time: on a busy machine the other
        # workers can take a while to reach the install lock.
        waiting = [manager.get(j.id) for j in archives[1:]]
        return all(j.state is JobState.EXTRACTING for j in waiting) and any(
            "another installation" in j.status_text for j in waiting
        )

    wait_until(others_waiting)
    assert h.installer.call_count == 1  # the others are blocked behind the running install
    h.installer.gate.set()
    for job in archives:
        h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.installer.max_active == 1


def test_installing_job_frees_its_download_slot(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = _started(h)  # max_concurrent_downloads == 1
    first = enqueue(manager, slug="a")
    h.wait_state(manager, first.id, JobState.EXTRACTING)
    second = enqueue(manager, slug="b")
    h.wait_state(manager, second.id, JobState.EXTRACTING)  # downloaded while the first one installs
    h.installer.gate.set()
    h.wait_state(manager, first.id, JobState.COMPLETED)
    h.wait_state(manager, second.id, JobState.COMPLETED)


def test_register_install_failure_still_completes(h: Harness) -> None:
    def broken(result, request):
        raise RuntimeError("db gone")

    h.library.register_install = broken  # type: ignore[method-assign]
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.install_path
    wait_until(lambda: h.recorder.of_type(GameInstalled))


# --- throttling --------------------------------------------------------------------------------


def test_progress_events_are_throttled_and_flushed(h: Harness) -> None:
    mono = FakeClock(1000.0)
    h.script.default_action = "stall"
    h.script.steps = 64
    manager = _started(h, monotonic=mono)
    job = enqueue(manager)
    wait_until(lambda: h.script.blocked == 1)
    threading.Event().wait(0.1)
    updates = [e.job for e in h.recorder.of_type(JobUpdated) if e.job.state is JobState.DOWNLOADING]
    # frozen clock: only the state change got through, ~32 progress callbacks were throttled
    assert len(updates) == 1 and updates[0].bytes_done == 0
    assert h.db_job(job.id)["bytes_done"] == 0
    assert manager.get(job.id).bytes_done == DEFAULT_SIZE // 2

    mono.advance(0.3)  # event window passed: the scheduler publishes the owed update
    wait_until(lambda: any(e.job.bytes_done == DEFAULT_SIZE // 2 for e in h.recorder.of_type(JobUpdated)))
    assert h.db_job(job.id)["bytes_done"] == 0  # persistence window (2 s) not passed yet
    mono.advance(2.0)
    wait_until(lambda: h.db_job(job.id)["bytes_done"] == DEFAULT_SIZE // 2)
    count = len(h.recorder.of_type(JobUpdated))
    threading.Event().wait(0.1)
    assert len(h.recorder.of_type(JobUpdated)) == count  # nothing owed → nothing re-sent
    h.script.release.set()
    h.wait_state(manager, job.id, JobState.COMPLETED)


def test_progress_events_at_most_four_per_second(h: Harness) -> None:
    import time

    h.script.steps = 2000
    manager = _started(h)
    started = time.monotonic()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    elapsed = time.monotonic() - started
    stamps = [e.job.bytes_done for e in h.recorder.of_type(JobUpdated)
              if e.job.id == job.id and e.job.state is JobState.DOWNLOADING]
    # 2000 progress callbacks: the state change plus at most one event per 250 ms window
    # (bounded by the real elapsed time, so a slow/loaded machine cannot make this flaky).
    assert 1 <= len(stamps) <= 2 + int(elapsed / 0.25)
    assert len(stamps) < 100


# --- with the real LinkResolver ------------------------------------------------------------------


def test_real_resolver_with_browser_verification(h: Harness) -> None:
    from anker_client.core.models import TicketPage
    from anker_client.services.downloads.resolver import LinkResolver
    from tests.unit.test_resolver import TICKET, FakeClient, FakeVerifier

    client = FakeClient(TicketPage(ticket_url=TICKET, file_url="https://ankergames.net/download-file/t",
                                   requires_verification=True))
    verifier = FakeVerifier()
    resolver = LinkResolver(client, verifier, verification_timeout=lambda: 99.0)
    manager = _started(h, resolver=resolver)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert verifier.requests[0].job_id == job.id and verifier.requests[0].timeout_seconds == 99.0
    assert done.filename == "Real Name.zip"
    assert done.bytes_total == client.probe_link.size
    states = h.recorder.job_states(job.id)
    assert states[:3] == [JobState.RESOLVING, JobState.VERIFYING, JobState.RESOLVING]
    assert states[-1] is JobState.COMPLETED


def test_real_resolver_without_verifier_fails_with_ticket_url(h: Harness) -> None:
    from anker_client.core.models import TicketPage
    from anker_client.services.downloads.resolver import LinkResolver
    from tests.unit.test_resolver import TICKET, FakeClient

    client = FakeClient(TicketPage(ticket_url=TICKET, file_url="f", requires_verification=True))
    manager = _started(h, resolver=LinkResolver(client))
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.VERIFICATION
    assert failed.error_url == TICKET
    assert "Open it in your browser" in failed.error
