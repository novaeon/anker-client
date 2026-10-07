"""DownloadManager regressions found in review: races between stop requests and the
worker's outcome, retry-budget loops, scheduler/worker robustness, edge cases."""

from __future__ import annotations

import logging
import os
import threading
import types
from collections.abc import Callable
from pathlib import Path

import pytest

import anker_client.services.downloads.manager as manager_module
from anker_client.core.errors import (
    CorruptArchiveError,
    LinkExpiredError,
    NetworkError,
    SevenZipNotFoundError,
)
from anker_client.core.events import JobAdded
from anker_client.core.models import ErrorKind, JobState, ResolvedLink
from anker_client.services.downloads._manager_state import EXTRA_KEY
from anker_client.services.downloads.manager import DownloadManager
from tests.unit.test_manager_fakes import DEFAULT_SIZE, Harness, enqueue, make_harness, wait_until


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    harness = make_harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def _started(h: Harness, **kw) -> DownloadManager:
    manager = h.make_manager(**kw)
    manager.start()
    return manager


def _job_dir(h: Harness, job_id: str) -> str:
    return os.path.join(h.library_root, ".ankerclient", "downloads", job_id)


class _OnLog(logging.Handler):
    """Runs ``action`` once, synchronously on the logging thread, for the first matching record."""

    def __init__(self, predicate: Callable[[str], bool], action: Callable[[], None]) -> None:
        super().__init__(logging.DEBUG)
        self._predicate = predicate
        self._action = action
        self.fired = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if not self.fired.is_set() and self._predicate(record.getMessage()):
            self.fired.set()
            self._action()


@pytest.fixture
def on_manager_log():
    logger = logging.getLogger(manager_module.__name__)
    installed: list[logging.Handler] = []
    old_level = logger.level

    def install(predicate: Callable[[str], bool], action: Callable[[], None]) -> _OnLog:
        handler = _OnLog(predicate, action)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        installed.append(handler)
        return handler

    yield install
    for handler in installed:
        logger.removeHandler(handler)
    logger.setLevel(old_level)


# --- retry budget ---------------------------------------------------------------------------------


def test_archive_that_is_always_corrupt_eventually_fails(h: Harness) -> None:
    """A successful download used to reset ``attempts``: a corrupt archive looped forever."""
    h.installer.actions = [CorruptArchiveError() for _ in range(10)]
    manager = _started(h)
    job = enqueue(manager)
    for attempt, delay in enumerate((30, 120, 600), start=1):
        wait_until(
            lambda a=attempt: (j := manager.get(job.id)) and j.state is JobState.WAITING and j.attempts == a,
            message=f"(retry {attempt})",
        )
        h.clock.advance(delay)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.error_kind is ErrorKind.EXTRACTION
    assert h.script.call_count == 4 and h.installer.call_count == 4
    assert not os.path.exists(_job_dir(h, job.id))  # the last damaged copy is gone too


def test_damaged_imported_archive_fails_at_once_and_is_kept(h: Harness) -> None:
    archive = h.tmp / "Mine.zip"
    archive.write_bytes(b"PK" + b"x" * 64)
    h.installer.actions = [CorruptArchiveError()]
    manager = _started(h)
    job = manager.import_archive(str(archive), slug="mine", title="Mine")
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.retry_at is None and failed.error_kind is ErrorKind.EXTRACTION
    assert "import" in failed.error
    assert archive.exists()
    assert h.installer.call_count == 1


def test_install_of_vanished_imported_archive_says_import_again(h: Harness) -> None:
    archive = h.tmp / "Gone.zip"
    archive.write_bytes(b"PK")
    h.installer.actions = [SevenZipNotFoundError()]
    manager = _started(h)
    job = manager.import_archive(str(archive), slug="gone", title="Gone")
    h.wait_state(manager, job.id, JobState.FAILED)
    archive.unlink()
    manager.install(job.id)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "Import it again" in failed.error


