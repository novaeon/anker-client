"""ImageCache: sharded storage, validation, atomic writes, de-duplication, pruning, import."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from anker_client.core.errors import AnkerError, NetworkError, NotFoundError, OperationCancelled
from anker_client.core.paths import AppPaths
from anker_client.core.tasks import CancelToken
from anker_client.services import images as images_module
from anker_client.services.images import ImageCache

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x01" * 300
GIF = b"GIF89a" + b"\x02" * 100
WEBP = b"RIFF" + (100).to_bytes(4, "little") + b"WEBPVP8 " + b"\x03" * 100
HTML = b"<!doctype html><html><body>Just a moment...</body></html>"


def sha(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# --- local HTTP server -------------------------------------------------------------------------


@dataclass
class Route:
    body: bytes = PNG
    status: int = 200
    content_type: str | None = "image/png"
    send_length: bool = True
    gate: threading.Event | None = None
    started: threading.Event = field(default_factory=threading.Event)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes: dict[str, Route] = {}
        self.hits: Counter[str] = Counter()
        self.accept_headers: list[str] = []

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        self.server.hits[self.path] += 1
        self.server.accept_headers.append(self.headers.get("Accept", ""))
        route = self.server.routes.get(self.path) or Route(body=b"not found", status=404, content_type="text/plain")
        route.started.set()
        if route.gate is not None:
            route.gate.wait(10)
        self.send_response(route.status)
        if route.content_type is not None:
            self.send_header("Content-Type", route.content_type)
        if route.send_length:
            self.send_header("Content-Length", str(len(route.body)))
        self.end_headers()
        try:
            self.wfile.write(route.body)
        except OSError:
            pass

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


class RequestsHttp:
    """Minimal stand-in for ``HttpClient.get`` (real HTTP, same error mapping)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, stream: bool = False, headers: dict[str, str] | None = None,
            token: CancelToken | None = None, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, "stream": stream, "headers": dict(headers or {}), "token": token})
        try:
            response = requests.get(url, stream=stream, headers=headers, timeout=5)
        except requests.RequestException as exc:
            raise NetworkError(detail=str(exc)) from exc
        if response.status_code == 404:
            response.close()
            raise NotFoundError()
        if response.status_code >= 400:
            response.close()
            raise NetworkError(status=response.status_code)
        return response


# --- scripted fake ------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes = PNG, *, content_type: str = "image/png",
                 chunks: list[bytes | Callable[[], None] | Exception] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        base = {"Content-Type": content_type} if content_type else {}
        self.headers = CaseInsensitiveDict({**base, **(headers or {})})
        self._chunks = chunks if chunks is not None else [body[i : i + 64] for i in range(0, len(body), 64)]
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        for item in self._chunks:
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item()
                continue
            yield item

    def close(self) -> None:
        self.closed = True


class ScriptedHttp:
    def __init__(self, handler: Callable[[str, dict[str, Any]], FakeResponse]) -> None:
        self.handler = handler
        self.calls: list[str] = []
        self.responses: list[FakeResponse] = []
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        with self._lock:
            self.calls.append(url)
        response = self.handler(url, kwargs)
        self.responses.append(response)
        return response


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.under(tmp_path / "home").ensure()


@pytest.fixture
def http() -> RequestsHttp:
    return RequestsHttp()


@pytest.fixture
def cache(http: RequestsHttp, paths: AppPaths) -> ImageCache:
    return ImageCache(http, paths)  # type: ignore[arg-type]


def watch_waiters(cache: ImageCache) -> threading.Event:
    """An event set as soon as a caller starts waiting on another caller's download of the same URL."""
    waiting = threading.Event()
    original = cache._follow  # type: ignore[attr-defined]

    def follow(*args: Any, **kwargs: Any) -> Path | None:
        waiting.set()
        return original(*args, **kwargs)

    cache._follow = follow  # type: ignore[attr-defined,method-assign]
    return waiting


def run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


# --- fetch ------------------------------------------------------------------------------------


