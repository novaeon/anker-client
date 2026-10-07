"""Pure decisions and file helpers for :mod:`anker_client.services.downloads.manager`.

Kept free of threading so the rules (error → next state, archive locations,
what may be deleted) can be unit-tested directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from anker_client.constants import LIBRARY_WORK_DIRNAME
from anker_client.core.errors import (
    AnkerError,
    DiskSpaceError,
    ExternalHostError,
    LinkExpiredError,
    RateLimitedError,
    VerificationError,
)
from anker_client.core.formatting import format_duration
from anker_client.core.models import DownloadJob, ErrorKind, JobState
from anker_client.core.paths import is_dangerous_delete_target, sanitize_windows_name

log = logging.getLogger(__name__)

#: Waits before automatic retries of retryable errors; its length is the retry budget.
DEFAULT_BACKOFF: tuple[float, ...] = (30.0, 120.0, 600.0)

#: A resolved CDN link younger than this is tried again on resume before re-resolving
#: (saves a ticket — and possibly a browser verification — per pause/resume).
LINK_REUSE_MAX_AGE = 3600.0

#: Longest archive file name we create (extension included). Keeps
#: ``<library>\.ankerclient\downloads\<job id>\<name>.part.json`` well under MAX_PATH.
MAX_ARCHIVE_NAME = 120

# Cancellation reasons (``CancelToken.reason``).
REASON_PAUSE = "pause"
REASON_SHUTDOWN = "shutdown"
REASON_CANCEL = "cancel"
REASON_RESTART = "restart"  # settings change: stop and immediately requeue

DOWNLOAD_PHASE_STATES = frozenset({JobState.RESOLVING, JobState.VERIFYING, JobState.DOWNLOADING})
INSTALL_PHASE_STATES = frozenset({JobState.EXTRACTING, JobState.INSTALLING})


class LinkKeepsExpiringError(LinkExpiredError):
    """A freshly re-resolved link expired again mid-download: give up until the user retries."""

    retryable = False

    @classmethod
    def default_message(cls) -> str:
        return "The download link keeps expiring. Retry the download in a moment."


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """What a pipeline failure turns the job into."""

    state: JobState  # WAITING or FAILED
    error: str
    error_kind: ErrorKind
    error_url: str = ""
    retry_at: float | None = None
    attempts: int = 0
    status_text: str = ""


def decide_failure(
    exc: AnkerError,
    *,
    attempts: int,
    now: float,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
) -> FailureDecision:
    """Apply the manager's error policy to ``exc``.

    * ``RateLimitedError`` → WAITING until ``now + retry_after`` (does not use up a retry).
    * ``ExternalHostError`` / any ``VerificationError`` → FAILED with ``error_url``
      (the page the user can open in a browser) — retrying automatically would only
      pop the browser again or hit the same hand-off.
    * ``LinkKeepsExpiringError`` (a re-resolved link expired too) → FAILED. A plain
      ``LinkExpiredError`` (e.g. the ticket page itself was gone) is retried with backoff.
    * ``DiskSpaceError`` → FAILED.
    * other retryable errors → WAITING with ``backoff[attempts]`` while retries remain, else FAILED.
    * everything else → FAILED.
    """
    message = exc.user_message
    kind = exc.kind
    if isinstance(exc, RateLimitedError):
        return FailureDecision(
            state=JobState.WAITING,
            error=message,
            error_kind=kind,
            retry_at=now + exc.retry_after,
            attempts=attempts,
            status_text="Rate limited by AnkerGames",
        )
    if isinstance(exc, ExternalHostError):
        return FailureDecision(JobState.FAILED, message, kind, error_url=exc.url, attempts=attempts)
    if isinstance(exc, VerificationError):
        return FailureDecision(JobState.FAILED, message, kind, error_url=exc.ticket_url, attempts=attempts)
    if isinstance(exc, DiskSpaceError) or not exc.retryable:
        return FailureDecision(JobState.FAILED, message, kind, attempts=attempts)
    if attempts >= len(backoff):
        return FailureDecision(JobState.FAILED, message, kind, attempts=attempts)
    delay = float(backoff[attempts])
    retry_number = attempts + 1
    return FailureDecision(
        state=JobState.WAITING,
        error=message,
        error_kind=kind,
        retry_at=now + delay,
        attempts=retry_number,
        status_text=f"Retry {retry_number} of {len(backoff)} in {format_duration(delay)}",
    )


# --- locations ---------------------------------------------------------------------------


def download_base_dir(download_dir_setting: str, library_root: str) -> str:
    """``settings.download_dir`` or ``<library_root>\\.ankerclient\\downloads``."""
    configured = (download_dir_setting or "").strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(os.path.join(library_root, LIBRARY_WORK_DIRNAME, "downloads"))


def archive_filename(link_filename: str, slug: str) -> str:
    """A Windows-safe archive name from the server's filename, else ``<slug>.zip``.

    Long names are shortened to :data:`MAX_ARCHIVE_NAME` characters without losing
    the extension (7-Zip and the installer pick the archive format from it).
    """
    raw = os.path.basename((link_filename or "").replace("\\", "/"))
    name = sanitize_windows_name(raw, fallback=None, max_length=len(raw) + 1)
    if name and len(name) > MAX_ARCHIVE_NAME:
        name = _shorten_keeping_extension(name)
    if name:
        return name
    stem = sanitize_windows_name(slug, fallback="download", max_length=MAX_ARCHIVE_NAME - 4)
    return f"{stem}.zip"


def _shorten_keeping_extension(name: str) -> str:
    stem, ext = os.path.splitext(name)
    if not ext or len(ext) > 16:
        return sanitize_windows_name(name, fallback=None, max_length=MAX_ARCHIVE_NAME)
    short_stem = sanitize_windows_name(stem, fallback=None, max_length=MAX_ARCHIVE_NAME - len(ext))
    return f"{short_stem}{ext}" if short_stem else ""


def job_work_dir(job: DownloadJob) -> str:
    """The job's private download folder (``<base>\\<job.id>``), "" when unknown or not ours."""
    if job.imported_archive or not job.archive_path or not job.id:
        return ""
    folder = os.path.dirname(os.path.abspath(job.archive_path))
    if os.path.basename(folder) != job.id:
        return ""
    return folder


def discard_job_files(job: DownloadJob, *, attempts: int = 3, retry_delay: float = 0.2) -> bool:
    """Delete the job's private download folder (partial data + archive).

    Never touches imported archives or anything outside ``<base>\\<job.id>``.
    Returns True when nothing of ours is left on disk.
    """
    folder = job_work_dir(job)
    if not folder or not os.path.isdir(folder):
        return True
    if is_dangerous_delete_target(folder):  # defensive: the folder name is a uuid
        log.error("Refusing to delete %s", folder)
        return False
    for attempt in range(attempts):
        try:
            shutil.rmtree(folder, onexc=_clear_readonly_and_retry)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            # Antivirus/indexers briefly lock freshly written files on Windows.
            if attempt + 1 == attempts:
                log.warning("Could not delete download folder %s: %s", folder, exc)
                return False
            time.sleep(retry_delay)
    return False


def remove_dir_if_empty(folder: str) -> None:
    if not folder:
        return
    try:
        os.rmdir(folder)
    except OSError:
        pass  # not empty / already gone / locked: leave it


def _clear_readonly_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error hook: clear the read-only attribute and try once more."""
    os.chmod(path, stat.S_IWRITE)
    func(path)
