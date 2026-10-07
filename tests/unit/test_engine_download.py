"""HttpDownloader end to end against a local Range-capable server: downloads, resume, progress, sidecar."""

from __future__ import annotations

import os
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from anker_client.core.tasks import CancelToken
from anker_client.services.downloads import _engine_partfile, _engine_transfer
from anker_client.services.downloads.engine import DownloadProgress, HttpDownloader
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


# --- complete downloads -------------------------------------------------------------------


def test_full_download_single_connection(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(MIB + 123)
    server = make_server(data)
    dest = tmp_path / "Game.rar"

    result = make_downloader(http, connections=1).download(server.link(), str(dest), token=CancelToken())

    assert result == str(dest)
    assert file_sha256(dest) == sha256(data)
    assert not any(p.exists() for p in part_files(dest))
    assert len(server.records) == 1
    record = server.records[0]
    assert record.range == f"bytes=0-{len(data) - 1}"
    assert record.if_range == '"v1"'
    assert record.accept_encoding == "identity"


def test_full_download_multi_connection(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(4 * MIB)
    server = make_server(data)
    server.throttle_bps = 8 * MIB  # keep the four connections overlapping
    dest = tmp_path / "nested" / "dir" / "Game.zip"  # missing folders are created

    make_downloader(http, min_segment_size=512 * KIB).download(server.link(), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    # the four initial segments (a late steal may add starts anywhere, e.g. inside segment 0)
    assert {0, MIB, 2 * MIB, 3 * MIB} <= set(server.ranged_starts())
    assert server.max_active >= 3
    assert not any(p.exists() for p in part_files(dest))


def test_small_files_use_one_connection(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(300 * KIB)  # < 2 x min_segment_size
    server = make_server(data)
    make_downloader(http, min_segment_size=256 * KIB).download(server.link(), str(tmp_path / "a"), token=CancelToken())
    assert server.ranged_starts() == [0]


def test_zero_byte_file(tmp_path: Path, http, make_server) -> None:
    server = make_server(b"")
    dest = tmp_path / "empty.bin"
    make_downloader(http).download(server.link(), str(dest), token=CancelToken())
    assert dest.exists() and dest.stat().st_size == 0
    assert not any(p.exists() for p in part_files(dest))


def test_replaces_an_existing_destination_file(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(200 * KIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    dest.write_bytes(b"old archive")
    make_downloader(http).download(server.link(), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)


def test_large_download_is_fast(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(32 * MIB)
    server = make_server(data)
    server.block_size = 256 * KIB
    dest = tmp_path / "big.bin"
    started = time.perf_counter()
    HttpDownloader(http, RateLimiter(0), connections=4, min_segment_size=4 * MIB).download(
        server.link(), str(dest), token=CancelToken()
    )
    assert time.perf_counter() - started < 10
    assert file_sha256(dest) == sha256(data)


# --- resume / sidecar ---------------------------------------------------------------------


def test_cancel_keeps_partial_with_valid_sidecar(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    downloader = make_downloader(http, RateLimiter(MIB))

    state = make_partial(downloader, server.link(), dest, at_least=256 * KIB)

    part, sidecar = part_files(dest)
    assert part.exists() and sidecar.exists()
    assert not Path(f"{sidecar}.tmp").exists()
    assert set(state) == {"version", "url", "size", "etag", "last_modified", "segments"}
    assert state["version"] == 1
    assert state["url"] == server.url
    assert state["size"] == len(data)
    assert state["etag"] == '"v1"'
    assert state["last_modified"] == server.last_modified
    expected_start = 0
    for seg in state["segments"]:
        assert set(seg) == {"start", "end", "done"}
        assert seg["start"] == expected_start
        assert 0 <= seg["done"] <= seg["end"] - seg["start"] + 1
        expected_start = seg["end"] + 1
    assert expected_start == len(data)
    done = sum(seg["done"] for seg in state["segments"])
    assert 0 < done < len(data)
    assert HttpDownloader.partial_size(str(dest)) == done
    # everything the sidecar claims is really on disk
    with open(part, "rb") as fh:
        assert os.path.getsize(part) == len(data)
        for seg in state["segments"]:
            fh.seek(seg["start"])
            assert fh.read(seg["done"]) == data[seg["start"] : seg["start"] + seg["done"]]


def test_resume_continues_from_the_exact_offsets(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    state = make_partial(downloader, server.link(), dest, at_least=512 * KIB)

    server.reset_records()
    limiter.set_rate(0)
    downloader.download(server.link(url=server.url + "?sig=renewed"), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    starts = server.ranged_starts()
    for seg in state["segments"]:
        length = seg["end"] - seg["start"] + 1
        if seg["done"] < length:
            assert seg["start"] + seg["done"] in starts
        if seg["done"]:
            assert seg["start"] not in starts  # nothing downloaded twice from a segment start
    assert all(r.if_range == '"v1"' for r in server.records)
    assert not any(p.exists() for p in part_files(dest))


def test_resume_twice_across_pauses(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    first = make_partial(downloader, server.link(), dest, at_least=256 * KIB)
    first_done = sum(s["done"] for s in first["segments"])
    second = make_partial(downloader, server.link(), dest, at_least=first_done + 256 * KIB)
    assert sum(s["done"] for s in second["segments"]) > first_done
    limiter.set_rate(0)
    downloader.download(server.link(), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)


def test_partial_without_sidecar_is_discarded(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(300 * KIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    Path(f"{dest}.part").write_bytes(b"garbage" * 1000)
    make_downloader(http).download(server.link(), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)


def test_corrupt_sidecar_restarts_from_zero(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(600 * KIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    Path(f"{dest}.part").write_bytes(b"\0" * len(data))
    Path(f"{dest}.part.json").write_text('{"version": 1, "segments": "nope"}', encoding="utf-8")
    make_downloader(http).download(server.link(), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)
    assert 0 in server.ranged_starts()


def test_link_without_range_support_does_not_resume(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    limiter = RateLimiter(MIB)
    downloader = make_downloader(http, limiter)
    make_partial(downloader, server.link(), dest, at_least=256 * KIB)
    server.reset_records()
    server.ranges = server.advertise_ranges = False
    limiter.set_rate(0)
    downloader.download(server.link(accept_ranges=False), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)
    assert [r.range for r in server.records] == [None]


def test_sidecar_is_saved_at_most_once_per_second(tmp_path: Path, http, make_server, monkeypatch) -> None:
    saves: list[float] = []
    real_save = _engine_transfer.save_sidecar

    def counting_save(path: str, state: Any) -> None:
        saves.append(time.perf_counter())
        real_save(path, state)

    monkeypatch.setattr(_engine_transfer, "save_sidecar", counting_save)
    data = random_bytes(1536 * KIB)
    server = make_server(data)
    started = time.perf_counter()
    make_downloader(http, RateLimiter(MIB)).download(server.link(), str(tmp_path / "x"), token=CancelToken())
    duration = time.perf_counter() - started
    assert 1 <= len(saves) <= int(duration) + 2
    assert all(b - a >= 0.95 for a, b in pairwise(saves))


def test_non_sparse_volume_downloads_sequentially(tmp_path: Path, http, make_server, monkeypatch) -> None:
    monkeypatch.setattr(_engine_partfile, "_make_sparse", lambda handle: False)
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    make_downloader(http).download(server.link(), str(dest), token=CancelToken())
    assert file_sha256(dest) == sha256(data)
    assert server.ranged_starts() == [0]


# --- progress -----------------------------------------------------------------------------


def test_progress_is_throttled_monotonic_and_complete(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(1536 * KIB)
    server = make_server(data)
    reports: list[tuple[float, DownloadProgress]] = []
    started = time.perf_counter()
    make_downloader(http, RateLimiter(MIB), connections=2).download(
        server.link(), str(tmp_path / "x"), token=CancelToken(),
        on_progress=lambda p: reports.append((time.perf_counter(), p)),
    )
    duration = time.perf_counter() - started
    times = [t for t, _ in reports]
    progress = [p for _, p in reports]

    assert len(reports) <= duration * 4 + 2
    assert all(b - a >= 0.24 for a, b in pairwise(times[:-1]))  # final report may be sooner
    done = [p.bytes_done for p in progress]
    assert done == sorted(done)
    assert progress[-1].bytes_done == progress[-1].bytes_total == len(data)
    assert progress[-1].eta_seconds == 0.0
    assert all(p.bytes_total == len(data) for p in progress)
    assert max(p.connections for p in progress) >= 1
    late = [p.speed_bps for t, p in reports[:-1] if t - started > 0.6]
    assert late and all(0.5 * MIB < s < 1.8 * MIB for s in late), late
    assert all(p.eta_seconds is None or p.eta_seconds >= 0 for p in progress)


def test_progress_callback_errors_do_not_break_the_download(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(300 * KIB)
    server = make_server(data)

    def explode(_progress: DownloadProgress) -> None:
        raise RuntimeError("ui bug")

    dest = tmp_path / "x"
    make_downloader(http).download(server.link(), str(dest), token=CancelToken(), on_progress=explode)
    assert file_sha256(dest) == sha256(data)


# --- partial_size / discard_partial -------------------------------------------------------


def test_partial_size_and_discard_partial(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    assert HttpDownloader.partial_size(str(dest)) == 0
    HttpDownloader.discard_partial(str(dest))  # nothing to delete: no error

    state = make_partial(make_downloader(http, RateLimiter(MIB)), server.link(), dest, at_least=256 * KIB)
    assert HttpDownloader.partial_size(str(dest)) == sum(s["done"] for s in state["segments"]) > 0

    HttpDownloader.discard_partial(str(dest))
    assert not any(p.exists() for p in part_files(dest))
    assert HttpDownloader.partial_size(str(dest)) == 0
    assert list(tmp_path.iterdir()) == []  # nothing else touched


def test_partial_size_is_zero_when_the_part_file_is_gone(tmp_path: Path, http, make_server) -> None:
    server = make_server(random_bytes(2 * MIB))
    dest = tmp_path / "Game.rar"
    make_partial(make_downloader(http, RateLimiter(MIB)), server.link(), dest, at_least=128 * KIB)
    Path(f"{dest}.part").unlink()
    assert HttpDownloader.partial_size(str(dest)) == 0


# --- integration with the real HttpClient ---------------------------------------------------


def test_works_with_the_real_http_client(tmp_path: Path, make_server) -> None:
    from anker_client.site.http import HttpClient

    try:
        client = HttpClient()
    except NotImplementedError:
        pytest.skip("site.http.HttpClient is not implemented yet")
    try:
        data = random_bytes(3 * MIB)
        server = make_server(data)
        server.status_queue = [503]
        server.disconnect_queue = [64 * KIB]
        dest = tmp_path / "Game.rar"
        make_downloader(client).download(server.link(), str(dest), token=CancelToken())
        assert file_sha256(dest) == sha256(data)
        assert all(r.accept_encoding == "identity" for r in server.records)
    finally:
        client.close()
