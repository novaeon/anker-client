"""A tiny scriptable HTTP server on 127.0.0.1 for the site package tests (plus its own sanity tests).

Other ``test_site_*`` modules import :class:`LocalSite`, :class:`Reply` and wrap
:func:`serve_local_site` in their own ``local_site`` fixture.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests


@dataclass
class Reply:
    status: int = 200
    body: bytes | str = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    content_type: str = "text/html; charset=utf-8"

    @classmethod
    def json(cls, data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
        return cls(status, json.dumps(data), dict(headers or {}), content_type="application/json")

    @classmethod
    def redirect(cls, location: str, status: int = 302, headers: dict[str, str] | None = None) -> Reply:
        return cls(status, b"", {"Location": location, **(headers or {})})


@dataclass
class Seen:
    """One request as the server received it."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    raw_query: str = ""  # the query string exactly as sent (``query`` is parsed)

    @property
    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        jar.load(self.headers.get("cookie", ""))
        return {k: m.value for k, m in jar.items()}

    @property
    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.body.decode(), keep_blank_values=True).items()}

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode() or "null")


Handler = Callable[[Seen], Reply]
Route = Reply | Handler | list[Reply]


class LocalSite:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Route] = {}
        self._lock = threading.Lock()
        self.requests: list[Seen] = []
        site = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # quiet
                pass

            def handle(self) -> None:
                try:
                    super().handle()
                except (ConnectionError, OSError):
                    pass  # the client dropped a kept-alive connection (streamed probes, cancellation)

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                seen = Seen(
                    method=self.command,
                    path=parts.path,
                    query=parse_qs(parts.query, keep_blank_values=True),
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                    raw_query=parts.query,
                )
                reply = site._dispatch(seen)
                if reply.delay:
                    time.sleep(reply.delay)
                payload = reply.body.encode("utf-8") if isinstance(reply.body, str) else reply.body
                try:
                    self.send_response(reply.status)
                    headers = {"Content-Type": reply.content_type, **reply.headers}
                    for key, value in headers.items():
                        for item in value.split("\n") if key.lower() == "set-cookie" else [value]:
                            self.send_header(key, item)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
                except (ConnectionError, OSError):
                    pass  # the client went away (cancellation tests)

            do_GET = do_POST = do_HEAD = _serve  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True, name="local-site"
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base_url + path

    def route(self, method: str, path: str, route: Route) -> None:
        with self._lock:
            self._routes[(method.upper(), path)] = route

    def seen(self, method: str | None = None, path: str | None = None) -> list[Seen]:
        with self._lock:
            return [
                r for r in self.requests if (method is None or r.method == method) and (path is None or r.path == path)
            ]

    def _dispatch(self, seen: Seen) -> Reply:
        with self._lock:
            self.requests.append(seen)
            route = self._routes.get((seen.method, seen.path))
            if route is None and seen.method == "HEAD":
                route = self._routes.get(("GET", seen.path))
            if isinstance(route, list):
                reply = route.pop(0) if len(route) > 1 else route[0]
                return reply
        if route is None:
            return Reply(404, "not found", content_type="text/plain")
        if callable(route) and not isinstance(route, Reply):
            return route(seen)
        return route

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def serve_local_site() -> Iterator[LocalSite]:
    """Generator body for a ``local_site`` fixture: a running server, closed afterwards."""
    site = LocalSite()
    try:
        yield site
    finally:
        site.close()


@pytest.fixture
def local_site() -> Iterator[LocalSite]:
    yield from serve_local_site()


# --- sanity tests of the helper itself --------------------------------------------


def test_local_site_routes_and_records(local_site: LocalSite) -> None:
    local_site.route("GET", "/hello", Reply(200, "hi"))
    local_site.route("POST", "/form", lambda seen: Reply.json({"got": seen.form}))
    assert requests.get(local_site.url("/hello?x=1"), timeout=5).text == "hi"
    assert requests.post(local_site.url("/form"), data={"a": "b"}, timeout=5).json() == {"got": {"a": "b"}}
    assert requests.get(local_site.url("/missing"), timeout=5).status_code == 404
    assert [r.path for r in local_site.seen()] == ["/hello", "/form", "/missing"]
    assert local_site.seen("GET", "/hello")[0].query == {"x": ["1"]}


def test_local_site_reply_sequences_repeat_last(local_site: LocalSite) -> None:
    local_site.route("GET", "/seq", [Reply(503, "busy"), Reply(200, "ok")])
    statuses = [requests.get(local_site.url("/seq"), timeout=5).status_code for _ in range(3)]
    assert statuses == [503, 200, 200]
