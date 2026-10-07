"""Persistent download/install queue.

State machine (``JobState``)::

    QUEUED ─► RESOLVING ─► (VERIFYING) ─► DOWNLOADING ─► EXTRACTING/INSTALLING ─► COMPLETED
       ▲          │              │              │                 │
       │          └──────────────┴──────┬───────┴─────────────────┘
       │                                ▼
       ├──── resume/retry ──── PAUSED / WAITING(retry_at) / FAILED / CANCELLED

Rules
* Jobs persist in the ``jobs`` table (full ``DownloadJob`` JSON plus the
  manager's private fields under ``"_manager"``: pause reason, archive
  complete, install requested, last resolved link) on every state change and
  at most every 2 s during progress. ``jobs()`` returns copies ordered by
  ``position``. Jobs are loaded lazily on first use (so ``jobs()`` works
  before ``start()``).
* Loading: any job found in an active state (crash/exit) becomes PAUSED
  (DOWNLOADING/RESOLVING/VERIFYING; counted as paused by shutdown) or FAILED
  with a "Retry install" message (EXTRACTING/INSTALLING, archive kept).
  ``start()``: if ``settings.auto_resume_downloads``, PAUSED jobs that were
  paused by shutdown (never user-paused ones) are re-queued; otherwise loaded
  QUEUED/WAITING jobs are paused too, so nothing starts on its own.
* A single scheduler thread starts up to ``settings.max_concurrent_downloads``
  pipelines (each on its own named thread) in ``position`` order; WAITING jobs
  become eligible when ``clock() >= retry_at``. A job whose archive is already
  complete (install / install retry / imported archive) and a job that reached
  its install phase do not occupy a download slot — installs are serialised
  by their own lock instead.
* Pipeline per job: base-install check for PATCH/ADDON → disk-space pre-check
  (``install.diskspace``) → resolve (``LinkResolver``; a link resolved less
  than an hour ago is reused first on resume) → download (``HttpDownloader``;
  archive at ``<download_dir or library_root\\.ankerclient\\downloads>\\<job.id>\\<filename>``)
  → optional archive test (``verify_archive`` when
  ``settings.verify_archive_before_install``) → install (``Installer``;
  installs are serialised through one lock) → ``LibraryService.register_install``
  → COMPLETED, ``GameInstalled`` + ``Notification`` events.
  With ``settings.auto_install`` off, the job stops at a completed download
  and exposes ``install(job_id)`` (state COMPLETED with ``install_path == ""``).
  PATCH/ADDON jobs (and FULL re-installs of a managed install of the same slug)
  pass ``existing_install_path`` to the installer.
* ``LinkExpiredError`` during download → re-resolve once and continue the same
  partial file; repeated → FAILED. (Expiry of a reused stored link does not
  count — it simply triggers a fresh resolve. A ``LinkExpiredError`` raised
  while resolving, e.g. a vanished ticket page, is an ordinary retryable error.)
* ``RateLimitedError`` → WAITING with ``retry_at = now + retry_after``.
  Retryable errors (``AnkerError.retryable``) → WAITING with backoff
  (30 s, 2 min, 10 min) — three automatic retries per job run (the count is
  only reset by a user resume/retry or a completed install), then FAILED.
  Non-retryable → FAILED with ``error``, ``error_kind`` and ``error_url``
  (ticket URL for EXTERNAL_HOST/VERIFICATION so the UI can offer "Open in
  browser"). A damaged downloaded archive (``CorruptArchiveError``) is deleted
  before the retry so it is downloaded again; a damaged *imported* archive
  fails at once (retrying cannot repair it).
* A stop request (pause/cancel/shutdown/restart) always wins over an error
  raised while the pipeline unwinds, and the latest request is the one applied.
* ``pause``: cancel the pipeline token with reason "pause", keep partial data,
  state PAUSED. ``resume``/``retry``: back to QUEUED (``resume`` while a pause is
  still unwinding turns it into a restart). ``cancel``: stop, delete
  partial + archive (unless ``imported_archive``), state CANCELLED.
  ``remove`` deletes finished jobs from the list (a FAILED job's partial data
  is deleted; a running/unfinished job is cancelled first). ``move(job_id,
  index)`` reorders the queue (index into ``jobs()``).
* Duplicate ``enqueue`` for the same (slug, option.download_id) that is not
  finished returns the existing job.
* ``import_archive`` creates a job that skips resolve/download and installs a
  user-provided archive (never deletes that archive).
* Settings changes (``SettingsChanged``) apply live: ``speed_limit_kbps`` →
  ``rate_limiter.set_rate``; ``max_concurrent_downloads`` → scheduler re-checks;
  ``connections_per_download`` → running downloads restart (partial kept,
  stored link reused) with the new connection count.
* Events: ``JobAdded``, ``JobUpdated`` (throttled ≤4/s per job for progress;
  state changes immediately, in order), ``JobRemoved``, ``QueueChanged``,
  ``Notification`` (completed/failed, tray=True).
* ``shutdown(timeout)``: pause every running job with reason "shutdown"
  (resumable next start), join threads, flush pending progress. A worker that
  misses the deadline is recorded with the outcome it was asked for (paused by
  shutdown/user, or CANCELLED — a cancelled download never auto-resumes); no
  table write happens after ``shutdown`` returns.

Threading: all mutable job state lives behind one lock. Mutations queue their
events in an outbox that is published (in order, outside the job lock) by
whichever thread made the change, so the UI never sees a stale state after a
newer one and the job lock is never held during I/O or callbacks. Table writes
are queued in mutation order too and applied by a single writer — the
scheduler thread while it runs (so short-lived worker/engine threads never open
SQLite connections), otherwise the calling thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from anker_client.core.db import Database
from anker_client.core.errors import (
    AnkerError,
    CorruptArchiveError,
    ExtractionError,
    InstallError,
    IntegrityError,
    LinkExpiredError,
    OperationCancelled,
)
from anker_client.core.events import (
    Event,
    EventBus,
    GameInstalled,
    JobAdded,
    JobRemoved,
    JobUpdated,
    Notification,
    QueueChanged,
    SettingsChanged,
)
from anker_client.core.formatting import parse_size
from anker_client.core.models import (
    DownloadJob,
    DownloadKind,
    DownloadOption,
    ErrorKind,
    InstalledGame,
    InstallRequest,
    InstallResult,
    JobState,
    ResolvedLink,
    utc_now_iso,
)
from anker_client.core.paths import AppPaths
from anker_client.core.settings import Settings, SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads._manager_policy import (
    DEFAULT_BACKOFF,
    DOWNLOAD_PHASE_STATES,
    LINK_REUSE_MAX_AGE,
    REASON_CANCEL,
    REASON_PAUSE,
    REASON_RESTART,
    REASON_SHUTDOWN,
    LinkKeepsExpiringError,
    archive_filename,
    decide_failure,
    discard_job_files,
    download_base_dir,
    job_work_dir,
    remove_dir_if_empty,
)
from anker_client.services.downloads._manager_state import (
    PAUSED_BY_SHUTDOWN,
    PAUSED_BY_USER,
    JobMeta,
    JobRow,
    JobStore,
    encode_job,
)
from anker_client.services.downloads.engine import DownloadProgress, HttpDownloader
from anker_client.services.downloads.ratelimit import RateLimiter
from anker_client.services.downloads.resolver import LinkResolver
from anker_client.services.install import diskspace
from anker_client.services.install.installer import Installer
from anker_client.services.library import LibraryService

log = logging.getLogger(__name__)

#: Higher wins when several stop requests reach the same running pipeline
#: (a user pause must never turn into an auto-resumable shutdown pause).
_STOP_PRIORITY = {REASON_RESTART: 0, REASON_SHUTDOWN: 1, REASON_PAUSE: 2, REASON_CANCEL: 3}

_IMPORT_LABELS = {
    DownloadKind.FULL: "Imported archive",
    DownloadKind.PATCH: "Imported update",
    DownloadKind.ADDON: "Imported add-on",
}

VerifyArchive = Callable[[str, CancelToken], None]


@dataclass(slots=True)
class _Run:
    """A running pipeline (one worker thread)."""

    token: CancelToken
    thread: threading.Thread
    install_phase: bool = False  # past the download: no longer occupies a download slot
    stop_reason: str = ""  # strongest stop request so far (see _STOP_PRIORITY)


@dataclass(frozen=True, slots=True)
class _DbOp:
    """Queued ``jobs`` table changes (applied in order by a single writer)."""

    rows: tuple[JobRow, ...] = ()
    deletes: tuple[str, ...] = ()


class DownloadManager:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        events: EventBus,
        paths: AppPaths,
        resolver: LinkResolver,
        downloader_factory: Callable[[int], HttpDownloader],  # connections -> downloader
        rate_limiter: RateLimiter,
        installer: Installer,
        library: LibraryService,
        verify_archive: VerifyArchive | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        poll_interval: float = 0.5,
        progress_event_interval: float = 0.25,
        progress_persist_interval: float = 2.0,
    ) -> None:
        self._store = JobStore(db)
        self._settings = settings
        self._events = events
        self._paths = paths
        self._resolver = resolver
        self._downloader_factory = downloader_factory
        self._rate_limiter = rate_limiter
        self._installer = installer
        self._library = library
        self._verify_archive = verify_archive
        self._clock = clock
        self._monotonic = monotonic
        self._backoff = tuple(float(b) for b in backoff)
        self._poll_interval = max(0.01, float(poll_interval))
        self._event_interval = max(0.0, float(progress_event_interval))
        self._persist_interval = max(0.0, float(progress_persist_interval))

        self._lock = threading.RLock()  # guards everything below
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[str, DownloadJob] = {}
        self._meta: dict[str, JobMeta] = {}
        self._running: dict[str, _Run] = {}
        self._cleaning: set[str] = set()  # jobs whose files are being deleted (not schedulable)
        self._loaded_ids: set[str] = set()
        self._outbox: deque[Event] = deque()  # events to publish, in mutation order
        self._db_queue: deque[_DbOp] = deque()  # table writes, in mutation order
        self._loaded = False
        self._started = False
        self._stopping = False
        self._closed = False
        self._wake = False
        self._scheduler: threading.Thread | None = None
        self._unsubscribe: Callable[[], None] | None = None

        self._out_lock = threading.RLock()  # serialises publishing of the outbox
        self._draining_thread: int | None = None
        # One writer at a time. While the scheduler runs it is the only writer, so the
        # per-thread SQLite connections of short-lived worker/engine threads never pile up.
        self._db_lock = threading.Lock()
        self._install_lock = threading.Lock()

    # ====================================================================================
    # lifecycle
    # ====================================================================================
    def start(self) -> None:
        with self._cond:
            if self._started or self._stopping:
                return
            self._started = True
            self._ensure_loaded_locked()
            settings = self._settings.get()
            for job_id in sorted(self._loaded_ids):
                job = self._jobs.get(job_id)
                if job is not None:
                    self._apply_startup_resume_locked(job, settings.auto_resume_downloads)
            self._queue_locked(event=QueueChanged())
            self._scheduler = threading.Thread(target=self._scheduler_loop, name="anker-dl-scheduler", daemon=True)
            self._scheduler.start()
        self._unsubscribe = self._events.subscribe(SettingsChanged, self._on_settings_changed)
        self._apply_rate_limit()
        self._drain()
        log.info("Download manager started with %d job(s)", len(self._jobs))

    def shutdown(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        tokens: list[tuple[CancelToken, str]] = []
        with self._cond:
            if self._closed:
                return
            self._stopping = True
            for job_id, run in self._running.items():
                self._request_stop_locked(job_id, REASON_SHUTDOWN)
                # Cancel even when a stronger request (e.g. a user pause) was recorded but its
                # issuer has not fired the token yet; the worker applies run.stop_reason.
                tokens.append((run.token, run.stop_reason))
            runs = list(self._running.values())
            scheduler = self._scheduler
            self._notify_locked()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for token, reason in tokens:
            token.cancel(reason)
        for run in runs:
            run.thread.join(max(0.0, deadline - time.monotonic()))
        if scheduler is not None:
            scheduler.join(max(0.0, deadline - time.monotonic()))
        with self._cond:
            for job_id, run in list(self._running.items()):
                if run.thread.is_alive():
                    log.warning("Download worker for %s did not stop in time", job_id)
                    self._mark_straggler_locked(job_id)
            for job in self._jobs.values():
                meta = self._meta[job.id]
                if meta.persist_pending or meta.event_pending:
                    self._touch_state_locked(job, stamp=False)
        self._drain()
        # Under the writer lock: once shutdown() returns no write can still be in flight
        # (the container closes the database right after), even from a straggler worker.
        with self._db_lock, self._cond:
            self._closed = True
        log.info("Download manager stopped")

    # ====================================================================================
    # queries
    # ====================================================================================
    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            self._ensure_loaded_locked()
            return [job.copy() for job in self._ordered_locked()]

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            self._ensure_loaded_locked()
            job = self._jobs.get(job_id)
            return job.copy() if job is not None else None

    def active_jobs(self) -> list[DownloadJob]:
        with self._lock:
            self._ensure_loaded_locked()
            return [job.copy() for job in self._ordered_locked() if job.state.is_active]

    def job_for(self, slug: str) -> DownloadJob | None:
        """The most relevant unfinished job for ``slug`` (for "Downloading…" buttons).

        Running jobs first, then queued/waiting ones, then paused ones; ties by queue position.
        """

        def rank(job: DownloadJob) -> tuple[int, int]:
            if job.state.is_active:
                group = 0
            elif job.state in (JobState.QUEUED, JobState.WAITING):
                group = 1
            else:
                group = 2
            return group, job.position

        if not slug:  # imported archives may have no slug; they belong to no store page
            return None
        with self._lock:
            self._ensure_loaded_locked()
            candidates = [j for j in self._jobs.values() if j.slug == slug and not j.state.is_finished]
            if not candidates:
                return None
            return min(candidates, key=rank).copy()

    # ====================================================================================
    # commands
    # ====================================================================================
    def enqueue(
        self,
        *,
        slug: str,
        title: str,
        option: DownloadOption,
        library_root: str | None = None,
        cover_url: str = "",
        version: str = "",
        source_updated_date: str = "",
        genres: list[str] | None = None,
    ) -> DownloadJob:
        if not slug:
            raise ValueError("enqueue() needs a game slug")
        root = library_root or self._default_library_root(slug, option.kind)
        with self._cond:
            self._ensure_loaded_locked()
            for existing in self._ordered_locked():
                if (
                    existing.slug == slug
                    and not existing.imported_archive
                    and existing.option.download_id == option.download_id
                    and not existing.state.is_finished
                ):
                    log.info("Download of %s (%s) is already queued as %s", slug, option.label, existing.id)
                    return existing.copy()
            job = DownloadJob(
                id=uuid.uuid4().hex,
                slug=slug,
                title=title or slug,
                option=option,
                library_root=root,
                cover_url=cover_url,
                target_version=version,
                source_updated_date=source_updated_date,
                genres=list(genres or []),
                position=self._next_position_locked(),
            )
            result = self._add_job_locked(job, JobMeta())
        self._drain()
        log.info("Queued %s (%s) as job %s", slug, option.label, job.id)
        return result

    def import_archive(
        self,
        archive_path: str,
        *,
        slug: str,
        title: str,
        kind: DownloadKind = DownloadKind.FULL,
        library_root: str | None = None,
        cover_url: str = "",
        version: str = "",
    ) -> DownloadJob:
        path = os.path.abspath(archive_path)
        if not os.path.isfile(path):
            raise InstallError(f"The archive {path} does not exist.")
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise InstallError(f"The archive {path} cannot be read.", detail=str(exc)) from exc
        root = library_root or self._default_library_root(slug, kind)
        option = DownloadOption(download_id=0, label=_IMPORT_LABELS.get(kind, "Imported archive"), kind=kind)
        with self._cond:
            self._ensure_loaded_locked()
            job = DownloadJob(
                id=uuid.uuid4().hex,
                slug=slug,
                title=title or slug or os.path.splitext(os.path.basename(path))[0],
                option=option,
                library_root=root,
                cover_url=cover_url,
                target_version=version,
                position=self._next_position_locked(),
                archive_path=path,
                filename=os.path.basename(path),
                bytes_done=size,
                bytes_total=size,
                imported_archive=True,
            )
            result = self._add_job_locked(job, JobMeta(archive_complete=True, install_requested=True))
        self._drain()
        log.info("Queued install of imported archive %s as job %s", path, job.id)
        return result

    def pause(self, job_id: str) -> None:
        request: tuple[CancelToken, str] | None = None
        with self._cond:
            job = self._job_locked(job_id)
            if job is None:
                return
            if self._is_running_locked(job_id):
                request = self._request_stop_locked(job_id, REASON_PAUSE)
                if request is not None:
                    job.status_text = "Pausing…"
                    self._touch_state_locked(job)
            elif job.state in (JobState.QUEUED, JobState.WAITING):
                self._set_paused_locked(job, PAUSED_BY_USER)
        if request is not None:
            request[0].cancel(request[1])
        self._drain()

    def resume(self, job_id: str) -> None:
        with self._cond:
            job = self._job_locked(job_id)
            if job is None:
                return
            if self._is_running_locked(job_id):
                run = self._running[job_id]
                if run.stop_reason == REASON_PAUSE and not self._stopping:
                    # Still unwinding a pause: let it come back as QUEUED instead of PAUSED.
                    run.stop_reason = REASON_RESTART
                    job.status_text = "Resuming…"
                    self._touch_state_locked(job)
            elif job.state in (JobState.PAUSED, JobState.WAITING):
                self._requeue_locked(job)
        self._drain()

    def retry(self, job_id: str) -> None:
        with self._cond:
            job = self._job_locked(job_id)
            if job is None or self._is_running_locked(job_id):
                return
            if job.state not in (JobState.FAILED, JobState.CANCELLED, JobState.WAITING, JobState.PAUSED):
                return
            meta = self._meta[job_id]
            if job.state is JobState.CANCELLED and not job.imported_archive:
                self._reset_download_locked(job)
            if meta.archive_complete:
                meta.install_requested = True
            self._requeue_locked(job)
        self._drain()

    def cancel(self, job_id: str) -> None:
        request: tuple[CancelToken, str] | None = None
        cleanup: DownloadJob | None = None
        with self._cond:
            job = self._job_locked(job_id)
            if job is None:
                return
            if self._is_running_locked(job_id):
                request = self._request_stop_locked(job_id, REASON_CANCEL)
                if request is not None:
                    job.status_text = "Cancelling…"
                    self._touch_state_locked(job)
            elif job.state is JobState.CANCELLED or (job.state is JobState.COMPLETED and job.install_path):
                return
            else:
                cleanup = job.copy()
                if not job.imported_archive:
                    self._cleaning.add(job_id)
                self._set_cancelled_locked(job)
        if request is not None:
            request[0].cancel(request[1])
        self._drain()
        if cleanup is not None and not cleanup.imported_archive:
            self._discard_files_then_release(cleanup)

    def install(self, job_id: str) -> None:
        """Install a downloaded-but-not-installed job (``auto_install`` off, or install retry)."""
        with self._cond:
            job = self._job_locked(job_id)
            if job is None or self._is_running_locked(job_id):
                return
            meta = self._meta[job_id]
            downloaded = job.state is JobState.COMPLETED and not job.install_path
            failed_install = job.state in (JobState.FAILED, JobState.PAUSED, JobState.WAITING) and meta.archive_complete
            if not (downloaded or failed_install):
                log.info("install(%s) ignored in state %s", job_id, job.state)
                return
            if not job.archive_path or not os.path.isfile(job.archive_path):
                meta.archive_complete = job.imported_archive
                message = (
                    f"The archive {job.archive_path} no longer exists. Import it again."
                    if job.imported_archive
                    else "The downloaded archive no longer exists. Retry to download it again."
                )
                self._fail_locked(job, InstallError(message))
            else:
                meta.install_requested = True
                self._requeue_locked(job)
        self._drain()

    def remove(self, job_id: str) -> None:
        request: tuple[CancelToken, str] | None = None
        cleanup: DownloadJob | None = None
        cancel_first = False
        with self._cond:
            job = self._job_locked(job_id)
            if job is None:
                return
            if self._is_running_locked(job_id):
                self._meta[job_id].remove_when_stopped = True
                request = self._request_stop_locked(job_id, REASON_CANCEL)
                if request is not None:
                    job.status_text = "Cancelling…"
                    self._touch_state_locked(job)
            elif not job.state.is_finished:
                cancel_first = True
            else:
                if self._should_discard_on_remove(job):
                    cleanup = job.copy()
                self._remove_locked(job_id)
        if request is not None:
            request[0].cancel(request[1])
        self._drain()
        if cancel_first:
            self.cancel(job_id)
            self.remove(job_id)
        elif cleanup is not None:
            discard_job_files(cleanup)

    def clear_finished(self) -> None:
        cleanups: list[DownloadJob] = []
        with self._cond:
            self._ensure_loaded_locked()
            finished = [j for j in self._ordered_locked() if j.state.is_finished]
            if not finished:
                return
            for job in finished:
                if self._should_discard_on_remove(job):
                    cleanups.append(job.copy())
                self._remove_locked(job.id)
            self._queue_locked(event=QueueChanged())
        self._drain()
        for job in cleanups:
            discard_job_files(job)

    def move(self, job_id: str, index: int) -> None:
        with self._cond:
            self._ensure_loaded_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return
            ordered = self._ordered_locked()
            ordered.remove(job)
            ordered.insert(max(0, min(int(index), len(ordered))), job)
            rows: list[JobRow] = []
            for position, item in enumerate(ordered):
                if item.position != position:
                    item.position = position
                    rows.append(encode_job(item, self._meta[item.id]))
            if not rows:
                return
            self._queue_locked(rows=tuple(rows), event=QueueChanged())
            self._notify_locked()
        self._drain()

    def pause_all(self) -> None:
        with self._lock:
            self._ensure_loaded_locked()
            targets = [
                job.id
                for job in self._ordered_locked()
                if job.state in (JobState.QUEUED, JobState.WAITING)
                or (job.state in DOWNLOAD_PHASE_STATES and self._is_running_locked(job.id))
            ]
        for job_id in targets:
            self.pause(job_id)

    def resume_all(self) -> None:
        with self._lock:
            self._ensure_loaded_locked()
            targets = [job.id for job in self._ordered_locked() if job.state is JobState.PAUSED]
        for job_id in targets:
            self.resume(job_id)

    # ====================================================================================
    # scheduler
    # ====================================================================================
    def _scheduler_loop(self) -> None:
        while True:
            try:
                with self._cond:
                    if self._stopping:
                        return
                    self._start_eligible_locked()
                    timeout = self._flush_throttled_locked()
                self._drain()
                self._flush_db()
            except Exception:
                # One bad pass must never stop the whole queue for the rest of the session.
                log.exception("Download scheduler pass failed")
                timeout = max(self._poll_interval, 1.0)
            with self._cond:
                if self._stopping:
                    return
                if not self._wake:
                    self._cond.wait(timeout)
                self._wake = False

    def _start_eligible_locked(self) -> None:
        limit = max(1, self._settings.get().max_concurrent_downloads)
        busy = sum(1 for run in self._running.values() if not run.install_phase)
        now = self._clock()
        for job in self._ordered_locked():
            if job.id in self._running or job.id in self._cleaning:
                continue
            due = job.state is JobState.QUEUED or (
                job.state is JobState.WAITING and (job.retry_at is None or job.retry_at <= now)
            )
            if not due:
                continue
            install_only = self._archive_ready_locked(job)
            if not install_only and busy >= limit:
                continue
            if self._launch_locked(job, install_only=install_only) and not install_only:
                busy += 1

    def _launch_locked(self, job: DownloadJob, *, install_only: bool) -> bool:
        """Start the pipeline thread of ``job``; False when the thread could not be started."""
        token = CancelToken()
        thread = threading.Thread(
            target=self._worker,
            args=(job.id, token),
            name=f"anker-dl-{job.id[:8]}",
            daemon=True,
        )
        job.state = JobState.EXTRACTING if install_only else JobState.RESOLVING
        job.status_text = "Waiting to install…" if install_only else "Starting…"
        job.retry_at = None
        job.error = ""
        job.error_kind = None
        job.error_url = ""
        job.completed_at = ""
        job.speed_bps = 0.0
        job.eta_seconds = None
        job.phase_progress = 0.0
        job.started_at = job.started_at or utc_now_iso()
        self._meta[job.id].pause_reason = ""
        self._running[job.id] = _Run(token=token, thread=thread, install_phase=install_only)
        self._touch_state_locked(job)
        log.info("Starting job %s (%s)%s", job.id, job.slug, " — install only" if install_only else "")
        try:
            thread.start()
        except RuntimeError as exc:  # e.g. "can't start new thread"
            log.error("Could not start a worker for job %s: %s", job.id, exc)
            del self._running[job.id]
            self._fail_locked(job, AnkerError("The download could not be started. Retry it.", detail=repr(exc)))
            return False
        return True

    def _next_wake_locked(self, now_mono: float) -> float:
        timeout = self._poll_interval
        now = self._clock()
        for job in self._jobs.values():
            # Only future deadlines: a due job that is still waiting for a free slot is started
            # when a slot frees up (workers notify), so it must not make the scheduler spin.
            if job.state is JobState.WAITING and job.retry_at is not None and job.retry_at > now:
                timeout = min(timeout, max(0.01, job.retry_at - now))
            meta = self._meta[job.id]
            if meta.event_pending:
                timeout = min(timeout, max(0.01, meta.last_event_at + self._event_interval - now_mono))
            if meta.persist_pending:
                timeout = min(timeout, max(0.01, meta.last_persist_at + self._persist_interval - now_mono))
        return timeout

    def _flush_throttled_locked(self) -> float:
        """Queue owed (throttled) progress events/writes that are due; returns the next wait timeout."""
        for job in self._jobs.values():
            meta = self._meta[job.id]
            if meta.event_pending or meta.persist_pending:
                self._flush_job_locked(job)
        return self._next_wake_locked(self._monotonic())

    def _notify_locked(self) -> None:
        self._wake = True
        self._cond.notify_all()

    # ====================================================================================
    # worker / pipeline
    # ====================================================================================
    def _worker(self, job_id: str, token: CancelToken) -> None:
        try:
            try:
                self._pipeline(job_id, token)
            except Exception as exc:
                self._handle_stop(job_id, token, exc)
        except Exception:
            # _finish_worker turns a job left in an active state into FAILED.
            log.exception("Download worker for job %s crashed", job_id)
        finally:
            self._finish_worker(job_id)

    def _pipeline(self, job_id: str, token: CancelToken) -> None:
        job = self._snapshot(job_id)
        if job.option.kind is not DownloadKind.FULL:
            self._require_base_install(job)
        if not self._archive_ready(job_id):
            self._occupy_download_slot(job_id)
            self._download_phase(job_id, token)
        if not self._should_install(job_id):
            self._complete_download_only(job_id)
            return
        self._install_phase(job_id, token)

    # --- download -------------------------------------------------------------------------
    def _occupy_download_slot(self, job_id: str) -> None:
        """A job launched as install-only whose archive vanished downloads after all:
        count it against ``max_concurrent_downloads`` from now on."""
        with self._lock:
            run = self._running.get(job_id)
            if run is not None:
                run.install_phase = False

    def _download_phase(self, job_id: str, token: CancelToken) -> None:
        settings = self._settings.get()
        job = self._snapshot(job_id)
        base = download_base_dir(settings.download_dir, job.library_root)
        estimate = job.bytes_total or parse_size(job.option.size_text)
        space_dir = os.path.dirname(job.archive_path) if job.archive_path else base
        self._check_space(estimate, download_dir=space_dir, library_root=job.library_root, already=job.bytes_done)
        token.raise_if_cancelled()

        link, fresh = self._obtain_link(job_id, token, allow_stored=True)
        dest = self._prepare_destination(job_id, link, base)
        downloader = self._downloader_factory(max(1, settings.connections_per_download))
        partial = self._partial_size(downloader, dest)
        self._check_space(link.size, download_dir=os.path.dirname(dest), library_root=job.library_root, already=partial)

        relinked = False
        while True:
            token.raise_if_cancelled()
            self._begin_download(job_id, link, partial)
            try:
                downloader.download(
                    link,
                    dest,
                    token=token,
                    on_progress=lambda progress: self._on_download_progress(job_id, progress),
                )
                break
            except LinkExpiredError as exc:
                token.raise_if_cancelled()
                if fresh and relinked:
                    raise LinkKeepsExpiringError(detail=exc.detail or str(exc)) from exc
                relinked = relinked or fresh
                log.info("Download link for job %s expired; resolving a new one", job_id)
                with self._lock:
                    self._meta[job_id].forget_link()
                link, fresh = self._obtain_link(job_id, token, allow_stored=False)
                partial = self._partial_size(downloader, dest)
            except IntegrityError:
                # The engine's byte count disagreed with the server: never resume that data.
                self._discard_partial(downloader, dest)
                raise
        self._finish_download(job_id, dest)

    def _obtain_link(self, job_id: str, token: CancelToken, *, allow_stored: bool) -> tuple[ResolvedLink, bool]:
        """A usable link and whether it was freshly resolved (vs. reused from the last run)."""
        with self._lock:
            meta = self._meta[job_id]
            if allow_stored and meta.link and self._clock() - meta.link_resolved_at < LINK_REUSE_MAX_AGE:
                try:
                    stored = ResolvedLink.from_dict(meta.link)
                except TypeError:
                    stored = None
                if stored is not None and stored.url:
                    return stored, False
            job = self._jobs[job_id].copy()
        link = self._resolver.resolve(
            job.option,
            slug=job.slug,
            title=job.title,
            job_id=job.id,
            token=token,
            on_state=lambda state, text: self._on_resolver_state(job_id, state, text),
        )
        token.raise_if_cancelled()
        with self._lock:
            meta = self._meta[job_id]
            meta.link = link.to_dict()
            meta.link_resolved_at = self._clock()
        return link, True

    def _prepare_destination(self, job_id: str, link: ResolvedLink, base: str) -> str:
        with self._lock:
            job = self._jobs[job_id]
            if not job.archive_path:
                name = archive_filename(link.filename, job.slug)
                job.archive_path = os.path.join(base, job.id, name)
                job.filename = name
            dest = job.archive_path
        folder = os.path.dirname(dest)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise AnkerError(
                f"Cannot create the download folder {folder}: {exc.strerror or exc}", detail=repr(exc)
            ) from exc
        return dest

    def _begin_download(self, job_id: str, link: ResolvedLink, partial: int) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.state = JobState.DOWNLOADING
            job.status_text = ""
            job.resolved_url = link.url
            job.etag = link.etag
            if link.size:
                job.bytes_total = link.size
            job.bytes_done = max(0, partial)
            job.speed_bps = 0.0
            job.eta_seconds = None
            self._touch_state_locked(job)
        self._drain()

    def _on_download_progress(self, job_id: str, progress: DownloadProgress) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state is not JobState.DOWNLOADING:
                return
            job.bytes_done = max(0, int(progress.bytes_done))
            if progress.bytes_total:
                job.bytes_total = int(progress.bytes_total)
            job.speed_bps = max(0.0, float(progress.speed_bps))
            job.eta_seconds = progress.eta_seconds
            self._touch_progress_locked(job)
        self._drain()

    def _on_resolver_state(self, job_id: str, state: JobState, text: str) -> None:
        if state not in (JobState.RESOLVING, JobState.VERIFYING):
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or (job.state is state and job.status_text == text):
                return
            job.state = state
            job.status_text = text
            self._touch_state_locked(job)
        self._drain()

    def _finish_download(self, job_id: str, dest: str) -> None:
        try:
            size = os.path.getsize(dest)
        except OSError as exc:
            raise AnkerError("The downloaded file disappeared before it could be installed.", detail=repr(exc)) from exc
        with self._lock:
            job = self._jobs[job_id]
            meta = self._meta[job_id]
            job.bytes_done = size
            job.bytes_total = size
            job.speed_bps = 0.0
            job.eta_seconds = None
            # ``attempts`` is deliberately NOT reset here: a damaged archive is deleted and
            # downloaded again, and resetting would turn that into an endless download loop.
            job.status_text = "Download complete"
            meta.archive_complete = True
            meta.forget_link()
            self._touch_state_locked(job)
        self._drain()
        log.info("Job %s downloaded %s (%d bytes)", job_id, dest, size)

    def _complete_download_only(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.state = JobState.COMPLETED
            job.install_path = ""
            job.completed_at = utc_now_iso()
            job.status_text = "Downloaded — ready to install"
            job.speed_bps = 0.0
            job.eta_seconds = None
            self._touch_state_locked(job)
            self._queue_locked(
                event=Notification(
                    title="Download complete",
                    message=f"{job.title} is ready to install.",
                    level="success",
                    tray=True,
                )
            )
        self._drain()

    # --- install --------------------------------------------------------------------------
    def _install_phase(self, job_id: str, token: CancelToken) -> None:
        with self._cond:
            self._meta[job_id].install_requested = True
            run = self._running.get(job_id)
            if run is not None:
                run.install_phase = True
            job = self._jobs[job_id]
            job.state = JobState.EXTRACTING
            job.phase_progress = 0.0
            job.status_text = (
                "Waiting for another installation to finish…" if self._install_lock.locked() else "Preparing…"
            )
            self._touch_state_locked(job)
            self._notify_locked()  # a download slot became free
        self._drain()

        self._acquire_install_lock(token)
        try:
            job = self._snapshot(job_id)
            request, is_update = self._build_install_request(job)
            size = job.bytes_total or self._file_size(job.archive_path)
            self._check_space(
                size,
                download_dir=os.path.dirname(job.archive_path),
                library_root=request.library_root,
                already=size or 0,
            )
            try:
                if self._verify_archive is not None and self._settings.get().verify_archive_before_install:
                    self._run_verify_step(job_id, job.archive_path, token)
                token.raise_if_cancelled()
                self._set_install_state(job_id, JobState.EXTRACTING, 0.0, "Extracting…")
                result = self._installer.install(
                    request,
                    token=token,
                    on_progress=lambda phase, fraction: self._on_install_progress(job_id, phase, fraction),
                    keep_archive=job.imported_archive,
                )
            except CorruptArchiveError:
                if not token.cancelled and not job.imported_archive:
                    self._discard_corrupt_archive(job_id)
                raise
        finally:
            self._install_lock.release()
        self._complete_install(job_id, request, result, is_update=is_update)

    def _acquire_install_lock(self, token: CancelToken) -> None:
        while True:
            token.raise_if_cancelled()
            if self._install_lock.acquire(timeout=0.2):
                return

    def _run_verify_step(self, job_id: str, archive: str, token: CancelToken) -> None:
        assert self._verify_archive is not None
        self._set_install_state(job_id, JobState.EXTRACTING, 0.0, "Testing archive…")
        try:
            self._verify_archive(archive, token)
        except AnkerError:
            raise
        except OSError as exc:
            raise ExtractionError(detail=repr(exc)) from exc

    def _set_install_state(self, job_id: str, state: JobState, fraction: float, text: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.state = state
            job.phase_progress = max(0.0, min(1.0, fraction))
            job.status_text = text
            self._touch_state_locked(job)
        self._drain()

    def _on_install_progress(self, job_id: str, phase: str, fraction: float) -> None:
        state = JobState.INSTALLING if phase == "installing" else JobState.EXTRACTING
        try:
            value = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in (JobState.EXTRACTING, JobState.INSTALLING):
                return
            job.phase_progress = value
            if job.state is not state:
                job.state = state
                job.status_text = "Installing…" if state is JobState.INSTALLING else "Extracting…"
                self._touch_state_locked(job)
            else:
                self._touch_progress_locked(job)
        self._drain()

    def _build_install_request(self, job: DownloadJob) -> tuple[InstallRequest, bool]:
        """The installer request and whether it targets an existing install."""
        existing = self._installed_game(job.slug)
        library_root = job.library_root
        existing_path = ""
        if job.option.kind is not DownloadKind.FULL:
            if existing is None:
                raise InstallError(_base_missing_message(job))
            existing_path = existing.path
            library_root = existing.library_root or library_root
        elif existing is not None and existing.managed and existing.path and os.path.isdir(existing.path):
            existing_path = existing.path
            library_root = existing.library_root or library_root
        version = job.target_version
        if job.option.kind is DownloadKind.PATCH and job.option.to_version:
            version = job.option.to_version
        request = InstallRequest(
            archive_path=job.archive_path,
            slug=job.slug,
            title=job.title,
            option=job.option,
            library_root=library_root,
            version=version,
            source_updated_date=job.source_updated_date,
            cover_url=job.cover_url,
            genres=list(job.genres),
            existing_install_path=existing_path,
        )
        return request, bool(existing_path)

    def _complete_install(
        self, job_id: str, request: InstallRequest, result: InstallResult, *, is_update: bool
    ) -> None:
        game: InstalledGame | None = None
        try:
            game = self._library.register_install(result, request)
        except Exception:
            # The manifest on disk is the source of truth; the next library scan picks it up.
            log.exception("Could not register the install of %s at %s", request.slug, result.install_path)
        job = self._snapshot(job_id)
        archive_gone = not job.imported_archive and not os.path.exists(job.archive_path)
        if archive_gone:
            remove_dir_if_empty(job_work_dir(job))
        title = game.title if game is not None and game.title else job.title
        fallback_id = job.slug or f"local:{os.path.basename(result.install_path).casefold()}"
        install_id = game.install_id if game is not None else fallback_id
        with self._lock:
            job = self._jobs[job_id]
            meta = self._meta[job_id]
            job.state = JobState.COMPLETED
            job.install_path = result.install_path
            job.completed_at = utc_now_iso()
            job.phase_progress = 1.0
            job.status_text = "Updated" if is_update else "Installed"
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.attempts = 0
            meta.install_requested = False
            if archive_gone:
                meta.archive_complete = False
            self._touch_state_locked(job)
            self._queue_locked(
                event=GameInstalled(
                    install_id=install_id,
                    title=title,
                    needs_executable=not result.executable,
                    is_update=is_update,
                )
            )
            verb = "updated" if is_update else "installed"
            self._queue_locked(
                event=Notification(
                    title="Ready to play",
                    message=f"{title} was {verb}.",
                    level="success",
                    tray=True,
                )
            )
        self._drain()
        log.info("Job %s %s %s at %s", job_id, "updated" if is_update else "installed", job.slug, result.install_path)

    def _discard_corrupt_archive(self, job_id: str) -> None:
        job = self._snapshot(job_id)
        discard_job_files(job)
        with self._lock:
            live = self._jobs.get(job_id)
            if live is None:
                return
            self._reset_download_locked(live)

    # --- outcome handling -------------------------------------------------------------
    def _handle_stop(self, job_id: str, token: CancelToken, exc: Exception) -> None:
        """Apply the outcome of a pipeline that raised ``exc``.

        The outcome is decided under the lock from the *current* stop request
        (``_Run.stop_reason``), together with the state change, so a pause, resume
        or cancel that lands while the worker unwinds is never lost: any stop
        request wins over an error, ``resume`` during a pause turns it into a restart,
        and ``cancel`` deletes the job's files first (outside the lock — the job is
        still registered as running, so nothing can reschedule it meanwhile).
        """
        stopped = token.cancelled or isinstance(exc, OperationCancelled)
        error = None if stopped else self._pipeline_error(job_id, exc)
        discarded = False
        files_left = False
        while True:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                run = self._running.get(job_id)
                reason = run.stop_reason if run is not None else ""
                if not reason and error is None:
                    # Cancelled without a request of ours (e.g. the user closed the browser check).
                    reason = token.reason if token.reason in _STOP_PRIORITY else REASON_PAUSE
                if reason == REASON_CANCEL and not job.imported_archive and not discarded:
                    snapshot = job.copy()
                else:
                    self._apply_outcome_locked(job, reason, error, files_left=files_left)
                    break
            files_left = not discard_job_files(snapshot)
            discarded = True
        self._drain()
        log.info("Job %s stopped (%s)", job_id, reason or "failed")

    def _pipeline_error(self, job_id: str, exc: Exception) -> AnkerError:
        """``exc`` as the ``AnkerError`` the job fails with (logged once here)."""
        with self._lock:
            job = self._jobs.get(job_id)
            slug = job.slug if job is not None else ""
            imported = job is not None and job.imported_archive
        if not isinstance(exc, AnkerError):
            log.error("Unexpected error in download job %s (%s)", job_id, slug, exc_info=exc)
            return AnkerError(
                "Something went wrong while processing this download. See the log for details.",
                detail=repr(exc),
            )
        log.warning("Job %s (%s) failed: %s %s", job_id, slug, exc.user_message, exc.detail)
        if imported and isinstance(exc, CorruptArchiveError):
            # Retrying cannot repair a user-supplied archive (and we never delete it).
            return ExtractionError(
                "The archive is damaged. Download it again, then import the new copy.",
                detail=exc.detail or exc.user_message,
            )
        return exc

    def _apply_outcome_locked(
        self, job: DownloadJob, reason: str, error: AnkerError | None, *, files_left: bool = False
    ) -> None:
        if reason == REASON_CANCEL:
            self._set_cancelled_locked(job, files_left=files_left)
        elif reason == REASON_RESTART:
            job.state = JobState.QUEUED
            job.status_text = ""
            job.speed_bps = 0.0
            job.eta_seconds = None
            self._touch_state_locked(job)
            self._notify_locked()
        elif reason == REASON_SHUTDOWN:
            self._set_paused_locked(job, PAUSED_BY_SHUTDOWN)
        elif reason == REASON_PAUSE or error is None:
            self._set_paused_locked(job, PAUSED_BY_USER)
        else:
            self._fail_locked(job, error)

    def _finish_worker(self, job_id: str) -> None:
        remove_after = False
        with self._cond:
            self._running.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job is not None and job.state.is_active:
                # Safety net: a worker must never leave a job "running" with no thread behind it.
                log.error("Worker of job %s ended in state %s; marking it failed", job_id, job.state.value)
                self._fail_locked(job, AnkerError("The download stopped unexpectedly. Retry it."))
            meta = self._meta.get(job_id)
            remove_after = meta is not None and meta.remove_when_stopped
            self._notify_locked()
        self._drain()
        if remove_after:
            self.remove(job_id)

    # ====================================================================================
    # state helpers (call with self._lock held)
    # ====================================================================================
    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        rows: list[JobRow] = []
        for job, meta in self._store.load():
            if job.id in self._jobs:
                continue
            if _recover_loaded_job(job, meta):
                job.updated_at = utc_now_iso()
                rows.append(encode_job(job, meta))
            self._jobs[job.id] = job
            self._meta[job.id] = meta
            self._loaded_ids.add(job.id)
        for position, job in enumerate(self._ordered_locked()):
            if job.position != position:
                job.position = position
                rows.append(encode_job(job, self._meta[job.id]))
        if rows:
            # Persist immediately: a crash before the first drain must not lose recovery.
            self._store.upsert(_dedupe_rows(rows))

    def _apply_startup_resume_locked(self, job: DownloadJob, auto_resume: bool) -> None:
        meta = self._meta[job.id]
        if auto_resume:
            if job.state is JobState.PAUSED and meta.pause_reason == PAUSED_BY_SHUTDOWN:
                self._requeue_locked(job, reset_attempts=False)
        elif job.state in (JobState.QUEUED, JobState.WAITING):
            self._set_paused_locked(job, PAUSED_BY_SHUTDOWN)

    def _job_locked(self, job_id: str) -> DownloadJob | None:
        self._ensure_loaded_locked()
        return self._jobs.get(job_id)

    def _ordered_locked(self) -> list[DownloadJob]:
        return sorted(self._jobs.values(), key=lambda j: (j.position, j.created_at))

    def _next_position_locked(self) -> int:
        return max((job.position for job in self._jobs.values()), default=-1) + 1

    def _is_running_locked(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        return job_id in self._running and job is not None and job.state.is_active

    def _request_stop_locked(self, job_id: str, reason: str) -> tuple[CancelToken, str] | None:
        """Record a stop request; returns (token, reason) to cancel outside the lock when needed."""
        run = self._running.get(job_id)
        if run is None:
            return None
        if run.stop_reason and _STOP_PRIORITY[reason] <= _STOP_PRIORITY[run.stop_reason]:
            return None
        run.stop_reason = reason
        return run.token, reason

    def _mark_straggler_locked(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        run = self._running.get(job_id)
        if job is None or run is None or not job.state.is_active:
            return
        # Persist the outcome the worker would have applied (startup recovery handles the rest).
        if run.stop_reason == REASON_CANCEL:
            if job.state in DOWNLOAD_PHASE_STATES:
                # Never let recovery auto-resume a download the user cancelled. Its files may
                # still be open, so they are not deleted now; ``archive_path`` is kept so that
                # removing the cancelled job later deletes them.
                job.state = JobState.CANCELLED
                job.status_text = "Cancelled"
                job.speed_bps = 0.0
                job.eta_seconds = None
                job.completed_at = utc_now_iso()
                self._meta[job_id].pause_reason = ""
                self._touch_state_locked(job)
            return
        if job.state in DOWNLOAD_PHASE_STATES:
            reason = PAUSED_BY_USER if run.stop_reason == REASON_PAUSE else PAUSED_BY_SHUTDOWN
            self._set_paused_locked(job, reason)

    def _add_job_locked(self, job: DownloadJob, meta: JobMeta) -> DownloadJob:
        self._jobs[job.id] = job
        self._meta[job.id] = meta
        snapshot = job.copy()
        self._queue_locked(rows=(encode_job(job, meta),), event=JobAdded(snapshot))
        self._notify_locked()
        return job.copy()

    def _remove_locked(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._meta.pop(job_id, None)
        self._loaded_ids.discard(job_id)
        self._queue_locked(deletes=(job_id,), event=JobRemoved(job_id))

    def _requeue_locked(self, job: DownloadJob, *, reset_attempts: bool = True) -> None:
        meta = self._meta[job.id]
        job.state = JobState.QUEUED
        job.status_text = ""
        job.retry_at = None
        job.error = ""
        job.error_kind = None
        job.error_url = ""
        job.completed_at = ""
        job.speed_bps = 0.0
        job.eta_seconds = None
        if reset_attempts:
            job.attempts = 0
        meta.pause_reason = ""
        self._touch_state_locked(job)
        self._notify_locked()

    def _set_paused_locked(self, job: DownloadJob, reason: str) -> None:
        job.state = JobState.PAUSED
        job.status_text = ""
        job.retry_at = None
        job.speed_bps = 0.0
        job.eta_seconds = None
        self._meta[job.id].pause_reason = reason
        self._touch_state_locked(job)
        self._notify_locked()

    def _set_cancelled_locked(self, job: DownloadJob, *, files_left: bool = False) -> None:
        """CANCELLED; ``files_left``: the job folder could not be deleted (e.g. a file locked
        by an antivirus scan) — ``archive_path`` is kept so remove/clear_finished retry it."""
        meta = self._meta[job.id]
        job.state = JobState.CANCELLED
        job.status_text = "Cancelled"
        job.retry_at = None
        job.speed_bps = 0.0
        job.eta_seconds = None
        job.phase_progress = 0.0
        job.completed_at = utc_now_iso()
        meta.pause_reason = ""
        meta.install_requested = False
        if not job.imported_archive:
            leftover = job.archive_path if files_left else ""
            self._reset_download_locked(job, touch=False)
            job.archive_path = leftover
        self._touch_state_locked(job)
        self._notify_locked()

    def _reset_download_locked(self, job: DownloadJob, *, touch: bool = True) -> None:
        """Forget all downloaded data (after it was deleted)."""
        meta = self._meta[job.id]
        job.bytes_done = 0
        job.archive_path = ""
        job.filename = ""
        job.resolved_url = ""
        job.etag = ""
        meta.archive_complete = False
        meta.forget_link()
        if touch:
            self._touch_state_locked(job)

    def _fail_locked(self, job: DownloadJob, error: AnkerError) -> None:
        meta = self._meta[job.id]
        decision = decide_failure(error, attempts=job.attempts, now=self._clock(), backoff=self._backoff)
        job.state = decision.state
        job.error = decision.error
        job.error_kind = decision.error_kind or ErrorKind.UNKNOWN
        job.error_url = decision.error_url
        job.retry_at = decision.retry_at
        job.attempts = decision.attempts
        job.status_text = decision.status_text
        job.speed_bps = 0.0
        job.eta_seconds = None
        if decision.state is JobState.FAILED:
            job.completed_at = utc_now_iso()
            phase = "Installation failed" if meta.archive_complete else "Download failed"
            self._touch_state_locked(job)
            notification = Notification(title=phase, message=f"{job.title}: {job.error}", level="error", tray=True)
            self._queue_locked(event=notification)
        else:
            self._touch_state_locked(job)
        self._notify_locked()

    def _archive_ready_locked(self, job: DownloadJob) -> bool:
        meta = self._meta[job.id]
        return bool(meta.archive_complete and job.archive_path and os.path.isfile(job.archive_path))

    def _should_discard_on_remove(self, job: DownloadJob) -> bool:
        # FAILED/CANCELLED leftovers are useless; a completed download is the user's archive.
        return not job.imported_archive and job.state is not JobState.COMPLETED and bool(job_work_dir(job))

    # --- outbox -------------------------------------------------------------------------
    def _touch_state_locked(self, job: DownloadJob, *, stamp: bool = True) -> None:
        """Queue an immediate write + ``JobUpdated`` for a state-level change."""
        meta = self._meta[job.id]
        now = self._monotonic()
        if stamp:
            job.updated_at = utc_now_iso()
        meta.last_event_at = now
        meta.last_persist_at = now
        meta.event_pending = False
        meta.persist_pending = False
        self._queue_locked(rows=(encode_job(job, meta),), event=JobUpdated(job.copy()))

    def _touch_progress_locked(self, job: DownloadJob) -> None:
        """Record a progress-only change: written ≤ every ``progress_persist_interval`` and
        published ≤ every ``progress_event_interval``; the scheduler flushes what is owed."""
        meta = self._meta[job.id]
        meta.event_pending = True
        meta.persist_pending = True
        self._flush_job_locked(job)

    def _flush_job_locked(self, job: DownloadJob) -> None:
        """Queue the owed progress write/event of ``job`` whose throttle window has passed."""
        meta = self._meta[job.id]
        now = self._monotonic()
        rows: tuple[JobRow, ...] = ()
        event: Event | None = None
        if meta.persist_pending and now - meta.last_persist_at >= self._persist_interval:
            job.updated_at = utc_now_iso()
            rows = (encode_job(job, meta),)
            meta.last_persist_at = now
            meta.persist_pending = False
        if meta.event_pending and now - meta.last_event_at >= self._event_interval:
            event = JobUpdated(job.copy())
            meta.last_event_at = now
            meta.event_pending = False
        if rows or event is not None:
            self._queue_locked(rows=rows, event=event)

    def _queue_locked(
        self, *, rows: tuple[JobRow, ...] = (), deletes: tuple[str, ...] = (), event: Event | None = None
    ) -> None:
        """Queue side effects of a mutation (in mutation order); applied by :meth:`_drain`."""
        if rows or deletes:
            self._db_queue.append(_DbOp(rows=rows, deletes=deletes))
            if self._db_writer_is_scheduler():
                self._notify_locked()
        if event is not None:
            self._outbox.append(event)

    def _db_writer_is_scheduler(self) -> bool:
        scheduler = self._scheduler
        return scheduler is not None and scheduler.is_alive() and not self._stopping

    def _drain(self) -> None:
        """Publish queued events in order, then persist (unless the scheduler does it).

        Never call with ``_lock`` held."""
        me = threading.get_ident()
        with self._out_lock:
            if self._draining_thread == me:
                return  # re-entered from an event subscriber; the outer loop delivers the rest
            self._draining_thread = me
            try:
                while True:
                    with self._lock:
                        if not self._outbox:
                            break
                        event = self._outbox.popleft()
                        closed = self._closed
                    if not closed:
                        self._events.publish(event)
            finally:
                self._draining_thread = None
        with self._lock:
            delegated = self._db_writer_is_scheduler()
        if not delegated:
            self._flush_db()

    def _flush_db(self) -> None:
        """Apply queued table writes in order (one writer at a time; consecutive upserts batched)."""
        with self._db_lock:
            with self._lock:
                ops = list(self._db_queue)
                self._db_queue.clear()
                if self._closed:
                    return
            pending: list[JobRow] = []
            for op in ops:
                if op.deletes:
                    self._store.upsert(pending)
                    pending = []
                    self._store.delete(op.deletes)
                pending.extend(op.rows)
            self._store.upsert(pending)

    # ====================================================================================
    # misc helpers
    # ====================================================================================
    def _snapshot(self, job_id: str) -> DownloadJob:
        with self._lock:
            return self._jobs[job_id].copy()

    def _archive_ready(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs[job_id]
            meta = self._meta[job_id]
            if self._archive_ready_locked(job):
                return True
            if job.imported_archive:
                raise InstallError(f"The archive {job.archive_path} no longer exists.")
            if meta.archive_complete:
                log.warning("Archive of job %s is missing; downloading it again", job_id)
                self._reset_download_locked(job)
            return False

    def _should_install(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs[job_id]
            meta = self._meta[job_id]
            if job.imported_archive or meta.install_requested:
                return True
        return self._settings.get().auto_install

    def _require_base_install(self, job: DownloadJob) -> None:
        if self._installed_game(job.slug) is None:
            raise InstallError(_base_missing_message(job))

    def _installed_game(self, slug: str) -> InstalledGame | None:
        if not slug:
            return None
        try:
            return self._library.find_by_slug(slug)
        except Exception:
            log.warning("Library lookup for %s failed", slug, exc_info=True)
            return None

    def _default_library_root(self, slug: str, kind: DownloadKind) -> str:
        if kind is not DownloadKind.FULL:
            installed = self._installed_game(slug)
            if installed is not None and installed.library_root:
                return installed.library_root
        return self._settings.get().default_library

    def _check_space(self, size: int | None, *, download_dir: str, library_root: str, already: int) -> None:
        if not size:
            return
        try:
            diskspace.ensure_space(
                size,
                download_dir=download_dir,
                library_root=library_root,
                already_downloaded=max(0, int(already)),
            )
        except AnkerError:
            raise
        except OSError as exc:
            raise AnkerError(
                f"Cannot check free space for {library_root}: {exc.strerror or exc}", detail=repr(exc)
            ) from exc

    @staticmethod
    def _partial_size(downloader: HttpDownloader, dest: str) -> int:
        try:
            return max(0, int(downloader.partial_size(dest)))
        except Exception:
            log.debug("partial_size failed for %s", dest, exc_info=True)
            return 0

    @staticmethod
    def _discard_partial(downloader: HttpDownloader, dest: str) -> None:
        try:
            downloader.discard_partial(dest)
        except Exception:
            log.warning("Could not discard partial download %s", dest, exc_info=True)

    @staticmethod
    def _file_size(path: str) -> int | None:
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    def _discard_files_then_release(self, job: DownloadJob) -> None:
        deleted = False
        try:
            deleted = discard_job_files(job)
        finally:
            with self._cond:
                self._cleaning.discard(job.id)
                live = self._jobs.get(job.id)
                if not deleted and live is not None and live.state is JobState.CANCELLED and not live.archive_path:
                    live.archive_path = job.archive_path  # remove()/clear_finished() try again
                    self._touch_state_locked(live)
                self._notify_locked()
            self._drain()

    def _apply_rate_limit(self, settings: Settings | None = None) -> None:
        current = settings or self._settings.get()
        try:
            self._rate_limiter.set_rate(current.speed_limit_bps)
        except Exception:
            log.exception("Could not apply the download speed limit")

    def _on_settings_changed(self, event: SettingsChanged) -> None:
        keys = event.keys
        if "speed_limit_kbps" in keys:
            self._apply_rate_limit()
        restarts: list[tuple[CancelToken, str]] = []
        with self._cond:
            if self._stopping:
                return
            if "connections_per_download" in keys:
                for job_id in list(self._running):
                    job = self._jobs.get(job_id)
                    if job is not None and job.state is JobState.DOWNLOADING:
                        request = self._request_stop_locked(job_id, REASON_RESTART)
                        if request is not None:
                            restarts.append(request)
            if keys & {"max_concurrent_downloads", "auto_install", "connections_per_download"}:
                self._notify_locked()
        for token, reason in restarts:
            token.cancel(reason)


# --- module helpers -----------------------------------------------------------------------


def _recover_loaded_job(job: DownloadJob, meta: JobMeta) -> bool:
    """Repair a job loaded from disk after a crash/exit; returns True when it changed."""
    changed = False
    if job.state in DOWNLOAD_PHASE_STATES:
        job.state = JobState.PAUSED
        job.status_text = ""
        meta.pause_reason = PAUSED_BY_SHUTDOWN
        changed = True
    elif job.state in (JobState.EXTRACTING, JobState.INSTALLING):
        job.state = JobState.FAILED
        job.error = "Installation was interrupted. Retry to install it again."
        job.error_kind = ErrorKind.INSTALL
        job.status_text = ""
        job.phase_progress = 0.0
        job.completed_at = utc_now_iso()
        changed = True
    if job.speed_bps or job.eta_seconds is not None:
        job.speed_bps = 0.0
        job.eta_seconds = None
        changed = True
    return changed


def _dedupe_rows(rows: list[JobRow]) -> list[JobRow]:
    latest: dict[str, JobRow] = {}
    for row in rows:
        latest[row.id] = row
    return list(latest.values())


def _base_missing_message(job: DownloadJob) -> str:
    what = "update" if job.option.kind is DownloadKind.PATCH else "add-on"
    return f"Install {job.title} first: this {what} is applied to an existing installation."