# --- link expiry ------------------------------------------------------------------------------------


def test_link_expired_while_resolving_is_retried(h: Harness) -> None:
    h.resolver.script = [LinkExpiredError()]
    manager = _started(h)
    job = enqueue(manager)
    waiting = h.wait_state(manager, job.id, JobState.WAITING)
    assert waiting.error_kind is ErrorKind.LINK_EXPIRED and waiting.attempts == 1
    h.clock.advance(30)
    h.wait_state(manager, job.id, JobState.COMPLETED)


def test_link_expiring_again_after_re_resolve_fails_with_clear_message(h: Harness) -> None:
    h.script.actions = [LinkExpiredError(), LinkExpiredError()]
    manager = _started(h)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "keeps expiring" in failed.error and failed.retry_at is None
    assert failed.error_kind is ErrorKind.LINK_EXPIRED
    assert h.resolver.call_count == 2
    assert os.path.isfile(failed.archive_path + ".part")  # partial kept for a manual retry


# --- stop requests racing the worker's outcome -------------------------------------------------------


def _failing_download(h: Harness, manager: DownloadManager, on_manager_log, command: str):
    """The download raises a retryable error; ``command`` lands while the worker handles it."""
    h.script.actions = [NetworkError()]
    job_holder: dict[str, str] = {}

    def is_failure_record(message: str) -> bool:
        return "failed:" in message and job_holder.get("id", "?") in message

    hook = on_manager_log(is_failure_record, lambda: getattr(manager, command)(job_holder["id"]))
    job = enqueue(manager)
    job_holder["id"] = job.id
    manager.start()  # only now: the hook must know the id before the worker can fail
    return job, hook


def test_pause_landing_while_worker_handles_an_error_wins(h: Harness, on_manager_log) -> None:
    manager = h.make_manager()
    job, hook = _failing_download(h, manager, on_manager_log, "pause")
    paused = h.wait_state(manager, job.id, JobState.PAUSED, JobState.WAITING)
    assert hook.fired.is_set()
    assert paused.state is JobState.PAUSED, "the user's pause was overwritten by the automatic retry"
    assert paused.retry_at is None
    assert h.db_job(job.id)[EXTRA_KEY]["pause_reason"] == "user"
    h.clock.advance(3600)
    threading.Event().wait(0.1)
    assert manager.get(job.id).state is JobState.PAUSED  # never retried on its own


def test_cancel_landing_while_worker_handles_an_error_wins(h: Harness, on_manager_log) -> None:
    manager = h.make_manager()
    job, hook = _failing_download(h, manager, on_manager_log, "cancel")
    done = h.wait_state(manager, job.id, JobState.CANCELLED, JobState.WAITING)
    assert hook.fired.is_set()
    assert done.state is JobState.CANCELLED, "the user's cancel was lost"
    assert not os.path.exists(_job_dir(h, job.id))


def test_pause_resume_storm_never_runs_a_job_twice(h: Harness) -> None:
    h.script.default_action = "block"
    manager = _started(h)
    job = enqueue(manager)
    wait_until(lambda: h.script.blocked == 1)
    for _ in range(40):
        manager.pause(job.id)
        manager.resume(job.id)
    h.script.default_action = "ok"
    h.script.release.set()
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.max_active == 1
    assert h.installer.call_count == 1
    assert {c.dest for c in h.script.calls} == {h.script.calls[0].dest}


# --- robustness ------------------------------------------------------------------------------------


def test_scheduler_survives_a_failing_pass(h: Harness) -> None:
    manager = h.make_manager()
    original = manager._start_eligible_locked
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scheduler bug")
        original()

    manager._start_eligible_locked = flaky  # type: ignore[method-assign]
    job = enqueue(manager)
    manager.start()
    h.wait_state(manager, job.id, JobState.COMPLETED, timeout=8)
    assert calls["n"] >= 2


