"""Resumable, segmented HTTP file downloader.

Behaviour
* Writes to ``<dest>.part`` with a sidecar ``<dest>.part.json`` describing
  ``{"version": 1, "url": …, "size": …, "etag": …, "last_modified": …,
  "segments": [{"start": s, "end": e, "done": n}, …]}`` (``end`` inclusive;
  ``"size": null`` / ``"end": null`` while the size is unknown). The sidecar is
  rewritten atomically at most once per second and on stop.
* Resume: if a sidecar exists, the new link's size matches and its validators
  agree with the saved ones (a strong ETag when both sides have one; otherwise
  also Last-Modified when both sides have it) continue each segment from
  ``start + done`` using ``Range`` + ``If-Range``; otherwise discard and
  restart. ``If-Range`` carries the *saved* validators (strong ETag, else
  Last-Modified), because they describe the bytes already on disk. A sidecar
  marking every byte done (an earlier final rename failed) is finished without
  any request, even when the server does not support ranges. A 200 reply to a ranged request (server ignored the
  range) restarts that file from 0 with a single connection. A reply showing
  a different file (Content-Range total, Content-Length or ETag differ, or
  416) restarts from 0 with the new metadata. At most ``max_restarts``
  restarts per call, then ``DownloadError``.
* Segmentation: when ``link.accept_ranges`` and size ≥ 2×``min_segment_size``,
  split into up to ``connections`` segments; finished connections steal work by
  splitting the largest remaining segment (dynamic segmentation) so the tail
  of a download keeps all connections busy (a split happens only while at
  least 2×``min_steal_size`` remains). Without range support: one
  connection, no resume.
* Pre-allocate the ``.part`` file to the full size when known (sparse on
  NTFS, so later segments never wait for zero-filling). A fresh download
  first checks the volume's free space (``DiskSpaceError``).
* Every read loop: ``token.raise_if_cancelled()`` → ``rate_limiter.acquire(n)``
  → write at the segment offset (one shared handle, lock around seek+write).
  Requests ask for ``Accept-Encoding: identity`` so byte offsets are exact.
* Transient failures (connection reset, timeout, 5xx, 429, short read) on a
  segment are retried up to 5 times with backoff (1, 2, 4, 8, 16 s, cancellable,
  at least ``Retry-After``); the count resets whenever the connection made
  progress. While a connection backs off, idle connections may take over its
  segment. HTTP 401/403/404/410 (or an HTML page instead of the file) →
  ``LinkExpiredError`` (caller re-resolves and calls ``download`` again with
  the new link — partial data is kept).
* Progress callback (on the calling thread) at most every 250 ms with
  ``DownloadProgress``, plus one final report on success; speed is an
  exponential moving average over ~5 s; ETA from remaining/speed.
  ``bytes_done`` only decreases when the file restarts from 0.
* On success: verify the byte count equals ``size`` (when known), fsync,
  rename ``.part`` → ``dest`` (replace), delete the sidecar, return ``dest``.
* On cancellation: stop all connections promptly (blocked socket reads are
  aborted), flush the sidecar, KEEP the partial files, raise
  ``OperationCancelled``. The caller decides whether to delete
  (``discard_partial``) — pausing keeps them.
* ``OSError`` writing to disk (e.g. ENOSPC) → ``DownloadError`` with a clear message
  (``DiskSpaceError`` when free space is the cause).

One ``HttpDownloader`` may run several downloads concurrently (all state is
per call); two concurrent calls for the same ``dest_path`` are not supported.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from anker_client.core.errors import DiskSpaceError, DownloadError, IntegrityError
from anker_client.core.formatting import format_bytes
from anker_client.core.models import ResolvedLink
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads import _engine_sidecar as sidecars
from anker_client.services.downloads._engine_http import (
    RestartRequired,
    is_weak_etag,
    redact_url,
    same_etag,
    validator_conflict,
)
from anker_client.services.downloads._engine_partfile import PartFile, disk_error, free_bytes, volume_label
from anker_client.services.downloads._engine_progress import SpeedMeter
from anker_client.services.downloads._engine_segments import Segment, split_evenly
from anker_client.services.downloads._engine_transfer import Transfer, TransferConfig, TransferPlan
from anker_client.services.downloads.ratelimit import RateLimiter
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
_MAX_CONNECTIONS = 32


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    bytes_done: int
    bytes_total: int | None
    speed_bps: float
    eta_seconds: float | None
    connections: int = 1


class _ProgressReporter:
    """Throttles reports to one per ``interval`` and adds speed/ETA."""

    def __init__(self, callback: Callable[[DownloadProgress], None] | None, interval: float) -> None:
        self._callback = callback
        self._interval = interval
        self._meter = SpeedMeter()
        self._last_emit: float | None = None
        self._failed = False

    def __call__(self, done: int, total: int | None, connections: int, *, final: bool = False) -> None:
        if self._callback is None:
            return
        now = time.perf_counter()
        if not final and self._last_emit is not None and now - self._last_emit < self._interval:
            return
        speed = self._meter.update(done, now)
        remaining = None if total is None else max(0, total - done)
        progress = DownloadProgress(
            bytes_done=done,
            bytes_total=total,
            speed_bps=speed,
            eta_seconds=0.0 if final else SpeedMeter.eta(remaining, speed),
            connections=connections,
        )
        self._last_emit = now
        try:
            self._callback(progress)
        except Exception:
            if not self._failed:
                log.exception("Download progress callback failed (further failures are not logged)")
            self._failed = True


class HttpDownloader:
    def __init__(
        self,
        http: HttpClient,
        rate_limiter: RateLimiter,
        *,
        connections: int = 4,
        min_segment_size: int = 16 * 1024 * 1024,
        chunk_size: int = 256 * 1024,
        min_steal_size: int | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
        timeout: tuple[float, float] = (10.0, 30.0),
        progress_interval: float = 0.25,
        sidecar_interval: float = 1.0,
        max_restarts: int = 3,
        stop_timeout: float = 3.0,
    ) -> None:
        self._http = http
        self._limiter = rate_limiter
        self._connections = max(1, min(int(connections), _MAX_CONNECTIONS))
        self._min_segment = max(1, int(min_segment_size))
        self._max_restarts = max(0, int(max_restarts))
        chunk = max(1024, int(chunk_size))
        steal_min = min_steal_size if min_steal_size is not None else max(4 * chunk, self._min_segment // 4)
        self._config = TransferConfig(
            chunk_size=chunk,
            steal_min=max(1, int(steal_min)),
            retry_delays=tuple(max(0.0, float(d)) for d in retry_delays),
            timeout=timeout,
            progress_interval=max(0.0, progress_interval),
            sidecar_interval=max(0.0, sidecar_interval),
            stop_timeout=max(0.0, stop_timeout),
        )

    # --- public API ---------------------------------------------------------------------

    def download(
        self,
        link: ResolvedLink,
        dest_path: str,
        *,
        token: CancelToken,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> str:
        """Download (or resume) ``link`` to ``dest_path``; returns ``dest_path``."""
        token.raise_if_cancelled()
        if not os.fspath(dest_path):
            raise DownloadError("No download location was given.", detail="empty dest_path")
        dest = os.path.abspath(os.fspath(dest_path))
        part, sidecar = sidecars.part_path(dest), sidecars.sidecar_path(dest)
        name = os.path.basename(dest)
        _prepare_directory(dest)
        report = _ProgressReporter(on_progress, self._config.progress_interval)

        plan = self._resume_plan(link, part, sidecar) or self._fresh_plan(
            url=link.url,
            size=link.size,
            etag=link.etag,
            last_modified=link.last_modified,
            content_type=link.content_type,
            accept_ranges=link.accept_ranges,
        )
        log.info(
            "Downloading %s from %s: %s, %d connection(s)%s",
            name, redact_url(link.url), format_bytes(plan.size, unknown="unknown size"), plan.connections,
            "" if plan.fresh else f", resuming at {format_bytes(sum(s.done for s in plan.segments))}",
        )
        started = time.perf_counter()
        finished = self._run_until_done(plan, part, sidecar, token=token, report=report, name=name)
        final_size = finished.bytes_done
        _commit(part, sidecar, dest, finished)
        log.info("Downloaded %s (%s in %.1fs)", name, format_bytes(final_size), time.perf_counter() - started)
        report(final_size, final_size, 0, final=True)
        return dest_path

    def _run_until_done(
        self, plan: TransferPlan, part: str, sidecar: str, *, token: CancelToken, report: _ProgressReporter, name: str
    ) -> sidecars.Sidecar:
        """Run transfers (restarting from 0 when required) until ``part`` is complete.

        Returns the download state of the finished file (every byte done).
        """
        restarts = 0
        while True:
            part_file = self._open_part(plan, part, sidecar)
            if not part_file.sparse and plan.connections > 1:
                log.info("%s: no sparse-file support on this volume; downloading sequentially", name)
                plan = _sequential(plan)
            try:
                transfer = Transfer(
                    http=self._http,
                    limiter=self._limiter,
                    plan=plan,
                    part=part_file,
                    sidecar_path=sidecar,
                    token=token,
                    config=self._config,
                    report=report,
                    name=name,
                )
                try:
                    transfer.run()
                except RestartRequired as restart:
                    restarts += 1
                    if restarts > self._max_restarts:
                        raise DownloadError(
                            "The file kept changing on the server. Try again later.", detail=restart.reason
                        ) from restart
                    plan = self._restart_plan(plan, restart)
                    log.warning("%s: %s; starting over (%d connection(s))", name, restart.reason, plan.connections)
                    continue
                final_size = transfer.size if transfer.size is not None else transfer.bytes_done
                self._finish_part(part_file, final_size, transfer.bytes_done)
                return sidecars.Sidecar(
                    url=plan.url,
                    size=final_size,
                    etag=transfer.etag,
                    last_modified=transfer.last_modified,
                    segments=[(0, final_size - 1, final_size)] if final_size else [],
                )
            finally:
                part_file.close()

    @staticmethod
    def partial_size(dest_path: str) -> int:
        """Bytes already downloaded for ``dest_path`` according to its sidecar (0 if none)."""
        if not os.fspath(dest_path):
            return 0
        dest = os.path.abspath(os.fspath(dest_path))
        if not os.path.isfile(sidecars.part_path(dest)):
            return 0
        state = sidecars.load(sidecars.sidecar_path(dest))
        return state.bytes_done if state is not None else 0

    @staticmethod
    def discard_partial(dest_path: str) -> None:
        """Delete ``<dest>.part`` and ``<dest>.part.json`` if present."""
        if not os.fspath(dest_path):
            return  # abspath("") is the working directory: never touch files next to it
        dest = os.path.abspath(os.fspath(dest_path))
        sidecar = sidecars.sidecar_path(dest)
        for path in (sidecars.part_path(dest), sidecar, sidecars.temp_path(sidecar)):
            sidecars.remove_quietly(path)

    # --- planning -----------------------------------------------------------------------

    def _connections_for(self, remaining: int, pending_segments: int = 1) -> int:
        if remaining < 2 * self._min_segment:
            return max(1, min(self._connections, pending_segments))
        return max(1, min(self._connections, max(pending_segments, remaining // self._min_segment)))

    def _fresh_plan(
        self,
        *,
        url: str,
        size: int | None,
        etag: str,
        last_modified: str,
        content_type: str,
        accept_ranges: bool,
    ) -> TransferPlan:
        if size is not None and accept_ranges:
            connections = self._connections_for(size)
            segments = split_evenly(size, connections)
            ranged = True
        else:
            connections = 1
            ranged = False
            segments = [Segment(0, None if size is None else size - 1)] if size != 0 else []
        return TransferPlan(
            url=url,
            size=size,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            ranged=ranged,
            connections=connections,
            segments=segments,
            fresh=True,
        )

    def _resume_plan(self, link: ResolvedLink, part: str, sidecar: str) -> TransferPlan | None:
        state = sidecars.load(sidecar)
        if state is None:
            if os.path.exists(part):
                log.info("Discarding %s: no usable download state", os.path.basename(part))
            return None
        reason = _resume_blocker(state, link, part)
        if reason:
            log.info("Not resuming %s: %s", os.path.basename(part), reason)
            return None
        segments = [Segment(start, end, done) for start, end, done in state.segments]
        pending = [s for s in segments if not s.complete]
        remaining = sum((s.length or 0) - s.done for s in pending)
        etag, last_modified = _resume_validators(state, link)
        return TransferPlan(
            url=link.url,
            size=state.size,
            etag=etag,
            last_modified=last_modified,
            content_type=link.content_type,
            ranged=True,
            connections=self._connections_for(remaining, len(pending)),
            segments=segments,
            fresh=False,
        )

    def _restart_plan(self, old: TransferPlan, restart: RestartRequired) -> TransferPlan:
        remote = restart.remote
        if restart.single_connection:
            size = remote.size if remote is not None and remote.size is not None else old.size
            return self._fresh_plan(
                url=old.url,
                size=size,
                etag=(remote.etag if remote is not None else "") or old.etag,
                last_modified=(remote.last_modified if remote is not None else "") or old.last_modified,
                content_type=old.content_type,
                accept_ranges=False,
            )
        if remote is None:
            return self._fresh_plan(
                url=old.url, size=None, etag="", last_modified="", content_type=old.content_type, accept_ranges=False
            )
        return self._fresh_plan(
            url=old.url,
            size=remote.size,
            etag=remote.etag,
            last_modified=remote.last_modified,
            content_type=old.content_type,
            accept_ranges=remote.accept_ranges and remote.size is not None,
        )

    # --- files --------------------------------------------------------------------------

    def _open_part(self, plan: TransferPlan, part: str, sidecar: str) -> PartFile:
        if not plan.fresh:
            try:
                return PartFile.open_existing(part)
            except OSError as exc:
                raise disk_error(exc, part, required=None, action="open the partial download on") from exc
        # A fresh start: drop old state first so its space counts as free.
        for path in (sidecar, part):
            if not sidecars.remove_quietly(path):
                raise DownloadError(
                    "Could not replace the previous partial download (the file is in use).", detail=path
                )
        _check_free_space(part, plan.size)
        try:
            return PartFile.create(part, plan.size)
        except OSError as exc:
            raise disk_error(exc, part, required=plan.size, action="create the download file on") from exc

    @staticmethod
    def _finish_part(part_file: PartFile, size: int, done: int) -> None:
        if done != size:
            raise IntegrityError(detail=f"{part_file.path}: {done} bytes downloaded, expected {size}")
        try:
            part_file.finish(size)
        except OSError as exc:
            raise disk_error(exc, part_file.path, required=None, action="save the download to") from exc


def _sequential(plan: TransferPlan) -> TransferPlan:
    """One connection writing front to back (no zero-fill stalls on non-sparse volumes)."""
    segments = plan.segments
    if plan.fresh and plan.size:
        segments = [Segment(0, plan.size - 1)]
    return dataclasses.replace(plan, connections=1, segments=segments)


def _resume_blocker(state: sidecars.Sidecar, link: ResolvedLink, part: str) -> str:
    """Why the saved state cannot be continued with ``link`` ("" when it can)."""
    if not link.accept_ranges and not state.complete:  # a complete file only needs its rename
        return "the server does not support resuming"
    if link.size is None or state.size is None:
        return "the file size is unknown"
    if link.size != state.size:
        return f"the file size changed ({state.size} -> {link.size} bytes)"
    conflict = validator_conflict(state.etag, state.last_modified, link.etag, link.last_modified)
    if conflict:
        return f"the file changed on the server ({conflict})"
    try:
        if os.path.getsize(part) != state.size:
            return "the partial file is missing or truncated"
    except OSError:
        return "the partial file is missing"
    return ""


def _resume_validators(state: sidecars.Sidecar, link: ResolvedLink) -> tuple[str, str]:
    """``(etag, last_modified)`` to send in ``If-Range`` when continuing ``state``.

    They must describe the bytes already on disk, so the saved values win: a
    validator taken only from the new link would make the server confirm the
    *new* file and splice it onto the old bytes. The link's ETag is used only
    when it is strong and the sidecar has none or the same one (checked by
    ``_resume_blocker``), as a strong ETag is the better ``If-Range`` validator.
    """
    etag = state.etag or link.etag
    if link.etag and not is_weak_etag(link.etag) and (not state.etag or same_etag(state.etag, link.etag)):
        etag = link.etag
    return etag, state.last_modified or link.last_modified


def _prepare_directory(dest: str) -> None:
    if os.path.isdir(dest):
        raise DownloadError("The download target is a folder.", detail=dest)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as exc:
        raise disk_error(exc, dest, required=None, action="create the download folder on") from exc


def _check_free_space(part: str, size: int | None) -> None:
    if not size:
        return
    available = free_bytes(os.path.dirname(part))
    if available is not None and available < size:
        raise DiskSpaceError(required=size, available=available, path=volume_label(part))


def _commit(part: str, sidecar: str, dest: str, finished: sidecars.Sidecar) -> None:
    """Move the finished ``.part`` into place and drop the download state."""
    try:
        sidecars.replace_with_retry(part, dest, attempts=8)
    except OSError as exc:
        # Record that every byte is there, so a retry only has to repeat the rename.
        try:
            sidecars.save(sidecar, finished)
        except OSError as save_exc:
            log.warning("Could not save download state %s: %s", sidecar, save_exc)
        raise DownloadError(
            "Could not finish saving the download (the file may be in use).", detail=f"{exc!r} ({dest})"
        ) from exc
    for path in (sidecar, sidecars.temp_path(sidecar)):
        sidecars.remove_quietly(path)

