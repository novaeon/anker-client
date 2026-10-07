"""Private bookkeeping for :mod:`anker_client.services.downloads.manager`.

* :class:`JobMeta` — per-job data the manager needs beyond ``DownloadJob``.
  The persistent part is stored next to the job in the ``jobs.json`` column
  under the :data:`EXTRA_KEY` key (``DownloadJob.from_dict`` ignores unknown
  keys, so the public model stays unchanged); the runtime part (throttle
  timestamps, pending flags) is never persisted.
* :class:`JobStore` — the ``jobs`` table (load, upsert, delete).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from anker_client.core.db import Database
from anker_client.core.models import DownloadJob

log = logging.getLogger(__name__)

#: Key of the manager's private fields inside the persisted job JSON.
EXTRA_KEY = "_manager"

#: ``JobMeta.pause_reason`` values.
PAUSED_BY_USER = "user"
PAUSED_BY_SHUTDOWN = "shutdown"


@dataclass(slots=True)
class JobMeta:
    # --- persisted ---------------------------------------------------------------------
    pause_reason: str = ""  # PAUSED_BY_USER | PAUSED_BY_SHUTDOWN | "" (only meaningful while PAUSED)
    archive_complete: bool = False  # the archive at job.archive_path is fully downloaded/imported
    install_requested: bool = False  # install even when settings.auto_install is off
    link: dict[str, Any] | None = None  # last ResolvedLink (reused on resume while fresh)
    link_resolved_at: float = 0.0  # epoch seconds of ``link``
    # --- runtime only --------------------------------------------------------------------
    remove_when_stopped: bool = False  # remove() was called while the pipeline was running
    last_event_at: float = float("-inf")  # monotonic time of the last JobUpdated
    last_persist_at: float = float("-inf")  # monotonic time of the last DB write
    event_pending: bool = False  # a throttled progress event is owed
    persist_pending: bool = False  # throttled progress has not been written yet

    def persisted(self) -> dict[str, Any]:
        return {
            "pause_reason": self.pause_reason,
            "archive_complete": self.archive_complete,
            "install_requested": self.install_requested,
            "link": self.link,
            "link_resolved_at": self.link_resolved_at,
        }

    @classmethod
    def from_persisted(cls, data: Any) -> JobMeta:
        meta = cls()
        if not isinstance(data, dict):
            return meta
        meta.pause_reason = str(data.get("pause_reason") or "")
        meta.archive_complete = bool(data.get("archive_complete"))
        meta.install_requested = bool(data.get("install_requested"))
        link = data.get("link")
        meta.link = dict(link) if isinstance(link, dict) else None
        try:
            meta.link_resolved_at = float(data.get("link_resolved_at") or 0.0)
        except (TypeError, ValueError):
            meta.link_resolved_at = 0.0
        return meta

    def forget_link(self) -> None:
        self.link = None
        self.link_resolved_at = 0.0


@dataclass(frozen=True, slots=True)
class JobRow:
    """One serialised ``jobs`` row, captured under the manager lock."""

    id: str
    state: str
    position: int
    json: str
    created_at: str
    updated_at: str


def encode_job(job: DownloadJob, meta: JobMeta) -> JobRow:
    data = job.to_dict()
    data[EXTRA_KEY] = meta.persisted()
    return JobRow(
        id=job.id,
        state=str(job.state.value),
        position=int(job.position),
        json=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def decode_job(text: str) -> tuple[DownloadJob, JobMeta]:
    """Parse a stored row; raises ``ValueError`` when it is not a usable job."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("job JSON is not an object")
    meta = JobMeta.from_persisted(data.pop(EXTRA_KEY, None))
    try:
        job = DownloadJob.from_dict(data)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError(f"invalid job: {exc}") from exc
    if not job.id or (not job.slug and not job.imported_archive):
        raise ValueError("job without id/slug")
    _coerce_numbers(job)
    return job, meta


def _coerce_numbers(job: DownloadJob) -> None:
    """Repair numeric fields of a stored job (a string ``position`` or ``retry_at`` would
    break every sort/comparison of the queue); raises ``ValueError`` when unusable."""
    try:
        job.position = int(job.position)
        job.attempts = max(0, int(job.attempts or 0))
        job.bytes_done = max(0, int(job.bytes_done or 0))
        job.bytes_total = None if job.bytes_total is None else int(job.bytes_total)
        job.retry_at = None if job.retry_at is None else float(job.retry_at)
        job.phase_progress = float(job.phase_progress or 0.0)
        job.speed_bps = float(job.speed_bps or 0.0)
        job.eta_seconds = None if job.eta_seconds is None else float(job.eta_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field: {exc}") from exc
    if not isinstance(job.genres, list):
        job.genres = []


class JobStore:
    """The ``jobs`` table. Every method swallows (and logs) SQLite errors: losing a
    progress write must never break a running download."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def load(self) -> list[tuple[DownloadJob, JobMeta]]:
        try:
            rows = self._db.query("SELECT id, json FROM jobs ORDER BY position, created_at")
        except sqlite3.Error:
            log.exception("Could not read the download queue")
            return []
        result: list[tuple[DownloadJob, JobMeta]] = []
        for row in rows:
            try:
                job, meta = decode_job(row["json"])
            except ValueError as exc:
                log.warning("Ignoring unreadable download job %s: %s", row["id"], exc)
                continue
            result.append((job, meta))
        return result

    def upsert(self, rows: Sequence[JobRow]) -> None:
        if not rows:
            return
        params = [(r.id, r.state, r.position, r.json, r.created_at, r.updated_at) for r in rows]
        sql = (
            "INSERT INTO jobs(id, state, position, json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, position = excluded.position, "
            "json = excluded.json, updated_at = excluded.updated_at"
        )
        try:
            if len(params) == 1:
                self._db.execute(sql, params[0])
            else:
                self._db.executemany(sql, params)
        except sqlite3.Error:
            log.exception("Could not save %d download job(s)", len(params))

    def delete(self, job_ids: Iterable[str]) -> None:
        ids = [(job_id,) for job_id in job_ids]
        if not ids:
            return
        try:
            self._db.executemany("DELETE FROM jobs WHERE id = ?", ids)
        except sqlite3.Error:
            log.exception("Could not delete %d download job(s)", len(ids))