class TestFetch:
    @pytest.mark.parametrize(("body", "content_type", "ext"), [
        (PNG, "image/png", ".png"),
        (JPEG, "image/jpeg", ".jpg"),
        (GIF, "image/gif", ".gif"),
        (WEBP, "image/webp", ".webp"),
    ])
    def test_stores_in_sha1_shard(self, server, cache, paths, body, content_type, ext):
        server.routes["/img"] = Route(body=body, content_type=content_type)
        url = server.url("/img")

        path = cache.fetch(url)

        digest = sha(url)
        assert path == paths.images_dir / digest[:2] / f"{digest}{ext}"
        assert path.read_bytes() == body
        assert all_files(paths.images_dir) == [path]  # no temp files left behind

    def test_second_fetch_is_a_cache_hit(self, server, cache, http):
        server.routes["/a.png"] = Route()
        url = server.url("/a.png")
        assert cache.cached_path(url) is None
        first = cache.fetch(url)
        assert cache.fetch(url) == first
        assert cache.cached_path(url) == first
        assert server.hits["/a.png"] == 1
        assert http.calls[0]["stream"] is True
        assert "image/" in http.calls[0]["headers"]["Accept"]

    def test_extension_comes_from_the_bytes_not_the_url(self, server, cache):
        server.routes["/poster.jpg"] = Route(body=WEBP, content_type="image/webp")
        url = server.url("/poster.jpg")
        path = cache.fetch(url)
        assert path.suffix == ".webp"
        assert cache.cached_path(url) == path

    def test_generic_content_type_with_image_bytes_is_accepted(self, server, cache):
        server.routes["/x"] = Route(body=PNG, content_type="application/octet-stream")
        assert cache.fetch(server.url("/x")).suffix == ".png"

    def test_missing_content_type_and_no_length(self, server, cache):
        server.routes["/x"] = Route(body=JPEG, content_type=None, send_length=False)
        assert cache.fetch(server.url("/x")).read_bytes() == JPEG

    @pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/json"])
    def test_non_image_content_type_is_rejected(self, server, cache, paths, content_type):
        server.routes["/x"] = Route(body=PNG, content_type=content_type)
        with pytest.raises(AnkerError):
            cache.fetch(server.url("/x"))
        assert all_files(paths.images_dir) == []

    def test_non_image_body_is_rejected(self, server, cache, paths):
        server.routes["/x.jpg"] = Route(body=HTML, content_type="image/jpeg")
        with pytest.raises(AnkerError) as info:
            cache.fetch(server.url("/x.jpg"))
        assert not isinstance(info.value, NetworkError)
        assert all_files(paths.images_dir) == []

    def test_svg_is_rejected(self, server, cache):
        server.routes["/x.svg"] = Route(body=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                                        content_type="image/svg+xml")
        with pytest.raises(AnkerError):
            cache.fetch(server.url("/x.svg"))

    def test_tiny_and_empty_bodies(self, server, cache):
        server.routes["/empty"] = Route(body=b"", content_type="image/png")
        server.routes["/tiny"] = Route(body=b"GIF89a", content_type="image/gif")
        with pytest.raises(AnkerError):
            cache.fetch(server.url("/empty"))
        assert cache.fetch(server.url("/tiny")).suffix == ".gif"

    def test_oversized_by_content_length(self, server, cache, paths, monkeypatch):
        monkeypatch.setattr(images_module, "MAX_IMAGE_BYTES", 300)
        server.routes["/big"] = Route(body=PNG)  # 520 bytes, announced
        with pytest.raises(AnkerError):
            cache.fetch(server.url("/big"))
        assert all_files(paths.images_dir) == []

    def test_oversized_while_streaming(self, server, cache, paths, monkeypatch):
        monkeypatch.setattr(images_module, "MAX_IMAGE_BYTES", 300)
        server.routes["/big"] = Route(body=PNG, send_length=False)
        with pytest.raises(AnkerError):
            cache.fetch(server.url("/big"))
        assert all_files(paths.images_dir) == []

    def test_real_limit_is_20_mib(self):
        assert images_module.MAX_IMAGE_BYTES == 20 * 1024 * 1024

    def test_http_errors_propagate(self, server, cache):
        with pytest.raises(NotFoundError):
            cache.fetch(server.url("/missing.png"))
        server.routes["/boom"] = Route(status=500, content_type="text/plain", body=b"error")
        with pytest.raises(NetworkError):
            cache.fetch(server.url("/boom"))

    @pytest.mark.parametrize("url", ["", "   ", "data:image/png;base64,AAAA", "C:\\covers\\x.png", "ftp://x/y.png"])
    def test_unsupported_urls(self, cache, url):
        with pytest.raises(NotFoundError):
            cache.fetch(url)
        assert cache.cached_path(url) is None

    def test_protocol_relative_url(self, paths):
        http = ScriptedHttp(lambda url, kw: FakeResponse(PNG))
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        path = cache.fetch("//cdn.example/x.png")
        assert http.calls == ["https://cdn.example/x.png"]
        assert cache.cached_path("https://cdn.example/x.png") == path

    def test_response_is_always_closed(self, paths):
        http = ScriptedHttp(lambda url, kw: FakeResponse(HTML, content_type="image/png"))
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        with pytest.raises(AnkerError):
            cache.fetch("https://x/a.png")
        http.handler = lambda url, kw: FakeResponse(PNG)
        cache.fetch("https://x/b.png")
        assert [r.closed for r in http.responses] == [True, True]

    def test_connection_error_mid_stream(self, paths):
        http = ScriptedHttp(lambda url, kw: FakeResponse(
            chunks=[PNG[:64], requests.exceptions.ChunkedEncodingError("connection reset")]))
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        with pytest.raises(NetworkError):
            cache.fetch("https://x/a.png")
        assert all_files(paths.images_dir) == []
        assert http.responses[0].closed

    def test_cancel_mid_stream(self, paths):
        token = CancelToken()
        http = ScriptedHttp(lambda url, kw: FakeResponse(chunks=[PNG[:64], token.cancel, PNG[64:]]))
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        with pytest.raises(OperationCancelled):
            cache.fetch("https://x/a.png", token=token)
        assert all_files(paths.images_dir) == []
        assert cache.cached_path("https://x/a.png") is None

    def test_token_is_passed_to_http(self, paths):
        seen: list[Any] = []
        http = ScriptedHttp(lambda url, kw: seen.append(kw.get("token")) or FakeResponse(PNG))
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        token = CancelToken()
        cache.fetch("https://x/a.png", token=token)
        assert seen == [token]

    def test_refetch_after_file_deleted(self, server, cache):
        server.routes["/a.png"] = Route()
        url = server.url("/a.png")
        cache.fetch(url).unlink()
        assert cache.cached_path(url) is None
        assert cache.fetch(url).exists()
        assert server.hits["/a.png"] == 2


