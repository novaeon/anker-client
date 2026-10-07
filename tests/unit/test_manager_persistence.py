"""DownloadManager: persistence in the jobs table, startup recovery, auto-resume, shutdown."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from anker_client.core.errors import NetworkError
from anker_client.core.events import QueueChanged
from anker_client.core.models import DownloadJob, DownloadOption, ErrorKind, JobState, utc_now_iso
from anker_client.services.downloads._manager_state import EXTRA_KEY, JobMeta, encode_job
from tests.unit.test_manager_fakes import DEFAULT_SIZE, Harness, enqueue, make_harness, wait_until


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    harness = make_harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def _insert(h: Harness, job: DownloadJob, meta: JobMeta | None = None) -> None:
    row = encode_job(job, meta or JobMeta())
    h.db.execute(
        "INSERT INTO jobs(id, state, position, json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
        (row.id, row.state, row.position, row.json, row.created_at, row.updated_at),
    )


def _job(h: Harness, job_id: str, state: JobState, **kw) -> DownloadJob:
    return DownloadJob(
        id=job_id,
        slug=kw.pop("slug", job_id),
        title=kw.pop("title", job_id.upper()),
        option=DownloadOption(7, "Direct"),
        library_root=h.library_root,
        state=state,
        **kw,
    )


def _archive(h: Harness, job_id: str) -> str:
    folder = os.path.join(h.library_root, ".ankerclient", "downloads", job_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "game.zip")
    with open(path, "wb") as handle:
        handle.write(b"x" * DEFAULT_SIZE)
    return path


# --- rows -------------------------------------------------------------------------------------------


def test_every_state_change_is_persisted_with_private_fields(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    row = h.db.query_one("SELECT state, position, json FROM jobs WHERE id = ?", (job.id,))
    assert row["state"] == "queued" and row["position"] == 0
    data = json.loads(row["json"])
    assert data["slug"] == "hollow-knight"
    assert data[EXTRA_KEY]["pause_reason"] == ""
    assert DownloadJob.from_dict(data).id == job.id  # unknown keys are ignored by the model

    manager.pause(job.id)
    assert h.db_job(job.id)["state"] == "paused"
    assert h.db_job(job.id)[EXTRA_KEY]["pause_reason"] == "user"

    manager.start()
    manager.resume(job.id)
    h.wait_state(manager, job.id, JobState.COMPLETED)
    stored = h.db_job(job.id)
    assert stored["state"] == "completed" and stored["install_path"]


def test_jobs_are_loaded_before_start(h: Harness) -> None:
    _insert(h, _job(h, "b", JobState.QUEUED, position=5))
    _insert(h, _job(h, "a", JobState.PAUSED, position=2))
    manager = h.make_manager()
    jobs = manager.jobs()
    assert [j.id for j in jobs] == ["a", "b"]
    assert [j.position for j in jobs] == [0, 1]  # normalised
    assert h.db_job("b")["position"] == 1


def test_corrupt_rows_are_ignored(h: Harness) -> None:
    h.db.execute(
        "INSERT INTO jobs(id, state, position, json, created_at, updated_at) VALUES('bad', 'queued', 0, '{not json',"
        " '', '')"
    )
    h.db.execute(
        "INSERT INTO jobs(id, state, position, json, created_at, updated_at) VALUES('bad2', 'queued', 1, '{\"id\": 1}',"
        " '', '')"
    )
    _insert(h, _job(h, "good", JobState.QUEUED, position=3))
    manager = h.make_manager()
    assert [j.id for j in manager.jobs()] == ["good"]


# --- recovery / auto resume --------------------------------------------------------------------------


def test_crash_recovery_rules(h: Harness) -> None:
    _insert(h, _job(h, "dl", JobState.DOWNLOADING, position=0, speed_bps=5.0, eta_seconds=3.0, bytes_done=100))
    _insert(h, _job(h, "verify", JobState.VERIFYING, position=1))
    archive = _archive(h, "inst")
    _insert(h, _job(h, "inst", JobState.INSTALLING, position=2, archive_path=archive),
            JobMeta(archive_complete=True, install_requested=True))
    h.settings.update(auto_resume_downloads=False)
    manager = h.make_manager()
    jobs = {j.id: j for j in manager.jobs()}
    assert jobs["dl"].state is JobState.PAUSED and jobs["dl"].speed_bps == 0 and jobs["dl"].eta_seconds is None
    assert jobs["verify"].state is JobState.PAUSED
    assert jobs["inst"].state is JobState.FAILED
    assert jobs["inst"].error_kind is ErrorKind.INSTALL and "Retry" in jobs["inst"].error
    assert h.db_job("dl")[EXTRA_KEY]["pause_reason"] == "shutdown"
    assert h.db_job("inst")["state"] == "failed"

    manager.start()  # auto-resume is off: nothing starts
    threading.Event().wait(0.1)
    assert manager.get("dl").state is JobState.PAUSED
    manager.retry("inst")
    h.wait_state(manager, "inst", JobState.COMPLETED)
    assert h.script.call_count == 0  # the kept archive was installed directly
    assert h.installer.calls[0].request.archive_path == archive


def test_auto_resume_requeues_only_shutdown_paused_jobs(h: Harness) -> None:
    _insert(h, _job(h, "crashed", JobState.DOWNLOADING, position=0))
    _insert(h, _job(h, "shut", JobState.PAUSED, position=1), JobMeta(pause_reason="shutdown"))
    _insert(h, _job(h, "user", JobState.PAUSED, position=2), JobMeta(pause_reason="user"))
    h.settings.update(max_concurrent_downloads=3)
    manager = h.make_manager()
    manager.start()
    h.wait_state(manager, "crashed", JobState.COMPLETED)
    h.wait_state(manager, "shut", JobState.COMPLETED)
    assert manager.get("user").state is JobState.PAUSED
    assert any(isinstance(e, QueueChanged) for e in h.recorder.snapshot())


def test_auto_resume_off_pauses_loaded_queue(h: Harness) -> None:
    _insert(h, _job(h, "q", JobState.QUEUED, position=0))
    _insert(h, _job(h, "w", JobState.WAITING, position=1, retry_at=0.0))
    h.settings.update(auto_resume_downloads=False)
    manager = h.make_manager()
    manager.start()
    threading.Event().wait(0.1)
    assert manager.get("q").state is JobState.PAUSED
    assert manager.get("w").state is JobState.PAUSED
    assert h.resolver.call_count == 0
    late = enqueue(manager, slug="late")  # enqueued in this session: runs normally
    h.wait_state(manager, late.id, JobState.COMPLETED)
    manager.resume_all()
    h.wait_state(manager, "q", JobState.COMPLETED)


def test_waiting_job_keeps_retry_at_across_restart(h: Harness) -> None:
    retry_at = h.clock() + 100
    _insert(h, _job(h, "w", JobState.WAITING, retry_at=retry_at, attempts=2, error="x",
                    error_kind=ErrorKind.NETWORK))
    manager = h.make_manager()
    manager.start()
    threading.Event().wait(0.1)
    job = manager.get("w")
    assert job.state is JobState.WAITING and job.retry_at == retry_at and job.attempts == 2
    h.clock.advance(100)
    h.wait_state(manager, "w", JobState.COMPLETED)


# --- shutdown / restart -----------------------------------------------------------------------------


def test_shutdown_pauses_with_reason_and_next_start_resumes(h: Harness) -> None:
    h.script.default_action = "block"
    first = h.make_manager()
    first.start()
    job = enqueue(first)
    paused_by_user = enqueue(first, slug="other")
    first.pause(paused_by_user.id)
    wait_until(lambda: h.script.blocked == 1)
    first.shutdown(timeout=5)
    assert first.get(job.id).state is JobState.PAUSED
    stored = h.db_job(job.id)
    assert stored["state"] == "paused"
    assert stored[EXTRA_KEY]["pause_reason"] == "shutdown"
    assert stored["bytes_done"] == DEFAULT_SIZE // 2  # progress flushed on the way out
    assert h.db_job(paused_by_user.id)[EXTRA_KEY]["pause_reason"] == "user"
    # threads are gone
    assert not [t for t in threading.enumerate() if t.name.startswith("anker-dl")]

    h.script.default_action = "ok"
    second = h.make_manager()
    second.start()
    done = h.wait_state(second, job.id, JobState.COMPLETED)
    assert h.script.calls[-1].start_offset == DEFAULT_SIZE // 2
    assert done.install_path
    assert second.get(paused_by_user.id).state is JobState.PAUSED


def test_shutdown_after_user_pause_request_stays_user_paused(h: Harness) -> None:
    h.script.default_action = "block"
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    wait_until(lambda: h.script.blocked == 1)
    with manager._lock:  # simulate the pause arriving just before shutdown
        request = manager._request_stop_locked(job.id, "pause")
    manager.shutdown(timeout=5)
    assert request is not None
    assert h.db_job(job.id)[EXTRA_KEY]["pause_reason"] == "user"


def test_shutdown_is_idempotent_and_blocks_restart(h: Harness) -> None:
    manager = h.make_manager()
    manager.start()
    manager.shutdown(timeout=1)
    manager.shutdown(timeout=1)
    manager.start()
    job = enqueue(manager)
    threading.Event().wait(0.1)
    assert manager.get(job.id).state is JobState.QUEUED


def test_shutdown_without_start(h: Harness) -> None:
    manager = h.make_manager()
    job = enqueue(manager)
    manager.shutdown(timeout=1)
    assert h.db_job(job.id)["state"] == "queued"
    assert h.resolver.call_count == 0
    later = enqueue(manager, slug="late")  # after shutdown: kept in memory only, never written
    assert h.db_job(later.id) is None


def test_waiting_retry_is_counted_in_db(h: Harness) -> None:
    h.resolver.script = [NetworkError()]
    manager = h.make_manager()
    manager.start()
    job = enqueue(manager)
    h.wait_state(manager, job.id, JobState.WAITING)
    stored = h.db_job(job.id)
    assert stored["state"] == "waiting" and stored["attempts"] == 1
    assert stored["retry_at"] == pytest.approx(h.clock() + 30)
    assert stored["updated_at"] <= utc_now_iso()


def test_only_the_scheduler_writes_while_running(h: Harness) -> None:
    """Worker/engine threads never touch SQLite (each would leak a per-thread connection)."""
    manager = h.make_manager()
    writers: set[str] = set()
    original_upsert, original_delete = manager._store.upsert, manager._store.delete

    def upsert(rows):
        if rows:
            writers.add(threading.current_thread().name)
        original_upsert(rows)

    def delete(ids):
        writers.add(threading.current_thread().name)
        original_delete(ids)

    manager._store.upsert = upsert  # type: ignore[method-assign]
    manager._store.delete = delete  # type: ignore[method-assign]
    manager.start()
    writers.clear()
    jobs = [enqueue(manager, slug=s) for s in "abc"]
    for job in jobs:
        h.wait_state(manager, job.id, JobState.COMPLETED)
    manager.remove(jobs[0].id)
    h.settle(manager)
    assert writers == {"anker-dl-scheduler"}
    manager.shutdown(timeout=5)
    assert h.db_job(jobs[1].id)["state"] == "completed"
    assert h.db_job(jobs[0].id) is None
