"""One attempt at moving the bytes of a download plan: connection threads + coordinator.

``Transfer.run()`` blocks the calling thread, which acts as the coordinator:
it reports progress, persists the sidecar about once per second and waits for
the connection threads. It returns when every segment is on disk, or raises:

* ``OperationCancelled`` — the caller's token fired (sidecar flushed, data kept);
  connections get a short grace period to stop, then blocked sockets are closed;
* ``RestartRequired`` — the plan is unusable (server ignored ``Range``, file
  changed); the engine starts over from byte 0;
* an ``AnkerError`` — ``LinkExpiredError``, ``DiskSpaceError``,
  ``DownloadError``, ``RateLimitedError`` (sidecar flushed, data kept).

Each connection thread loops: claim a segment (or steal half of the busiest
one) → ranged GET → read/limit/write until the segment is done → repeat.
Transient failures release the segment (so idle connections may pick it up)
and back off before the next attempt; consecutive failures without progress
are capped by ``retry_delays``.
"""

from __future__ import annotations

import http.client
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests
import urllib3.exceptions

from anker_client.core.errors import (
    AnkerError,
    DownloadError,
    LinkExpiredError,
    NetworkError,
    NotFoundError,
    OperationCancelled,
    RateLimitedError,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads._engine_http import (
    EXPIRED_STATUSES,
    RemoteInfo,
    RestartRequired,
    TransientError,
    abort_response,
    close_quietly,
    if_range_value,
    is_encoded,
    parse_content_range,
    redact_url,
    retry_after_seconds,
    same_etag,
)
from anker_client.services.downloads._engine_partfile import PartFile, PartFileClosed, disk_error
from anker_client.services.downloads._engine_segments import Segment, SegmentTable
from anker_client.services.downloads._engine_sidecar import Sidecar
from anker_client.services.downloads._engine_sidecar import save as save_sidecar
from anker_client.services.downloads.ratelimit import RateLimiter

log = logging.getLogger(__name__)

#: ``report(bytes_done, bytes_total, active_connections)``
ReportFn = Callable[[int, int | None, int], None]

# Connections normally notice a stop within milliseconds (between chunks, or woken
# in the rate limiter / backoff). Only reads blocked on a silent server need their
# socket closed underneath them, which is a last resort (see abort_response).
_ABORT_GRACE_SECONDS = 0.25
_NETWORK_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException, OSError)