class TestWithRealHttpClient:
    """The same flows through the production ``HttpClient`` (streaming, error mapping, cancellation)."""

    @pytest.fixture
    def real_cache(self, paths: AppPaths) -> Iterator[ImageCache]:
        from anker_client.site.http import HttpClient

        http = HttpClient(paced_hosts=["pacing.invalid"], max_attempts=1)
        yield ImageCache(http, paths)
        http.close()

    def test_fetch_validate_and_errors(self, server, real_cache, paths):
        server.routes["/a.png"] = Route(body=PNG)
        server.routes["/b"] = Route(body=JPEG, content_type="image/jpeg", send_length=False)
        server.routes["/c"] = Route(body=HTML, content_type="text/html; charset=utf-8")

        assert real_cache.fetch(server.url("/a.png"), token=CancelToken()).read_bytes() == PNG
        assert real_cache.fetch(server.url("/b")).suffix == ".jpg"  # no token: inline request
        with pytest.raises(AnkerError) as info:
            real_cache.fetch(server.url("/c"))
        assert not isinstance(info.value, NetworkError)
        with pytest.raises(NotFoundError):
            real_cache.fetch(server.url("/missing.png"), token=CancelToken())
        assert len(all_files(paths.images_dir)) == 2

    def test_cancel_while_the_server_stalls(self, server, real_cache, paths):
        gate = threading.Event()
        server.routes["/slow.png"] = Route(gate=gate)
        token = CancelToken()
        threading.Timer(0.1, token.cancel).start()
        started = time.monotonic()
        with pytest.raises(OperationCancelled):
            real_cache.fetch(server.url("/slow.png"), token=token)
        assert time.monotonic() - started < 3
        gate.set()
        assert real_cache.fetch(server.url("/slow.png")).read_bytes() == PNG
        assert [p.suffix for p in all_files(paths.images_dir)] == [".png"]


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_commit_onto_a_file_open_for_reading(paths):
    cache = ImageCache(ScriptedHttp(lambda url, kw: FakeResponse(PNG)), paths)  # type: ignore[arg-type]
    url = "https://x/a.png"
    digest = sha(url)
    final = paths.images_dir / digest[:2] / f"{digest}.png"
    final.parent.mkdir(parents=True)
    final.write_bytes(PNG)
    tmp = cache._new_temp_file(digest)  # type: ignore[attr-defined]
    tmp.write_bytes(PNG)
    with open(final, "rb"):  # a reader holds the file → os.replace fails with a sharing violation
        assert cache._commit(tmp, digest, ".png") == final  # type: ignore[attr-defined]
    assert not tmp.exists()
    assert final.read_bytes() == PNG


