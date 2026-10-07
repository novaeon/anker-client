"""HttpDownloader edge cases: TLS aborts, odd servers, odd names, shared limiters, guard rails."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.errors import DownloadError, OperationCancelled
from anker_client.core.models import ResolvedLink
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads import _engine_transfer
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
    make_tls_context,
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


def _engine_threads(file_name: str) -> list[str]:
    """Live connection threads of the download of ``file_name`` (named ``anker-dl-<file>-<n>``)."""
    prefix = f"anker-dl-{file_name}-"
    return [t.name for t in threading.enumerate() if t.name.startswith(prefix) and t.is_alive()]


# --- TLS (the real CDN is HTTPS) ----------------------------------------------------------


def test_https_download_and_prompt_cancel_of_a_stalled_tls_stream(tmp_path: Path, make_server) -> None:
    context, ca_file = make_tls_context(tmp_path)
    data = random_bytes(3 * MIB)
    server = make_server(data, tls=context)
    tls_http = SessionHttp(verify=ca_file)
    try:
        downloader = make_downloader(tls_http)
        done = tmp_path / "ok.rar"
        downloader.download(server.link(), str(done), token=CancelToken())
        assert server.url.startswith("https://")
        assert file_sha256(done) == sha256(data)

        # Blocked SSL reads are only woken by closing the socket handle underneath them.
        server.stall.set()
        stalled = tmp_path / "stalled.rar"
        token = CancelToken()
        thread, outcome = _download_in_thread(downloader, server.link(), stalled, token)
        time.sleep(0.4)
        cancelled_at = time.perf_counter()
        token.cancel("pause")
        thread.join(5)

        assert isinstance(outcome["result"], OperationCancelled)
        assert outcome["finished_at"] - cancelled_at < 1.0
        assert _engine_threads(stalled.name) == []
        HttpDownloader.discard_partial(str(stalled))  # no handle left open on the part file
        assert not any(p.exists() for p in part_files(stalled))
    finally:
        tls_http.close()


def test_cancel_while_waiting_for_response_headers_with_the_real_http_client(tmp_path: Path) -> None:
    from anker_client.site.http import HttpClient

    try:
        client = HttpClient()
    except NotImplementedError:
        pytest.skip("site.http.HttpClient is not implemented yet")
    listener = socket.create_server(("127.0.0.1", 0))
    accepted: list[socket.socket] = []

    def accept_forever() -> None:  # accepts connections and never answers
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(conn)

    threading.Thread(target=accept_forever, daemon=True).start()
    port = listener.getsockname()[1]
    link = ResolvedLink(url=f"http://127.0.0.1:{port}/x.rar", size=4 * MIB, etag='"a"', accept_ranges=True)
    try:
        token = CancelToken()
        thread, outcome = _download_in_thread(make_downloader(client), link, tmp_path / "x.rar", token)
        time.sleep(0.5)
        cancelled_at = time.perf_counter()
        token.cancel("pause")
        thread.join(5)
        assert isinstance(outcome["result"], OperationCancelled)
        assert outcome["finished_at"] - cancelled_at < 1.0
    finally:
        client.close()
        listener.close()
        for conn in accepted:
            conn.close()


# --- odd but legal servers ----------------------------------------------------------------


def test_server_sending_shorter_ranges_than_requested(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(3 * MIB)
    server = make_server(data)
    server.max_range = 100 * KIB  # every 206 covers at most 100 KiB
    dest = tmp_path / "Game.rar"

    make_downloader(http, retry_delays=()).download(server.link(), str(dest), token=CancelToken())  # no retries

    assert file_sha256(dest) == sha256(data)
    assert len(server.records) >= len(data) // (100 * KIB)
    assert all(r.status == 206 for r in server.records)


def test_server_without_validators_still_downloads_and_resumes(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data, etag="")
    server.last_modified = ""
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    state = make_partial(downloader, server.link(), dest, at_least=256 * KIB)
    assert (state["etag"], state["last_modified"]) == ("", "")
    server.reset_records()
    limiter.set_rate(0)
    downloader.download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert all(r.if_range is None and r.status == 206 for r in server.records)
    starts = set(server.ranged_starts())
    for seg in state["segments"]:
        if seg["done"] < seg["end"] - seg["start"] + 1:
            assert seg["start"] + seg["done"] in starts  # continued, not restarted


@pytest.mark.parametrize(
    "name",
    ["Ünïcødé ゲーム [v1.2] (x64) – é.rar", "name with  spaces.and.dots..rar", "#hash %percent& amp;.zip"],
)
def test_unicode_and_odd_file_names(tmp_path: Path, http, make_server, name: str) -> None:
    data = random_bytes(700 * KIB)
    server = make_server(data)
    dest = tmp_path / "Ünïcødé dir" / name

    make_downloader(http).download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert sorted(p.name for p in dest.parent.iterdir()) == [name]


def test_concurrent_downloads_share_one_rate_limit(tmp_path: Path, http, make_server) -> None:
    limiter = RateLimiter(4 * MIB)
    downloader = make_downloader(http, limiter)  # one downloader, several calls at once
    files = [random_bytes(MIB, seed=seed) for seed in (11, 12)]
    servers = [make_server(data) for data in files]
    results: dict[int, str] = {}

    def run(index: int) -> None:
        dest = tmp_path / f"game{index}.rar"
        downloader.download(servers[index].link(), str(dest), token=CancelToken())
        results[index] = file_sha256(dest)

    started = time.perf_counter()
    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    elapsed = time.perf_counter() - started

    assert results == {0: sha256(files[0]), 1: sha256(files[1])}
    assert 0.375 <= elapsed <= 0.8, elapsed  # 2 MiB in total at 4 MiB/s


# --- guard rails --------------------------------------------------------------------------


def test_empty_destination_is_rejected_and_never_resolves_to_the_working_directory(
    tmp_path: Path, http, make_server, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sibling_part = Path(f"{tmp_path}.part")  # what abspath("") + ".part" would name
    sibling_part.write_bytes(b"someone else's file")
    try:
        HttpDownloader.discard_partial("")
        assert sibling_part.exists()
        assert HttpDownloader.partial_size("") == 0
        server = make_server(random_bytes(10 * KIB))
        with pytest.raises(DownloadError):
            make_downloader(http).download(server.link(), "", token=CancelToken())
        assert server.records == []
    finally:
        sibling_part.unlink()


def test_failing_sidecar_saves_warn_once_and_the_download_still_succeeds(
    tmp_path: Path, http, make_server, monkeypatch, caplog
) -> None:
    def broken_save(path: str, state: Any) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(_engine_transfer, "save_sidecar", broken_save)
    data = random_bytes(512 * KIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"

    with caplog.at_level(logging.DEBUG, logger="anker_client.services.downloads"):
        make_downloader(http, RateLimiter(MIB), sidecar_interval=0.1).download(
            server.link(), str(dest), token=CancelToken()
        )

    assert file_sha256(dest) == sha256(data)
    failures = [r for r in caplog.records if "Could not save download state" in r.getMessage()]
    assert len(failures) >= 3  # saved at the start and every interval after
    assert [r.levelno for r in failures].count(logging.WARNING) == 1
