"""DownloadManager: user commands, queue order, concurrency and live settings."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from anker_client.core.errors import NetworkError, SevenZipNotFoundError
from anker_client.core.events import JobAdded, JobRemoved, QueueChanged
from anker_client.core.models import DownloadOption, JobState
from tests.unit.test_manager_fakes import (
    DEFAULT_SIZE,
    FULL,
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


def _job_dir(h: Harness, job_id: str) -> str:
    return os.path.join(h.library_root, ".ankerclient", "downloads", job_id)


def _downloading(h: Harness, manager, slug: str = "hollow-knight"):
    """Enqueue a job whose download blocks half-way; returns it once blocked."""
    h.script.default_action = "block"
    job = enqueue(manager, slug=slug)
    h.wait_state(manager, job.id, JobState.DOWNLOADING)
    wait_until(lambda: h.script.blocked >= 1)
    return job


# --- enqueue ------------------------------------------------------------------------------------


def test_enqueue_publishes_and_dedupes(h: Harness) -> None:
    manager = h.make_manager()
    first = enqueue(manager)
    again = enqueue(manager)
    assert again.id == first.id
    assert len(h.recorder.of_type(JobAdded)) == 1
    assert first.state is JobState.QUEUED
    assert first.library_root == h.library_root
    other_option = enqueue(manager, option=DownloadOption(999, "Language Pack"))
    assert other_option.id != first.id
    other_game = enqueue(manager, slug="celeste")
    assert [j.id for j in manager.jobs()] == [first.id, other_option.id, other_game.id]
    assert [j.position for j in manager.jobs()] == [0, 1, 2]


def test_enqueue_after_finished_creates_new_job(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    first = enqueue(manager)
    h.wait_state(manager, first.id, JobState.COMPLETED)
    second = enqueue(manager)
    assert second.id != first.id


def test_enqueue_requires_slug(h: Harness) -> None:
    manager = h.make_manager()
    with pytest.raises(ValueError):
        manager.enqueue(slug="", title="x", option=FULL)


def test_queries_return_copies(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    copy = manager.get(job.id)
    copy.title = "changed"
    manager.jobs()[0].state = JobState.FAILED
    assert manager.get(job.id).title == "Hollow Knight"
    assert manager.get(job.id).state is JobState.QUEUED
    assert manager.get("missing") is None


def test_job_for_prefers_running_then_queued_then_paused(h: Harness) -> None:
    manager = h.make_manager()
    paused = enqueue(manager, option=DownloadOption(1, "Direct"))
    queued = enqueue(manager, option=DownloadOption(2, "Language Pack"))
    manager.pause(paused.id)
    assert manager.job_for("hollow-knight").id == queued.id
    manager.pause(queued.id)
    assert manager.job_for("hollow-knight").id == paused.id
    manager.cancel(paused.id)
    manager.cancel(queued.id)
    assert manager.job_for("hollow-knight") is None
    assert manager.job_for("unknown") is None


# --- pause / resume -----------------------------------------------------------------------------


def test_pause_running_keeps_partial_and_resume_continues(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    assert manager.active_jobs()[0].id == job.id
    manager.pause(job.id)
    paused = h.wait_state(manager, job.id, JobState.PAUSED)
    assert paused.bytes_done == DEFAULT_SIZE // 2
    assert os.path.getsize(paused.archive_path + ".part") == DEFAULT_SIZE // 2
    assert h.db_job(job.id)["_manager"]["pause_reason"] == "user"
    assert manager.active_jobs() == []

    h.script.default_action = "ok"
    manager.resume(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.calls[-1].start_offset == DEFAULT_SIZE // 2
    assert h.resolver.call_count == 1  # fresh stored link reused: no new ticket


def test_resume_after_link_got_old_resolves_again(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    manager.pause(job.id)
    h.wait_state(manager, job.id, JobState.PAUSED)
    h.clock.advance(2 * 3600)
    h.script.default_action = "ok"
    manager.resume(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.resolver.call_count == 2


def test_pause_during_verification(h: Harness) -> None:
    h.resolver.script = ["block"]
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.VERIFYING)
    manager.pause(job.id)
    h.wait_state(manager, job.id, JobState.PAUSED)


def test_pause_queued_and_waiting_jobs(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    manager.pause(job.id)
    assert manager.get(job.id).state is JobState.PAUSED
    manager.resume(job.id)
    assert manager.get(job.id).state is JobState.QUEUED

    h.resolver.script = [NetworkError()]
    manager.start()
    h.wait_state(manager, job.id, JobState.WAITING)
    manager.pause(job.id)
    paused = manager.get(job.id)
    assert paused.state is JobState.PAUSED and paused.retry_at is None


def test_pause_all_and_resume_all(h: Harness) -> None:
    h.settings.update(max_concurrent_downloads=1)
    manager = h.make_manager()
    manager.start()
    running = _downloading(h, manager, "a")
    queued = enqueue(manager, slug="b")
    manager.pause_all()
    h.wait_state(manager, running.id, JobState.PAUSED)
    assert manager.get(queued.id).state is JobState.PAUSED
    h.script.default_action = "ok"
    manager.resume_all()
    h.wait_state(manager, running.id, JobState.COMPLETED)
    h.wait_state(manager, queued.id, JobState.COMPLETED)


# --- cancel / retry / remove ---------------------------------------------------------------------


def test_cancel_running_deletes_partial_data(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    assert os.path.isdir(_job_dir(h, job.id))
    manager.cancel(job.id)
    cancelled = h.wait_state(manager, job.id, JobState.CANCELLED)
    assert not os.path.exists(_job_dir(h, job.id))
    assert cancelled.bytes_done == 0 and cancelled.archive_path == ""
    assert h.installer.call_count == 0


def test_cancel_paused_job_deletes_partial_data(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    manager.pause(job.id)
    h.wait_state(manager, job.id, JobState.PAUSED)
    manager.cancel(job.id)
    assert manager.get(job.id).state is JobState.CANCELLED
    wait_until(lambda: not os.path.exists(_job_dir(h, job.id)))


def test_cancel_completed_install_is_a_no_op(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    manager.cancel(job.id)
    assert manager.get(job.id).state is JobState.COMPLETED


def test_retry_cancelled_job_downloads_from_scratch(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    manager.cancel(job.id)
    h.wait_state(manager, job.id, JobState.CANCELLED)
    h.script.default_action = "ok"
    manager.retry(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.calls[-1].start_offset == 0
    assert h.resolver.call_count == 2


def test_retry_failed_download_resumes_partial(h: Harness) -> None:
    h.script.actions = [SevenZipNotFoundError()]  # non-retryable → FAILED with partial kept
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    failed = h.wait_state(manager, job.id, JobState.FAILED)
    assert failed.bytes_done == 0
    manager.retry(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.calls[-1].start_offset == 16


def test_remove_finished_job_deletes_row_and_publishes(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    manager.remove(job.id)
    assert manager.get(job.id) is None
    assert h.db_job(job.id) is None
    assert h.recorder.of_type(JobRemoved)[-1].job_id == job.id


def test_remove_failed_job_deletes_its_partial_data(h: Harness) -> None:
    h.script.actions = [SevenZipNotFoundError()]
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.FAILED)
    assert os.path.isdir(_job_dir(h, job.id))
    manager.remove(job.id)
    assert not os.path.exists(_job_dir(h, job.id))


def test_remove_running_job_cancels_then_removes(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    manager.remove(job.id)
    wait_until(lambda: manager.get(job.id) is None)
    assert not os.path.exists(_job_dir(h, job.id))
    wait_until(lambda: h.db_job(job.id) is None)


def test_remove_queued_job(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    manager.remove(job.id)
    assert manager.get(job.id) is None
    assert h.db_job(job.id) is None


def test_clear_finished(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    done = enqueue(manager, slug="a")
    h.wait_state(manager, done.id, JobState.COMPLETED)
    cancelled = enqueue(manager, slug="b")
    manager.pause(cancelled.id)
    manager.cancel(cancelled.id)
    paused = enqueue(manager, slug="c")
    manager.pause(paused.id)
    h.recorder.clear()
    manager.clear_finished()
    assert [j.id for j in manager.jobs()] == [paused.id]
    assert {e.job_id for e in h.recorder.of_type(JobRemoved)} == {done.id, cancelled.id}
    assert h.recorder.of_type(QueueChanged)
    assert h.db_job(done.id) is None and h.db_job(paused.id) is not None


# --- ordering / concurrency -------------------------------------------------------------------------


def test_move_reorders_and_persists(h: Harness) -> None:
    manager = h.make_manager()
    a, b, c = (enqueue(manager, slug=s) for s in "abc")
    h.recorder.clear()
    manager.move(c.id, 0)
    assert [j.id for j in manager.jobs()] == [c.id, a.id, b.id]
    assert [h.db_job(x.id)["position"] for x in (c, a, b)] == [0, 1, 2]
    assert len(h.recorder.of_type(QueueChanged)) == 1
    manager.move(c.id, 99)
    assert [j.id for j in manager.jobs()] == [a.id, b.id, c.id]
    manager.move(c.id, 2)  # no change → no event
    assert len(h.recorder.of_type(QueueChanged)) == 2


def test_scheduler_follows_queue_order(h: Harness) -> None:
    manager = h.make_manager()
    a, b, c = (enqueue(manager, slug=s) for s in "abc")
    manager.move(c.id, 0)
    manager.start()
    for job in (a, b, c):
        h.wait_state(manager, job.id, JobState.COMPLETED)
    started_slugs = [call["slug"] for call in h.resolver.calls]
    assert started_slugs == ["c", "a", "b"]


def test_concurrency_limit_and_live_change(h: Harness) -> None:
    h.script.default_action = "block"
    manager = h.make_manager()
    manager.start()
    jobs = [enqueue(manager, slug=s) for s in "abc"]
    wait_until(lambda: h.script.blocked == 1)
    threading.Event().wait(0.1)
    states = [manager.get(j.id).state for j in jobs]
    assert states == [JobState.DOWNLOADING, JobState.QUEUED, JobState.QUEUED]

    h.settings.update(max_concurrent_downloads=2)
    wait_until(lambda: h.script.blocked == 2)
    threading.Event().wait(0.1)
    assert h.script.max_active == 2
    assert manager.get(jobs[2].id).state is JobState.QUEUED
    h.script.release.set()
    for job in jobs:
        h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.max_active == 2


def test_speed_limit_applies_live(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    assert h.limiter.rates[-1] == 0
    h.settings.update(speed_limit_kbps=512)
    assert h.limiter.rates[-1] == 512 * 1024


def test_connections_change_restarts_running_download(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    assert h.script.calls[0].connections == 4
    h.settings.update(connections_per_download=8)
    # the restarted download resumes half-way and blocks there again
    wait_until(lambda: h.script.call_count == 2 and h.script.blocked == 1)
    assert h.script.calls[-1].connections == 8
    assert manager.get(job.id).state is JobState.DOWNLOADING
    h.script.release.set()
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.calls[1].start_offset == DEFAULT_SIZE // 2
    assert h.resolver.call_count == 1  # link reused across the restart


def test_commands_on_unknown_job_are_ignored(h: Harness) -> None:
    manager = h.make_manager()
    for command in (manager.pause, manager.resume, manager.retry, manager.cancel, manager.install, manager.remove):
        command("nope")
    manager.move("nope", 0)
    manager.clear_finished()
    assert manager.jobs() == []


# --- races / install phase ----------------------------------------------------------------------


def test_resume_while_pausing_ends_up_running(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    h.script.default_action = "ok"
    manager.pause(job.id)
    manager.resume(job.id)  # either still unwinding (→ restart) or already paused (→ requeue)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.calls[-1].start_offset == DEFAULT_SIZE // 2


def test_resume_while_pausing_is_a_restart(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    job = _downloading(h, manager)
    with manager._lock:  # freeze the worker's outcome handling while both commands land
        request = manager._request_stop_locked(job.id, "pause")
        assert request is not None
    manager.resume(job.id)
    assert manager.get(job.id).status_text == "Resuming…"
    h.script.default_action = "ok"
    request[0].cancel(request[1])
    h.wait_state(manager, job.id, JobState.COMPLETED)
    paused_events = [e for e in h.recorder.job_states(job.id) if e is JobState.PAUSED]
    assert paused_events == []


def test_no_stale_progress_event_after_pause(h: Harness) -> None:
    from anker_client.core.events import JobUpdated
    from tests.unit.test_manager_fakes import FakeClock

    mono = FakeClock(50.0)
    h.script.default_action = "stall"
    h.script.steps = 32
    manager = h.make_manager(monotonic=mono)
    manager.start()
    job = enqueue(manager)
    wait_until(lambda: h.script.blocked == 1)  # progress callbacks are now owed (frozen clock)
    manager.pause(job.id)
    h.wait_state(manager, job.id, JobState.PAUSED)
    mono.advance(5)
    threading.Event().wait(0.1)
    updates = [e.job for e in h.recorder.of_type(JobUpdated) if e.job.id == job.id]
    last_paused = max(i for i, j in enumerate(updates) if j.state is JobState.PAUSED)
    assert all(j.state is JobState.PAUSED for j in updates[last_paused:])
    assert updates[-1].bytes_done == DEFAULT_SIZE // 2  # the pause carried the latest progress


def test_pause_during_install_then_resume_installs_without_download(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    wait_until(lambda: h.installer.call_count == 1)
    manager.pause(job.id)
    paused = h.wait_state(manager, job.id, JobState.PAUSED)
    assert os.path.isfile(paused.archive_path)
    h.installer.gate.set()
    manager.resume(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    assert h.script.call_count == 1 and h.resolver.call_count == 1
    assert h.installer.call_count == 2


def test_cancel_during_install_deletes_archive(h: Harness) -> None:
    h.installer.gate = threading.Event()
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    wait_until(lambda: h.installer.call_count == 1)
    manager.cancel(job.id)
    h.wait_state(manager, job.id, JobState.CANCELLED)
    assert not os.path.exists(_job_dir(h, job.id))


def test_install_is_ignored_for_unsuitable_states(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    manager.install(job.id)
    assert manager.get(job.id).state is JobState.QUEUED
    manager.start()
    h.wait_state(manager, job.id, JobState.COMPLETED)
    manager.install(job.id)  # already installed
    threading.Event().wait(0.05)
    assert h.installer.call_count == 1


def test_waiting_job_can_be_cancelled_and_removed(h: Harness) -> None:
    h.resolver.script = [NetworkError()]
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.WAITING)
    manager.remove(job.id)
    assert manager.get(job.id) is None
    assert h.db_job(job.id) is None