class TestConcurrency:
    def test_same_url_is_downloaded_once(self, server, cache):
        gate = threading.Event()
        route = server.routes["/slow.png"] = Route(gate=gate)
        url = server.url("/slow.png")

        waiting = watch_waiters(cache)
        first, out1 = run_in_thread(lambda: cache.fetch(url))
        assert route.started.wait(5)
        second, out2 = run_in_thread(lambda: cache.fetch(url))
        assert waiting.wait(5)  # the second caller is now waiting on the first
        assert server.hits["/slow.png"] == 1
        gate.set()
        first.join(5)
        second.join(5)

        assert out1["result"] == out2["result"]
        assert out1["result"].read_bytes() == PNG
        assert server.hits["/slow.png"] == 1

    def test_different_urls_download_in_parallel(self, server, cache):
        gate = threading.Event()
        routes = [server.routes.setdefault(f"/p{i}.png", Route(gate=gate)) for i in range(3)]
        threads = [run_in_thread(lambda i=i: cache.fetch(server.url(f"/p{i}.png"))) for i in range(3)]
        assert all(r.started.wait(5) for r in routes)  # all three requests are in flight together
        gate.set()
        for thread, outcome in threads:
            thread.join(5)
            assert outcome["result"].exists()

    def test_waiter_retries_when_the_leaders_file_vanished(self, paths):
        gate = threading.Event()
        in_handler = threading.Event()

        def handler(url: str, kw: dict[str, Any]) -> FakeResponse:
            if len(http.calls) == 1:
                in_handler.set()
                gate.wait(5)
            return FakeResponse(PNG)

        http = ScriptedHttp(handler)
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        original_commit = cache._commit  # type: ignore[attr-defined]
        commits = 0

        def commit_then_clear_once(*args: Any) -> Path:
            nonlocal commits
            commits += 1
            path = original_commit(*args)
            if commits == 1:
                path.unlink()  # e.g. "Clear cache" ran right after the first download finished
            return path

        cache._commit = commit_then_clear_once  # type: ignore[attr-defined,method-assign]
        waiting = watch_waiters(cache)
        first, _ = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert in_handler.wait(5)
        second, out2 = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert waiting.wait(5)  # the second caller is waiting on the first
        gate.set()
        first.join(5)
        second.join(5)
        assert out2["result"].exists()
        assert len(http.calls) == 2

    def test_waiter_shares_the_leaders_error(self, paths):
        gate = threading.Event()
        in_handler = threading.Event()

        def handler(url: str, kw: dict[str, Any]) -> FakeResponse:
            in_handler.set()
            gate.wait(5)
            raise NetworkError(status=502)

        http = ScriptedHttp(handler)
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        waiting = watch_waiters(cache)
        first, out1 = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert in_handler.wait(5)
        second, out2 = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert waiting.wait(5)
        gate.set()
        first.join(5)
        second.join(5)
        assert isinstance(out1["error"], NetworkError)
        assert isinstance(out2["error"], NetworkError)
        assert http.calls == ["https://x/a.png"]

    def test_waiter_takes_over_when_the_leader_is_cancelled(self, paths):
        in_handler = threading.Event()

        def handler(url: str, kw: dict[str, Any]) -> FakeResponse:
            if len(http.calls) == 1:
                in_handler.set()
                token: CancelToken = kw["token"]
                token.wait(5)
                raise OperationCancelled()
            return FakeResponse(PNG)

        http = ScriptedHttp(handler)
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        leader_token = CancelToken()
        waiting = watch_waiters(cache)
        first, out1 = run_in_thread(lambda: cache.fetch("https://x/a.png", token=leader_token))
        assert in_handler.wait(5)
        second, out2 = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert waiting.wait(5)
        leader_token.cancel()
        first.join(5)
        second.join(5)
        assert isinstance(out1["error"], OperationCancelled)
        assert out2["result"].read_bytes() == PNG
        assert len(http.calls) == 2

    def test_waiter_can_be_cancelled(self, paths):
        gate = threading.Event()
        in_handler = threading.Event()

        def handler(url: str, kw: dict[str, Any]) -> FakeResponse:
            in_handler.set()
            gate.wait(5)
            return FakeResponse(PNG)

        http = ScriptedHttp(handler)
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        waiting = watch_waiters(cache)
        first, out1 = run_in_thread(lambda: cache.fetch("https://x/a.png"))
        assert in_handler.wait(5)
        waiter_token = CancelToken()
        second, out2 = run_in_thread(lambda: cache.fetch("https://x/a.png", token=waiter_token))
        assert waiting.wait(5)
        started = time.monotonic()
        waiter_token.cancel()
        second.join(5)
        assert time.monotonic() - started < 1
        assert isinstance(out2["error"], OperationCancelled)
        gate.set()
        first.join(5)
        assert out1["result"].exists()