def test_worker_thread_that_cannot_start_fails_the_job(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _started(h)  # the scheduler itself starts normally

    class NoThreads(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    proxy = types.SimpleNamespace(**{n: getattr(threading, n) for n in dir(threading) if not n.startswith("__")})
    proxy.Thread = NoThreads
    monkeypatch.setattr(manager_module, "threading", proxy)
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "could not be started" in failed.error
    assert manager.active_jobs() == []

    monkeypatch.setattr(manager_module, "threading", threading)
    manager.retry(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)


def test_job_is_never_left_running_without_a_worker(h: Harness) -> None:
    h.resolver.script = [NetworkError()]
    manager = h.make_manager()

    def broken(*_args) -> None:
        raise RuntimeError("bug in outcome handling")

    manager._handle_stop = broken  # type: ignore[method-assign]
    manager.start()
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert "stopped unexpectedly" in failed.error
    assert not [t for t in threading.enumerate() if t.name == f"anker-dl-{job.id[:8]}"]


def test_install_only_job_whose_archive_vanished_counts_as_a_download(h: Harness) -> None:
    h.installer.actions = [SevenZipNotFoundError()]
    manager = _started(h)  # max_concurrent_downloads == 1
    first = enqueue(manager, slug="a")
    failed = h.wait_state(manager, first.id, JobState.FAILED)
    os.remove(failed.archive_path)
    original = manager._archive_ready_locked

    def scheduler_still_sees_the_archive(job) -> bool:
        # The archive disappears between the scheduler's check and the worker's.
        if job.id == first.id and threading.current_thread().name == "anker-dl-scheduler":
            return True
        return original(job)

    manager._archive_ready_locked = scheduler_still_sees_the_archive  # type: ignore[method-assign]
    h.script.default_action = "block"
    manager.retry(first.id)
    wait_until(lambda: h.script.blocked == 1)
    second = enqueue(manager, slug="b")
    threading.Event().wait(0.15)
    assert manager.get(second.id).state is JobState.QUEUED
    assert h.script.max_active == 1
    h.script.release.set()
    h.wait_state(manager, first.id, JobState.COMPLETED)
    h.wait_state(manager, second.id, JobState.COMPLETED)


# --- cancellation at the install lock -------------------------------------------------------------


def test_cancel_while_waiting_for_another_install(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = _started(h)
    first = enqueue(manager, slug="a")
    wait_until(lambda: h.installer.call_count == 1)
    second = enqueue(manager, slug="b")
    wait_until(lambda: (j := manager.get(second.id)) and "another installation" in j.status_text)
    manager.cancel(second.id)
    h.wait_state(manager, second.id, JobState.CANCELLED)
    assert not os.path.exists(_job_dir(h, second.id))
    h.installer.gate.set()
    h.wait_state(manager, first.id, JobState.COMPLETED)
    assert h.installer.call_count == 1


def test_pause_while_waiting_for_another_install_then_resume(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = _started(h)
    first = enqueue(manager, slug="a")
    wait_until(lambda: h.installer.call_count == 1)
    second = enqueue(manager, slug="b")
    wait_until(lambda: (j := manager.get(second.id)) and "another installation" in j.status_text)
    manager.pause(second.id)
    paused = h.wait_state(manager, second.id, JobState.PAUSED)
    assert os.path.isfile(paused.archive_path)
    h.installer.gate.set()
    h.wait_state(manager, first.id, JobState.COMPLETED)
    manager.resume(second.id)
    h.wait_state(manager, second.id, JobState.COMPLETED)
    assert h.script.call_count == 2  # the paused job was not downloaded again


# --- edge cases -----------------------------------------------------------------------------------


def test_concurrent_enqueues_of_the_same_option_create_one_job(h: Harness) -> None:
    manager = h.make_manager()
    barrier = threading.Barrier(8)
    ids: list[str] = []
    lock = threading.Lock()

    def add() -> None:
        barrier.wait()
        job_id = enqueue(manager).id
        with lock:
            ids.append(job_id)

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == 8 and len(set(ids)) == 1
    assert len(manager.jobs()) == 1
    assert len(h.recorder.of_type(JobAdded)) == 1


def test_unicode_title_and_filename_round_trip(h: Harness) -> None:
    h.resolver.link = ResolvedLink(url="https://cdn.example.test/u", filename="Ünïcödé — 日本語.7z", size=DEFAULT_SIZE)
    h.settings.update(auto_install=False)
    first = _started(h)
    job = enqueue(first, slug="unicode", title="Ünïcödé: Gämé?")
    done = h.wait_state(first, job.id, JobState.COMPLETED)
    assert done.filename == "Ünïcödé — 日本語.7z"
    assert os.path.isfile(done.archive_path)
    first.shutdown(timeout=5)
    second = h.make_manager()
    reloaded = second.get(job.id)
    assert reloaded is not None and reloaded.title == "Ünïcödé: Gämé?"
    assert reloaded.archive_path == done.archive_path


def test_very_long_server_filename_keeps_extension(h: Harness) -> None:
    h.resolver.link = ResolvedLink(url="https://cdn.example.test/l", filename="x" * 400 + ".rar", size=DEFAULT_SIZE)
    h.installer.delete_archive = False
    manager = _started(h)
    job = enqueue(manager)
    done = h.wait_state(manager, job.id, JobState.COMPLETED)
    assert done.filename.endswith(".rar") and len(done.filename) <= 120
    assert os.path.isfile(done.archive_path)


def test_cancelled_straggler_is_not_resumed_next_start(h: Harness) -> None:
    """A worker that misses the shutdown deadline after a user cancel must not come back."""
    from anker_client.core.errors import OperationCancelled
    from tests.unit.test_manager_fakes import FakeDownloader

    entered, unstick = threading.Event(), threading.Event()

    class StuckDownloader(FakeDownloader):
        def download(self, link, dest_path, *, token, on_progress=None):  # ignores its token
            with open(dest_path + ".part", "wb") as handle:
                handle.write(b"x" * 10)
            entered.set()
            unstick.wait(10)
            raise OperationCancelled()

    first = _started(h, downloader_factory=lambda connections: StuckDownloader(h.script, connections))
    job = enqueue(first)
    assert entered.wait(5)
    first.cancel(job.id)
    first.shutdown(timeout=0.2)
    stored = h.db_job(job.id)
    assert stored["state"] == "cancelled"
    assert stored["archive_path"]  # kept so that removing the job deletes the leftovers

    second = _started(h)
    threading.Event().wait(0.1)
    assert second.get(job.id).state is JobState.CANCELLED
    assert h.resolver.call_count == 1
    assert os.path.isdir(_job_dir(h, job.id))
    second.remove(job.id)  # the leftovers go with the job
    assert not os.path.exists(_job_dir(h, job.id))
    unstick.set()
    for thread in threading.enumerate():
        if thread.name == f"anker-dl-{job.id[:8]}":
            thread.join(5)


def test_due_waiting_job_without_a_free_slot_does_not_spin_the_scheduler(h: Harness) -> None:
    from tests.unit.test_manager_persistence import _insert, _job

    h.script.default_action = "block"
    _insert(h, _job(h, "waiting", JobState.WAITING, position=5, retry_at=0.0))  # due long ago
    manager = h.make_manager(poll_interval=0.5)
    passes = {"n": 0}
    original = manager._start_eligible_locked

    def counting() -> None:
        passes["n"] += 1
        original()

    manager._start_eligible_locked = counting  # type: ignore[method-assign]
    running = enqueue(manager)
    manager.move(running.id, 0)
    manager.start()
    wait_until(lambda: h.script.blocked == 1)  # the only slot is taken; "waiting" is due but cannot start
    assert manager.get("waiting").state is JobState.WAITING
    before = passes["n"]
    threading.Event().wait(0.4)
    assert passes["n"] - before <= 4, f"scheduler woke {passes['n'] - before} times in 0.4 s"
    h.script.release.set()
    h.wait_state(manager, running.id, JobState.COMPLETED)
    h.wait_state(manager, "waiting", JobState.COMPLETED)


def test_job_for_empty_slug_ignores_slugless_imports(h: Harness) -> None:
    archive = h.tmp / "unknown.zip"
    archive.write_bytes(b"PK")
    manager = h.make_manager()
    job = manager.import_archive(str(archive), slug="", title="")
    assert job.title == "unknown"
    assert manager.job_for("") is None


def test_cancel_during_verification_leaves_nothing_behind(h: Harness) -> None:
    h.resolver.script = ["block"]
    manager = _started(h)
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.VERIFYING)
    manager.cancel(job.id)
    cancelled = h.wait_state(manager, job.id, JobState.CANCELLED)
    assert cancelled.archive_path == "" and cancelled.bytes_done == 0
    assert not os.path.exists(_job_dir(h, job.id))
    assert h.script.call_count == 0


def test_cancel_waiting_job_deletes_its_partial_data(h: Harness) -> None:
    h.script.actions = [NetworkError()]  # writes a few bytes, then fails → WAITING
    manager = _started(h)
    job = enqueue(manager)
    waiting = h.wait_state(manager, job.id, JobState.WAITING)
    assert os.path.isfile(waiting.archive_path + ".part")
    manager.cancel(job.id)
    assert manager.get(job.id).state is JobState.CANCELLED
    assert not os.path.exists(_job_dir(h, job.id))
    h.clock.advance(3600)
    threading.Event().wait(0.1)
    assert manager.get(job.id).state is JobState.CANCELLED  # never retried


def test_import_archive_with_empty_path_is_rejected(h: Harness) -> None:
    from anker_client.core.errors import InstallError

    manager = h.make_manager()
    with pytest.raises(InstallError):
        manager.import_archive("", slug="x", title="X")
    assert manager.jobs() == []


@pytest.mark.parametrize("running", [True, False])
def test_locked_files_on_cancel_are_deleted_by_a_later_remove(
    h: Harness, monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    """Deleting right after a cancel can fail on Windows (AV scan); the path must not be lost."""
    real_discard = manager_module.discard_job_files
    monkeypatch.setattr(manager_module, "discard_job_files", lambda job, **kw: False)  # "file in use"
    h.script.default_action = "block"
    manager = _started(h)
    job = enqueue(manager)
    wait_until(lambda: h.script.blocked == 1)
    if not running:
        manager.pause(job.id)
        h.wait_state(manager, job.id, JobState.PAUSED)
    manager.cancel(job.id)
    cancelled = h.wait_state(manager, job.id, JobState.CANCELLED)
    assert cancelled.bytes_done == 0
    assert cancelled.archive_path and os.path.isdir(_job_dir(h, job.id))
    assert h.db_job(job.id)["archive_path"] == cancelled.archive_path

    monkeypatch.setattr(manager_module, "discard_job_files", real_discard)  # the lock is gone
    manager.remove(job.id)
    assert not os.path.exists(_job_dir(h, job.id))


def test_one_malformed_row_does_not_stall_the_queue(h: Harness) -> None:
    import json

    from anker_client.core.models import DownloadJob, DownloadOption

    bad = DownloadJob(id="bad", slug="bad", title="Bad", option=DownloadOption(1, "Direct"),
                      library_root=h.library_root, state=JobState.WAITING).to_dict()
    bad["retry_at"] = "soon"  # would break every scheduler comparison
    bad["state"] = "waiting"
    h.db.execute(
        "INSERT INTO jobs(id, state, position, json, created_at, updated_at) VALUES('bad', 'waiting', 0, ?, '', '')",
        (json.dumps(bad),),
    )
    manager = _started(h)
    good = enqueue(manager, slug="good")
    h.wait_state(manager, good.id, JobState.COMPLETED)
    assert manager.get("bad") is None
