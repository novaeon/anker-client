"""Pure helpers behind the DownloadManager: error policy, archive locations, safe cleanup, job rows."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from anker_client.core.errors import (
    AccessDeniedError,
    AnkerError,
    CorruptArchiveError,
    DiskSpaceError,
    DownloadError,
    ExternalHostError,
    LinkExpiredError,
    NetworkError,
    QuotaExceededError,
    RateLimitedError,
    VerificationTimeout,
    VerificationUnavailable,
)
from anker_client.core.models import DownloadJob, DownloadOption, ErrorKind, JobState
from anker_client.services.downloads._manager_policy import (
    MAX_ARCHIVE_NAME,
    LinkKeepsExpiringError,
    archive_filename,
    decide_failure,
    discard_job_files,
    download_base_dir,
    job_work_dir,
)
from anker_client.services.downloads._manager_state import EXTRA_KEY, JobMeta, decode_job, encode_job
from anker_client.services.downloads.manager import _recover_loaded_job

NOW = 1_000_000.0


def _job(**kw) -> DownloadJob:
    return DownloadJob(id=kw.pop("id", "abc123"), slug="game", title="Game", option=DownloadOption(1, "Direct"),
                       library_root=kw.pop("library_root", "C:/Games"), **kw)


# --- decide_failure -----------------------------------------------------------------------------


def test_rate_limited_waits_without_using_a_retry() -> None:
    d = decide_failure(RateLimitedError(42), attempts=2, now=NOW)
    assert d.state is JobState.WAITING and d.retry_at == NOW + 42 and d.attempts == 2
    assert d.error_kind is ErrorKind.RATE_LIMITED


@pytest.mark.parametrize(("attempts", "delay"), [(0, 30.0), (1, 120.0), (2, 600.0)])
def test_retryable_backoff(attempts: int, delay: float) -> None:
    d = decide_failure(NetworkError(), attempts=attempts, now=NOW)
    assert d.state is JobState.WAITING
    assert d.retry_at == NOW + delay
    assert d.attempts == attempts + 1
    assert d.status_text.startswith(f"Retry {attempts + 1} of 3")


def test_retry_budget_exhausted() -> None:
    d = decide_failure(DownloadError(), attempts=3, now=NOW)
    assert d.state is JobState.FAILED and d.retry_at is None and d.attempts == 3


def test_custom_backoff() -> None:
    assert decide_failure(NetworkError(), attempts=0, now=NOW, backoff=(1.0,)).retry_at == NOW + 1
    assert decide_failure(NetworkError(), attempts=1, now=NOW, backoff=(1.0,)).state is JobState.FAILED


def test_external_and_verification_errors_fail_with_url() -> None:
    ext = decide_failure(ExternalHostError("https://t", "Host"), attempts=0, now=NOW)
    assert (ext.state, ext.error_url, ext.error_kind) == (JobState.FAILED, "https://t", ErrorKind.EXTERNAL_HOST)
    for error in (VerificationTimeout(ticket_url="https://v"), VerificationUnavailable(ticket_url="https://v")):
        ver = decide_failure(error, attempts=0, now=NOW)
        assert (ver.state, ver.error_url, ver.error_kind) == (JobState.FAILED, "https://v", ErrorKind.VERIFICATION)


@pytest.mark.parametrize(
    "error",
    [DiskSpaceError(10, 1, "C:\\"), QuotaExceededError(), AccessDeniedError(), AnkerError("x")],
)
def test_non_retryable_errors_fail(error: AnkerError) -> None:
    d = decide_failure(error, attempts=0, now=NOW)
    assert d.state is JobState.FAILED and d.error == error.user_message and d.error_kind is error.kind


def test_link_expired_while_resolving_is_retried_with_backoff() -> None:
    d = decide_failure(LinkExpiredError(), attempts=0, now=NOW)
    assert d.state is JobState.WAITING and d.error_kind is ErrorKind.LINK_EXPIRED
    assert d.retry_at == NOW + 30 and d.attempts == 1


def test_link_that_keeps_expiring_fails() -> None:
    d = decide_failure(LinkKeepsExpiringError(), attempts=0, now=NOW)
    assert d.state is JobState.FAILED and d.error_kind is ErrorKind.LINK_EXPIRED
    assert "keeps expiring" in d.error and d.retry_at is None


def test_corrupt_archive_is_retryable() -> None:
    assert decide_failure(CorruptArchiveError(), attempts=0, now=NOW).state is JobState.WAITING


# --- locations ------------------------------------------------------------------------------------


def test_download_base_dir(tmp_path: Path) -> None:
    assert download_base_dir("", str(tmp_path)) == str(tmp_path / ".ankerclient" / "downloads")
    assert download_base_dir("  ", str(tmp_path)) == str(tmp_path / ".ankerclient" / "downloads")
    assert download_base_dir(str(tmp_path / "dl"), "C:/Games") == str(tmp_path / "dl")


@pytest.mark.parametrize(
    ("name", "slug", "expected"),
    [
        ("Game.v1.zip", "game", "Game.v1.zip"),
        ("", "my-game", "my-game.zip"),
        ("a/b/c.7z", "x", "c.7z"),
        ("..\\..\\x.rar", "x", "x.rar"),
        ("con", "x", "_con"),
        ("...", "slug", "slug.zip"),
        ("", "", "download.zip"),
    ],
)
def test_archive_filename(name: str, slug: str, expected: str) -> None:
    assert archive_filename(name, slug) == expected


def test_archive_filename_keeps_unicode() -> None:
    assert archive_filename("Ünïcödé Gämé — Édition 日本語.7z", "x") == "Ünïcödé Gämé — Édition 日本語.7z"


def test_long_archive_filename_keeps_its_extension() -> None:
    name = archive_filename("A" * 300 + ".part01.rar", "slug")
    assert len(name) == MAX_ARCHIVE_NAME
    assert name.endswith(".rar")
    assert name.startswith("AAAA")


def test_long_archive_filename_without_usable_extension_is_truncated() -> None:
    assert len(archive_filename("B" * 500, "slug")) == MAX_ARCHIVE_NAME
    weird = archive_filename("C" * 200 + "." + "d" * 40, "slug")  # "extension" too long to be one
    assert len(weird) == MAX_ARCHIVE_NAME


def test_long_slug_fallback_stays_short() -> None:
    name = archive_filename("", "s" * 400)
    assert len(name) <= MAX_ARCHIVE_NAME and name.endswith(".zip")


def test_job_work_dir_needs_a_job_id(tmp_path: Path) -> None:
    # An empty id would make the drive root / parent look like "the job folder".
    assert job_work_dir(_job(id="", archive_path=str(tmp_path / "a.zip"))) == ""


def test_job_work_dir_only_for_own_folders(tmp_path: Path) -> None:
    own = _job(id="j1", archive_path=str(tmp_path / "downloads" / "j1" / "a.zip"))
    assert job_work_dir(own) == str(tmp_path / "downloads" / "j1")
    assert job_work_dir(_job(id="j1", archive_path=str(tmp_path / "elsewhere" / "a.zip"))) == ""
    assert job_work_dir(_job(id="j1", archive_path="")) == ""
    imported = _job(id="j1", archive_path=str(tmp_path / "downloads" / "j1" / "a.zip"), imported_archive=True)
    assert job_work_dir(imported) == ""


def test_discard_job_files_deletes_only_the_job_folder(tmp_path: Path) -> None:
    folder = tmp_path / "downloads" / "j1"
    folder.mkdir(parents=True)
    (folder / "a.zip.part").write_bytes(b"x")
    readonly = folder / "a.zip.part.json"
    readonly.write_text("{}")
    os.chmod(readonly, stat.S_IREAD)
    sibling = tmp_path / "downloads" / "keep.txt"
    sibling.write_text("keep")
    assert discard_job_files(_job(id="j1", archive_path=str(folder / "a.zip"))) is True
    assert not folder.exists()
    assert sibling.exists()


def test_discard_job_files_refuses_foreign_paths(tmp_path: Path) -> None:
    user_archive = tmp_path / "Downloads" / "game.zip"
    user_archive.parent.mkdir()
    user_archive.write_bytes(b"x")
    assert discard_job_files(_job(id="j1", archive_path=str(user_archive))) is True
    assert user_archive.exists()
    imported = _job(id="Downloads", archive_path=str(user_archive), imported_archive=True)
    discard_job_files(imported)
    assert user_archive.exists()


# --- rows -----------------------------------------------------------------------------------------


def test_job_row_round_trip() -> None:
    job = _job(state=JobState.WAITING, retry_at=5.0, error_kind=ErrorKind.NETWORK, position=3)
    meta = JobMeta(pause_reason="user", archive_complete=True, install_requested=True,
                   link={"url": "https://x", "size": 5}, link_resolved_at=12.5)
    row = encode_job(job, meta)
    assert (row.id, row.state, row.position) == ("abc123", "waiting", 3)
    data = json.loads(row.json)
    assert data[EXTRA_KEY]["pause_reason"] == "user"
    job2, meta2 = decode_job(row.json)
    assert job2 == job
    assert meta2.persisted() == meta.persisted()


@pytest.mark.parametrize("text", ["{bad", "[]", '{"id": "x"}', json.dumps({"id": "", "slug": "s", "title": "t",
                                                                            "option": {"download_id": 1, "label": "D"},
                                                                            "library_root": "C:/"})])
def test_decode_rejects_unusable_rows(text: str) -> None:
    with pytest.raises(ValueError):
        decode_job(text)


def test_decode_tolerates_bad_private_fields() -> None:
    data = _job().to_dict()
    data[EXTRA_KEY] = {"link": "nope", "link_resolved_at": "never", "archive_complete": 1}
    meta = decode_job(json.dumps(data))[1]
    assert meta.link is None and meta.link_resolved_at == 0.0 and meta.archive_complete is True
    data[EXTRA_KEY] = "garbage"
    assert decode_job(json.dumps(data))[1] == JobMeta()


# --- recovery ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", [JobState.RESOLVING, JobState.VERIFYING, JobState.DOWNLOADING])
def test_recover_download_states_pause_for_auto_resume(state: JobState) -> None:
    job, meta = _job(state=state, speed_bps=10.0), JobMeta()
    assert _recover_loaded_job(job, meta) is True
    assert job.state is JobState.PAUSED and meta.pause_reason == "shutdown" and job.speed_bps == 0


@pytest.mark.parametrize("state", [JobState.EXTRACTING, JobState.INSTALLING])
def test_recover_install_states_fail(state: JobState) -> None:
    job, meta = _job(state=state, phase_progress=0.5), JobMeta(archive_complete=True)
    assert _recover_loaded_job(job, meta) is True
    assert job.state is JobState.FAILED and job.error_kind is ErrorKind.INSTALL
    assert meta.archive_complete is True


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.PAUSED, JobState.COMPLETED, JobState.FAILED])
def test_recover_leaves_resting_states_alone(state: JobState) -> None:
    job = _job(state=state)
    assert _recover_loaded_job(job, JobMeta()) is False
    assert job.state is state


def test_decode_repairs_numeric_fields_stored_as_text() -> None:
    data = _job(position=0).to_dict()
    data.update(position="3", attempts="2", bytes_done="10", bytes_total="20", retry_at="5.5", genres="RPG")
    job = decode_job(json.dumps(data))[0]
    assert (job.position, job.attempts, job.bytes_done, job.bytes_total, job.retry_at) == (3, 2, 10, 20, 5.5)
    assert job.genres == []


@pytest.mark.parametrize("field", ["position", "retry_at", "bytes_total"])
def test_decode_rejects_unrepairable_numbers(field: str) -> None:
    data = _job().to_dict()
    data[field] = "soon"
    with pytest.raises(ValueError):
        decode_job(json.dumps(data))