# --- maintenance --------------------------------------------------------------------------------


def put(root: Path, key: str, size: int, age_seconds: float, suffix: str = ".png") -> Path:
    digest = sha(key)
    shard = root / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / f"{digest}{suffix}"
    path.write_bytes(b"x" * size)
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))
    return path


class TestMaintenance:
    def test_size_bytes(self, cache, paths):
        assert cache.size_bytes() == 0
        put(paths.images_dir, "a", 100, 0)
        put(paths.images_dir, "b", 250, 0)
        assert cache.size_bytes() == 350

    def test_prune_deletes_least_recently_used_down_to_90_percent(self, http, paths):
        cache = ImageCache(http, paths, max_bytes=1000)  # type: ignore[arg-type]
        oldest = put(paths.images_dir, "a", 400, 4000)
        older = put(paths.images_dir, "b", 400, 3000)
        newer = put(paths.images_dir, "c", 400, 2000)
        newest = put(paths.images_dir, "d", 400, 1000)

        assert cache.prune() == 800

        assert not oldest.exists() and not older.exists()
        assert newer.exists() and newest.exists()
        assert cache.size_bytes() == 800
        assert cache.prune() == 0  # under the limit now

    def test_prune_under_limit_deletes_nothing(self, http, paths):
        cache = ImageCache(http, paths, max_bytes=10_000)  # type: ignore[arg-type]
        files = [put(paths.images_dir, k, 100, 99999) for k in "abc"]
        assert cache.prune() == 0
        assert all(f.exists() for f in files)

    def test_access_time_counts_as_use(self, http, paths):
        cache = ImageCache(http, paths, max_bytes=500)  # type: ignore[arg-type]
        read_recently = put(paths.images_dir, "a", 400, 5000)
        old_atime = time.time() - 5000
        os.utime(read_recently, (time.time(), old_atime))  # atime fresh, mtime old
        stale = put(paths.images_dir, "b", 400, 1000)
        cache.prune()
        assert read_recently.exists()
        assert not stale.exists()

    def test_cache_hits_touch_the_file(self, paths):
        http = ScriptedHttp(lambda url, kw: FakeResponse(PNG))
        cache = ImageCache(http, paths, max_bytes=len(PNG) * 2)  # type: ignore[arg-type]
        urls = [f"https://x/{i}.png" for i in range(3)]
        stored = [cache.fetch(u) for u in urls]
        for age, path in zip((9000, 8000, 7000), stored, strict=True):  # older than the 1 h touch throttle
            moment = time.time() - age
            os.utime(path, (moment, moment))

        assert cache.cached_path(urls[0]) == stored[0]  # hit on the oldest → touched
        cache.prune()

        assert stored[0].exists()
        assert not stored[1].exists()
        assert len(http.calls) == 3

    def test_prune_removes_abandoned_temp_files_only(self, http, paths):
        cache = ImageCache(http, paths, max_bytes=10_000)  # type: ignore[arg-type]
        stale_tmp = put(paths.images_dir, "t1", 50, 7200, suffix=".x.tmp")
        fresh_tmp = put(paths.images_dir, "t2", 50, 10, suffix=".x.tmp")
        assert cache.prune() == 50
        assert not stale_tmp.exists()
        assert fresh_tmp.exists()

    def test_clear_only_touches_shard_folders(self, cache, paths):
        cached = put(paths.images_dir, "a", 10, 0)
        fresh_tmp = put(paths.images_dir, "b", 10, 0, suffix=".x.tmp")
        stale_tmp = put(paths.images_dir, "c", 10, 9000, suffix=".x.tmp")
        foreign_file = paths.images_dir / "keep.txt"
        foreign_file.write_text("mine")
        foreign_dir = paths.images_dir / "notashard"
        foreign_dir.mkdir()
        (foreign_dir / "x.png").write_bytes(PNG)
        upper_dir = paths.images_dir / "zz"
        upper_dir.mkdir()
        (upper_dir / "y.png").write_bytes(PNG)
        outside = paths.cache_dir / "other.png"
        outside.write_bytes(PNG)

        cache.clear()

        assert not cached.exists() and not stale_tmp.exists()
        assert not cached.parent.exists() or cached.parent == fresh_tmp.parent
        assert fresh_tmp.exists()
        assert foreign_file.exists() and (foreign_dir / "x.png").exists() and (upper_dir / "y.png").exists()
        assert outside.exists()
        assert paths.images_dir.exists()

    def test_fetch_works_after_clear(self, server, cache):
        server.routes["/a.png"] = Route()
        url = server.url("/a.png")
        cache.fetch(url)
        cache.clear()
        assert cache.cached_path(url) is None
        assert cache.fetch(url).exists()

    def test_missing_cache_dir_is_tolerated(self, http, tmp_path):
        paths = AppPaths.under(tmp_path / "nowhere")
        cache = ImageCache(http, paths)  # type: ignore[arg-type]
        import shutil

        shutil.rmtree(paths.images_dir)
        assert cache.size_bytes() == 0
        assert cache.prune() == 0
        cache.clear()