class HttpGetter(Protocol):
    """The slice of ``site.http.HttpClient`` the engine uses."""

    def get(
        self,
        url: str,
        *,
        headers: Any = None,
        timeout: Any = None,
        stream: bool = False,
        allow_redirects: bool = True,
        retry: bool = True,
        raise_for_status: bool = True,
        token: CancelToken | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class TransferPlan:
    url: str
    size: int | None
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    ranged: bool = False  # Range requests: resumable, may use several connections
    connections: int = 1
    segments: list[Segment] = field(default_factory=list)
    fresh: bool = True  # False when continuing from a sidecar


@dataclass(frozen=True, slots=True)
class TransferConfig:
    chunk_size: int
    steal_min: int
    retry_delays: tuple[float, ...]
    timeout: tuple[float, float]
    progress_interval: float
    sidecar_interval: float
    stop_timeout: float


class _ConnectionStats:
    __slots__ = ("received",)

    def __init__(self) -> None:
        self.received = 0


class Transfer:
    def __init__(
        self,
        *,
        http: HttpGetter,
        limiter: RateLimiter,
        plan: TransferPlan,
        part: PartFile,
        sidecar_path: str,
        token: CancelToken,
        config: TransferConfig,
        report: ReportFn,
        name: str = "download",
    ) -> None:
        self._http = http
        self._limiter = limiter
        self._plan = plan
        self._part = part
        self._sidecar_path = sidecar_path
        self._token = token
        self._config = config
        self._report = report
        self._name = name
        self._table = SegmentTable(
            plan.segments, steal_min=config.steal_min, allow_split=plan.ranged and plan.connections > 1
        )
        # Private token: fired by the caller's token, by a fatal error, or at the end of run().
        self._run_token = CancelToken()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._responses: set[Any] = set()
        self._alive = 0
        self._error: BaseException | None = None
        self._save_failed = False  # coordinator thread only
        # validators, possibly learned from responses (guarded by _lock)
        self._size = plan.size
        self._etag = plan.etag
        self._last_modified = plan.last_modified

    # --- results ------------------------------------------------------------------------

    @property
    def size(self) -> int | None:
        with self._lock:
            return self._size

    @property
    def etag(self) -> str:
        with self._lock:
            return self._etag

    @property
    def last_modified(self) -> str:
        with self._lock:
            return self._last_modified

    @property
    def bytes_done(self) -> int:
        return self._table.bytes_done()

    @property
    def steals(self) -> int:
        return self._table.steals

    # --- coordinator --------------------------------------------------------------------

    def run(self) -> None:
        threads = [
            threading.Thread(target=self._worker, args=(index,), name=f"anker-dl-{self._name}-{index}", daemon=True)
            for index in range(max(1, self._plan.connections))
        ]
        unregister = self._token.on_cancel(self._stop)
        try:
            with self._lock:
                self._alive = len(threads)
            for thread in threads:
                thread.start()
            self._supervise()
        finally:
            self._stop()
            self._join(threads)
            unregister()
        self._conclude()

    def _supervise(self) -> None:
        clock = time.perf_counter  # high resolution on every Python version (monotonic is 15.6 ms on 3.12/Windows)
        next_report = next_save = clock()
        while True:
            with self._lock:
                alive = self._alive
            if alive == 0 or self._run_token.cancelled:
                return
            now = clock()
            if now >= next_report:
                self._report_progress()
                # scheduled from *after* the report, so the reporter's own throttle never skips a tick
                next_report = clock() + self._config.progress_interval
            if now >= next_save:
                self._persist()
                next_save = clock() + self._config.sidecar_interval
            self._wake.wait(max(0.0, min(next_report, next_save) - clock()))
            self._wake.clear()

    def _conclude(self) -> None:
        error = self._error
        if error is None and self._table.all_complete():
            return
        if self._token.cancelled:
            self._persist()
            raise OperationCancelled()
        if isinstance(error, RestartRequired):
            raise error
        self._persist()
        if error is not None:
            raise error
        raise DownloadError("The download stopped unexpectedly.", detail="connections exited with unfinished segments")

    def _report_progress(self) -> None:
        with self._lock:
            connections = len(self._responses)
            size = self._size
        self._report(self._table.bytes_done(), size, connections)

    def _persist(self) -> None:
        # Lock order everywhere: transfer lock, then table lock.
        with self._lock:
            sidecar = Sidecar(
                url=self._plan.url,
                size=self._size,
                etag=self._etag,
                last_modified=self._last_modified,
                segments=self._table.snapshot(),
            )
        try:
            save_sidecar(self._sidecar_path, sidecar)
        except OSError as exc:
            # Saved every second: warn once per transfer instead of flooding the rotating log.
            level = logging.DEBUG if self._save_failed else logging.WARNING
            self._save_failed = True
            log.log(level, "Could not save download state %s: %s", self._sidecar_path, exc)
        else:
            self._save_failed = False

    def _stop(self) -> None:
        self._run_token.cancel(self._token.reason or "stop")
        self._wake.set()

    def _fail(self, exc: BaseException) -> None:
        if isinstance(exc, RestartRequired):
            log.info("Restarting %s: %s", self._name, exc.reason)
        elif not isinstance(exc, AnkerError):
            log.error("Download connection crashed", exc_info=exc)
            wrapped = DownloadError(detail=repr(exc))
            wrapped.__cause__ = exc
            exc = wrapped
        with self._lock:
            if self._error is None:
                self._error = exc
        self._stop()

    def _join(self, threads: list[threading.Thread]) -> None:
        """Wait for the connections; abort the sockets of any still blocked after a short grace period."""
        started = time.perf_counter()
        deadline = started + self._config.stop_timeout
        grace_end = started + min(_ABORT_GRACE_SECONDS, self._config.stop_timeout)
        for thread in threads:
            if thread.ident is not None:
                thread.join(max(0.0, grace_end - time.perf_counter()))
        if any(t.is_alive() for t in threads):
            self._abort_responses()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(max(0.0, deadline - time.perf_counter()))
        stragglers = [t.name for t in threads if t.is_alive()]
        if stragglers:
            # They cannot write any more (the part file refuses writes once closed).
            log.warning("Connections still busy after stop (left to time out): %s", ", ".join(stragglers))

    def _abort_responses(self) -> None:
        with self._lock:
            responses = list(self._responses)
        for resp in responses:
            abort_response(resp)

    # --- connection threads -------------------------------------------------------------

    def _worker(self, worker_id: int) -> None:
        try:
            self._work(worker_id)
        except OperationCancelled:
            pass
        except Exception as exc:  # surfaced through the coordinator
            self._fail(exc)
        finally:
            with self._lock:
                self._alive -= 1
            self._wake.set()

    def _work(self, worker_id: int) -> None:
        delays = self._config.retry_delays
        stats = _ConnectionStats()
        failures = 0
        mark = 0
        while not self._run_token.cancelled:
            seg = self._table.claim(worker_id)
            if seg is None:
                return
            try:
                self._download_segment(seg, stats)
            except TransientError as exc:
                self._table.release(seg)
                if self._run_token.cancelled:
                    return
                if not self._plan.ranged:
                    self._table.reset(seg)  # without Range support nothing survives a failure
                elif stats.received > mark:
                    failures = 0  # the connection made progress since its last failure
                mark = stats.received
                failures += 1
                if failures > len(delays):
                    raise _give_up(exc) from exc
                delay = max(delays[failures - 1], exc.retry_after)
                log.warning(
                    "%s: connection %d failed (%s); retry %d/%d in %.1fs",
                    self._name, worker_id, exc, failures, len(delays), delay,
                )
                if self._run_token.wait(delay):
                    return
                continue
            except BaseException:
                self._table.release(seg)
                raise
            self._table.release(seg)

    def _download_segment(self, seg: Segment, stats: _ConnectionStats) -> None:
        """Issue requests until ``seg`` is complete (several if the server shortens ranges)."""
        while True:
            pos, end = self._table.next_request(seg)
            if end is not None and pos > end:
                return
            if not self._plan.ranged and pos != seg.start:
                self._table.reset(seg)  # no Range support: every request starts at byte 0
                pos = seg.start
            resp = self._open(self._request_headers(pos, end))
            try:
                expected = self._accept(resp, seg, pos, end)
                if self._pump(resp, seg, pos, expected, stats):
                    return
            finally:
                with self._lock:
                    self._responses.discard(resp)
                close_quietly(resp)

    def _request_headers(self, pos: int, end: int | None) -> dict[str, str]:
        headers = {"Accept-Encoding": "identity"}
        if self._plan.ranged:
            headers["Range"] = f"bytes={pos}-{'' if end is None else end}"
            with self._lock:
                validator = if_range_value(self._etag, self._last_modified)
            if validator:
                headers["If-Range"] = validator
        return headers

    def _open(self, headers: dict[str, str]) -> Any:
        self._run_token.raise_if_cancelled()
        url = self._plan.url
        try:
            resp = self._http.get(
                url,
                headers=headers,
                stream=True,
                timeout=self._config.timeout,
                allow_redirects=True,
                retry=False,
                raise_for_status=False,
                token=self._run_token,
            )
        except OperationCancelled:
            raise
        except RateLimitedError as exc:
            raise TransientError(exc.message, retry_after=float(exc.retry_after), status=429) from exc
        except NotFoundError as exc:
            raise LinkExpiredError(detail=f"{exc.message} ({redact_url(url)})") from exc
        except NetworkError as exc:
            if exc.status in EXPIRED_STATUSES:
                raise LinkExpiredError(detail=f"HTTP {exc.status} ({redact_url(url)})") from exc
            raise TransientError(exc.message, status=exc.status) from exc
        except AnkerError:
            raise
        except _NETWORK_ERRORS as exc:
            if self._run_token.cancelled:
                raise OperationCancelled() from exc
            raise TransientError(f"request failed: {exc}") from exc
        with self._lock:
            if not self._run_token.cancelled:
                self._responses.add(resp)
                return resp
        close_quietly(resp)
        raise OperationCancelled()

    def _accept(self, resp: Any, seg: Segment, pos: int, end: int | None) -> int | None:
        """Validate a response for a request starting at ``pos``; returns the body bytes it should carry."""
        status = resp.status_code
        if status in EXPIRED_STATUSES:
            raise LinkExpiredError(detail=f"HTTP {status} ({redact_url(self._plan.url)})")
        if status == 429 or status >= 500:
            raise TransientError(f"HTTP {status}", retry_after=retry_after_seconds(resp), status=status)
        if status == 416:
            raise RestartRequired("the server rejected the byte range", remote=RemoteInfo.from_response(resp))
        if status not in (200, 206):
            raise DownloadError(
                f"The download server answered with an unexpected status (HTTP {status}).",
                detail=redact_url(self._plan.url),
            )
        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type.startswith("text/html") and not self._plan.content_type.lower().startswith("text/html"):
            raise LinkExpiredError(detail=f"got an HTML page instead of the file ({redact_url(self._plan.url)})")
        remote = RemoteInfo.from_response(resp)
        if self._plan.ranged:
            return self._accept_ranged(resp, remote, pos, end)
        return self._accept_stream(resp, remote, seg)

    def _accept_ranged(self, resp: Any, remote: RemoteInfo, pos: int, end: int | None) -> int:
        if resp.status_code == 200 or is_encoded(resp):
            if self._changed(remote):
                raise RestartRequired("the file changed on the server", remote=remote)
            raise RestartRequired("the server ignored the byte range", remote=remote, single_connection=True)
        content_range = parse_content_range(resp.headers.get("Content-Range"))
        if content_range is None or content_range.start != pos:
            raise RestartRequired(
                f"unusable Content-Range {resp.headers.get('Content-Range')!r} for offset {pos}",
                remote=remote,
                single_connection=True,
            )
        if self._changed(remote):
            raise RestartRequired("the file changed on the server", remote=remote)
        last = content_range.end if end is None else min(content_range.end, end)
        return last - pos + 1

    def _accept_stream(self, resp: Any, remote: RemoteInfo, seg: Segment) -> int | None:
        if resp.status_code == 206:
            content_range = parse_content_range(resp.headers.get("Content-Range"))
            if content_range is None or content_range.start != 0:
                raise RestartRequired("unexpected partial response", remote=remote, single_connection=True)
        if self._changed(remote):
            raise RestartRequired("the file changed on the server", remote=remote)
        if remote.size is not None:
            with self._lock:  # size and segment end change together (see _persist)
                self._size = remote.size
                self._table.set_end(seg, seg.start + remote.size - 1)
        return remote.size

    def _changed(self, remote: RemoteInfo) -> bool:
        """True when ``remote`` describes a different file; otherwise adopt any new validators."""
        with self._lock:
            if self._size is not None and remote.size is not None and remote.size != self._size:
                return True
            if remote.etag:
                if self._etag and not same_etag(self._etag, remote.etag):
                    return True
                self._etag = self._etag or remote.etag
            if remote.last_modified and not self._last_modified:
                self._last_modified = remote.last_modified
            return False

    def _pump(self, resp: Any, seg: Segment, pos: int, expected: int | None, stats: _ConnectionStats) -> bool:
        """Copy the body into the file; True once ``seg`` is complete."""
        token = self._run_token
        limiter = self._limiter
        table = self._table
        received = 0
        chunks = resp.iter_content(self._config.chunk_size)
        while True:
            try:
                data = next(chunks, None)
            except Exception as exc:  # requests/urllib3/socket errors (or our own abort)
                if token.cancelled:
                    raise OperationCancelled() from exc
                raise TransientError(f"connection lost after {received} bytes ({type(exc).__name__})") from exc
            if data is None:
                break
            if not data:
                continue
            token.raise_if_cancelled()
            limiter.acquire(len(data), token)
            offset, allowed = table.reserve(seg, len(data))
            if allowed:
                self._write(offset, data if allowed == len(data) else memoryview(data)[:allowed])
            received += allowed
            stats.received += allowed
            if table.commit(seg, allowed):
                return True
        # end of body
        if expected is not None and received < expected:
            raise TransientError(f"connection closed early ({received} of {expected} bytes)")
        if seg.end is None:  # unknown size: EOF is the end of the file
            with self._lock:
                self._size = pos + received
                table.set_end(seg, pos + received - 1)
            return True
        if not self._plan.ranged:
            raise TransientError(f"stream ended early ({received} bytes)")
        return False  # server sent a shorter range than requested; ask for the rest

    def _write(self, offset: int, data: bytes | memoryview) -> None:
        try:
            self._part.write_at(offset, data)
        except PartFileClosed as exc:
            raise OperationCancelled() from exc
        except OSError as exc:
            size = self.size
            required = size - self._table.bytes_done() if size is not None else len(data)
            raise disk_error(exc, self._part.path, required=required) from exc


def _give_up(exc: TransientError) -> AnkerError:
    if exc.status == 429:
        return RateLimitedError(int(exc.retry_after) or 60, detail=str(exc))
    return DownloadError("The connection to the download server kept failing.", detail=str(exc))
