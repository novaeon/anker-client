"""HttpDownloader under faults: restarts, retries, expired links, disk errors, cancellation latency."""

from __future__ import annotations

import errno
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.errors import (
    DiskSpaceError,
    DownloadError,
    LinkExpiredError,
    OperationCancelled,
    RateLimitedError,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads import _engine_partfile, engine
from anker_client.services.downloads.engine import HttpDownloader
from anker_client.services.downloads.ratelimit import RateLimiter
from tests.unit.test_engine_support import (
    KIB,
    MIB,
    FileServer,
    SessionHttp,
    file_sha256,
    make_downloader,
    make_partial,
    part_files,
    random_bytes,
    sha256,
)


@pytest.fixture
def http():
    client = SessionHttp()
    yield client
    client.close()


@pytest.fixture
def make_server():
    servers: list[FileServer] = []

    def make(data: bytes, **kwargs: Any) -> FileServer:
        server = FileServer(data, **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _download_in_thread(downloader: HttpDownloader, link: Any, dest: Path, token: CancelToken) -> tuple:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            downloader.download(link, str(dest), token=token)
            outcome["result"] = "ok"
        except BaseException as exc:
            outcome["result"] = exc
        outcome["finished_at"] = time.perf_counter()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


# --- work stealing ------------------------------------------------------------------------


def test_work_stealing_keeps_all_connections_busy_near_the_end(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(4 * MIB)
    server = make_server(data)
    server.throttle_bps = 4 * MIB
    server.slow_first_bps = 128 * KIB  # the connection for segment 0 crawls: 1 MiB would take 8 s
    dest = tmp_path / "Game.rar"

    reports: list = []
    started = time.perf_counter()
    make_downloader(http, min_segment_size=512 * KIB, min_steal_size=32 * KIB).download(
        server.link(), str(dest), token=CancelToken(), on_progress=reports.append
    )
    elapsed = time.perf_counter() - started

    assert file_sha256(dest) == sha256(data)
    assert elapsed < 4.0, elapsed
    stolen_from_slow = [s for s in server.ranged_starts() if 0 < s < MIB]
    assert len(stolen_from_slow) >= 3  # the idle connections all piled onto the slow tail
    connections = [p.connections for p in reports[:-1]]
    assert max(connections) == 4 and all(c <= 4 for c in connections)


# --- restarts -----------------------------------------------------------------------------


def test_server_ignoring_range_restarts_with_one_connection(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    server.ranges = False  # still advertises Accept-Ranges, but always answers 200
    dest = tmp_path / "Game.rar"

    make_downloader(http).download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    ranges = [r.range for r in server.records]
    assert ranges[-1] is None
    first_plain = ranges.index(None)
    assert all(r is None for r in ranges[first_plain:])
    assert len(ranges) - first_plain == 1


def test_etag_change_between_runs_restarts_from_zero(tmp_path: Path, http, make_server) -> None:
    old = random_bytes(2 * MIB, seed=1)
    server = make_server(old)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    make_partial(downloader, server.link(), dest, at_least=512 * KIB)

    new = random_bytes(2 * MIB, seed=2)  # same size, new build
    with server.lock:
        server.data, server.etag = new, '"v2"'
    server.reset_records()
    limiter.set_rate(0)
    downloader.download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(new)
    assert 0 in server.ranged_starts()
    assert all(r.if_range == '"v2"' for r in server.records)


def test_size_change_seen_in_content_range_restarts_from_zero(tmp_path: Path, http, make_server) -> None:
    old = random_bytes(2 * MIB, seed=1)
    server = make_server(old)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    stale_link = server.link()
    make_partial(downloader, stale_link, dest, at_least=512 * KIB)

    new = random_bytes(3 * MIB, seed=3)
    with server.lock:
        server.data = new  # same ETag (misconfigured origin), different size
    limiter.set_rate(0)
    downloader.download(stale_link, str(dest), token=CancelToken())  # caller still has the old metadata

    assert file_sha256(dest) == sha256(new)


def test_file_replaced_behind_a_stale_link_is_detected_by_if_range(tmp_path: Path, http, make_server) -> None:
    old = random_bytes(2 * MIB, seed=1)
    server = make_server(old)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    stale_link = server.link()
    make_partial(downloader, stale_link, dest, at_least=512 * KIB)

    new = random_bytes(2 * MIB + 5, seed=4)
    with server.lock:
        server.data, server.etag = new, '"v2"'
    server.reset_records()
    limiter.set_rate(0)
    downloader.download(stale_link, str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(new)
    assert any(r.status == 200 for r in server.records)  # If-Range mismatch → full body
    assert any(r.if_range == '"v2"' for r in server.records)  # restarted with the new validator


def test_restarts_are_bounded(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(512 * KIB)
    server = make_server(data)

    class FlappingHttp(SessionHttp):
        """Every response claims a brand-new ETag."""

        def get(self, url: str, **kwargs: Any) -> Any:
            response = super().get(url, **kwargs)
            response.headers["ETag"] = f'"{time.perf_counter_ns()}"'
            return response

    flapping = FlappingHttp()
    try:
        downloader = make_downloader(flapping, max_restarts=2)
        with pytest.raises(DownloadError, match="kept changing"):
            downloader.download(server.link(), str(tmp_path / "x"), token=CancelToken())
    finally:
        flapping.close()


# --- transient failures -------------------------------------------------------------------


def test_mid_stream_disconnects_are_retried_transparently(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    server.disconnect_queue = [100 * KIB, 30 * KIB, 200 * KIB]
    dest = tmp_path / "Game.rar"

    make_downloader(http).download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert len(server.records) >= 4 + 3


def test_disconnect_without_range_support_restarts_the_stream(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(MIB)
    server = make_server(data)
    server.ranges = server.advertise_ranges = False
    server.disconnect_queue = [300 * KIB]
    dest = tmp_path / "Game.rar"

    make_downloader(http).download(server.link(accept_ranges=False), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert [r.range for r in server.records] == [None, None]


def test_503_once_is_retried(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(400 * KIB)
    server = make_server(data)
    server.status_queue = [503]
    dest = tmp_path / "Game.rar"

    make_downloader(http, connections=1).download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert [r.status for r in server.records] == [503, 206]


def test_retry_after_is_honoured(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(100 * KIB)
    server = make_server(data)
    server.status_queue = [429]
    server.retry_after = "1"
    started = time.perf_counter()
    make_downloader(http, connections=1).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    assert time.perf_counter() - started >= 0.9


def test_persistent_5xx_gives_up_after_five_retries(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(100 * KIB))
    server.status_all = 502
    dest = tmp_path / "Game.rar"
    with pytest.raises(DownloadError) as info:
        make_downloader(http, connections=1).download(server.link(), str(dest), token=CancelToken())
    assert not isinstance(info.value, LinkExpiredError)
    assert len(server.records) == 6  # first attempt + 5 retries
    assert part_files(dest)[1].exists()  # state kept for a later retry


def test_persistent_429_becomes_rate_limited_error(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(100 * KIB))
    server.status_all = 429
    with pytest.raises(RateLimitedError):
        make_downloader(http, connections=1).download(server.link(), str(tmp_path / "x"), token=CancelToken())


def test_unexpected_status_is_a_download_error(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(100 * KIB))
    server.status_all = 400
    with pytest.raises(DownloadError, match="HTTP 400"):
        make_downloader(http, connections=1).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    assert len(server.records) == 1


# --- expired links ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_expired_statuses_raise_link_expired(tmp_path: Path, http, make_server, status) -> None:
    server = make_server(random_bytes(100 * KIB))
    server.status_all = status
    with pytest.raises(LinkExpiredError):
        make_downloader(http, connections=1).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    assert len(server.records) == 1  # never retried with the same URL


@pytest.mark.parametrize("status", [403, 410])
def test_expired_link_keeps_partial_and_resumes_with_a_new_link(tmp_path: Path, http, make_server, status) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    make_partial(downloader, server.link(), dest, at_least=256 * KIB)
    before = HttpDownloader.partial_size(str(dest))

    server.status_all = status
    with pytest.raises(LinkExpiredError):
        downloader.download(server.link(), str(dest), token=CancelToken())
    assert all(p.exists() for p in part_files(dest))
    assert HttpDownloader.partial_size(str(dest)) == before

    server.status_all = None
    limiter.set_rate(0)
    downloader.download(server.link(url=server.url + "?sig=fresh"), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)


def test_html_page_instead_of_file_means_expired_link(tmp_path: Path, http, make_server) -> None:
    server = make_server(b"<html>Session expired</html>" * 100)
    server.content_type = "text/html; charset=utf-8"
    with pytest.raises(LinkExpiredError):
        make_downloader(http, connections=1).download(
            server.link(content_type="application/x-rar-compressed"), str(tmp_path / "x"), token=CancelToken()
        )


# --- unknown size -------------------------------------------------------------------------


def test_no_content_length_uses_a_single_stream(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(MIB + 7)
    server = make_server(data)
    server.content_length = False
    server.ranges = server.advertise_ranges = False
    dest = tmp_path / "Game.rar"
    reports: list = []

    make_downloader(http, RateLimiter(2 * MIB)).download(
        server.link(size=None, accept_ranges=False, etag=""), str(dest), token=CancelToken(),
        on_progress=reports.append,
    )

    assert file_sha256(dest) == sha256(data)
    assert [r.range for r in server.records] == [None]
    assert all(p.bytes_total is None for p in reports[:-1])
    assert reports[-1].bytes_done == reports[-1].bytes_total == len(data)


def test_unknown_size_learned_from_content_length(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(300 * KIB)
    server = make_server(data)
    server.ranges = server.advertise_ranges = False
    dest = tmp_path / "Game.rar"
    make_downloader(http).download(server.link(size=None, accept_ranges=False), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)


# --- disk errors --------------------------------------------------------------------------


def _failing_writes(monkeypatch, error: OSError, *, after: int) -> None:
    real_write = _engine_partfile.PartFile.write_at
    calls = {"n": 0}
    lock = threading.Lock()

    def write_at(self, offset: int, data: Any) -> None:
        with lock:
            calls["n"] += 1
            fail = calls["n"] > after
        if fail:
            raise error
        real_write(self, offset, data)

    monkeypatch.setattr(_engine_partfile.PartFile, "write_at", write_at)


def test_disk_full_raises_disk_space_error_and_keeps_state(tmp_path: Path, http, make_server, monkeypatch) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    _failing_writes(monkeypatch, OSError(errno.ENOSPC, "No space left on device"), after=10)

    with pytest.raises(DiskSpaceError) as info:
        make_downloader(http).download(server.link(), str(dest), token=CancelToken())

    assert "disk space" in info.value.user_message.lower()
    part, sidecar = part_files(dest)
    assert part.exists() and sidecar.exists()
    assert 0 < HttpDownloader.partial_size(str(dest)) < len(data)


def test_other_write_errors_raise_download_error(tmp_path: Path, http, make_server, monkeypatch) -> None:
    server = make_server(random_bytes(MIB))
    _failing_writes(monkeypatch, OSError(errno.EACCES, "Access is denied"), after=0)
    with pytest.raises(DownloadError) as info:
        make_downloader(http).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    assert not isinstance(info.value, DiskSpaceError)
    assert "Access is denied" in info.value.user_message


def test_free_space_is_checked_before_downloading(tmp_path: Path, http, make_server, monkeypatch) -> None:
    server = make_server(random_bytes(MIB))
    monkeypatch.setattr(engine, "free_bytes", lambda path: 1000)
    with pytest.raises(DiskSpaceError) as info:
        make_downloader(http).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    assert info.value.required == MIB and info.value.available == 1000
    assert server.records == []


def test_destination_that_is_a_folder_is_rejected(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(10 * KIB))
    (tmp_path / "Game.rar").mkdir()
    with pytest.raises(DownloadError):
        make_downloader(http).download(server.link(), str(tmp_path / "Game.rar"), token=CancelToken())


# --- cancellation -------------------------------------------------------------------------


def test_cancel_latency_under_one_second_while_throttled(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(4 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    token = CancelToken()
    thread, outcome = _download_in_thread(make_downloader(http, RateLimiter(64 * KIB)), server.link(), dest, token)

    time.sleep(0.6)
    cancelled_at = time.perf_counter()
    token.cancel("pause")
    thread.join(5)

    assert isinstance(outcome["result"], OperationCancelled)
    assert outcome["finished_at"] - cancelled_at < 1.0
    assert all(p.exists() for p in part_files(dest))
    assert HttpDownloader.partial_size(str(dest)) > 0


def test_cancel_latency_under_one_second_when_the_server_stalls(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    server.stall.set()  # every response stops after its first block (read timeout is 30 s)
    dest = tmp_path / "Game.rar"
    token = CancelToken()
    thread, outcome = _download_in_thread(make_downloader(http), server.link(), dest, token)

    time.sleep(0.6)
    cancelled_at = time.perf_counter()
    token.cancel("pause")
    thread.join(5)

    assert isinstance(outcome["result"], OperationCancelled)
    assert outcome["finished_at"] - cancelled_at < 1.0
    HttpDownloader.discard_partial(str(dest))  # handles are released: deleting works on Windows
    assert not any(p.exists() for p in part_files(dest))


def test_cancel_during_retry_backoff_is_prompt(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(100 * KIB))
    server.status_all = 503
    token = CancelToken()
    downloader = make_downloader(http, connections=1, retry_delays=(30.0,))
    thread, outcome = _download_in_thread(downloader, server.link(), tmp_path / "x", token)
    time.sleep(0.3)
    cancelled_at = time.perf_counter()
    token.cancel()
    thread.join(5)
    assert isinstance(outcome["result"], OperationCancelled)
    assert outcome["finished_at"] - cancelled_at < 0.5


def test_already_cancelled_token_does_nothing(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(10 * KIB))
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        make_downloader(http).download(server.link(), str(tmp_path / "x"), token=token)
    assert server.records == []
    assert not any(p.exists() for p in part_files(tmp_path / "x"))