class TestImportFile:
    def test_imports_a_legacy_cover(self, cache, paths, tmp_path):
        source = tmp_path / "Hollow Knight.png"
        source.write_bytes(PNG)
        url = "https://ankergames.net/uploads/hk.jpg"

        path = cache.import_file(url, source)

        digest = sha(url)
        assert path == paths.images_dir / digest[:2] / f"{digest}.png"
        assert path.read_bytes() == PNG
        assert cache.cached_path(url) == path
        assert source.exists()  # the legacy file is left in place

    def test_existing_entry_wins(self, cache, tmp_path):
        url = "https://x/a.png"
        first = tmp_path / "first.png"
        first.write_bytes(PNG)
        second = tmp_path / "second.jpg"
        second.write_bytes(JPEG)
        stored = cache.import_file(url, first)
        assert cache.import_file(url, second) == stored
        assert stored.read_bytes() == PNG

    def test_rejects_bad_sources(self, cache, tmp_path, monkeypatch):
        not_image = tmp_path / "x.png"
        not_image.write_bytes(HTML)
        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")
        assert cache.import_file("https://x/1.png", tmp_path / "missing.png") is None
        assert cache.import_file("https://x/2.png", not_image) is None
        assert cache.import_file("https://x/3.png", empty) is None
        assert cache.import_file("https://x/4.png", tmp_path) is None  # a directory
        assert cache.import_file("", not_image) is None
        big = tmp_path / "big.png"
        big.write_bytes(PNG)
        monkeypatch.setattr(images_module, "MAX_IMAGE_BYTES", 100)
        assert cache.import_file("https://x/5.png", big) is None
        assert all_files(cache._root) == []  # type: ignore[attr-defined]


def test_import_file_never_raises_on_cache_write_errors(cache, tmp_path, monkeypatch):
    source = tmp_path / "cover.png"
    source.write_bytes(PNG)

    def broken(*args: Any) -> Path:
        raise AnkerError("Could not write to the image cache.")

    monkeypatch.setattr(cache, "_commit", broken)
    assert cache.import_file("https://x/a.png", source) is None
    assert all_files(cache._root) == []  # type: ignore[attr-defined]
