"""Test helpers for the download engine (no tests in here).

* :class:`FileServer` — ``ThreadingHTTPServer`` on 127.0.0.1 serving generated
  bytes at ``/file.bin`` with full ``Range``/``If-Range``/``ETag`` support and
  switchable faults (forced statuses, mid-stream disconnects, ignored ranges,
  no Content-Length, throttling, stalls, HTML pages).
* :class:`SessionHttp` — stand-in for ``site.http.HttpClient.get`` backed by
  real per-thread ``requests.Session`` objects.
* :func:`make_tls_context` — a throw-away self-signed certificate for
  127.0.0.1 (needs ``cryptography``) so the abort path can be tested over TLS.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests

from anker_client.core.errors import OperationCancelled
from anker_client.core.models import ResolvedLink
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.engine import DownloadProgress, HttpDownloader
from anker_client.services.downloads.ratelimit import RateLimiter

KIB = 1024
MIB = 1024 * 1024
FAST_RETRIES = (0.01, 0.02, 0.04, 0.08, 0.16)

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def random_bytes(size: int, seed: int = 1) -> bytes:
    """Deterministic pseudo-random bytes (no repeating pattern, so misplaced writes are caught)."""
    return random.Random(seed).randbytes(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class RequestRecord:
    range: str | None
    if_range: str | None
    accept_encoding: str | None
    status: int = 0
    start: int | None = None
    started_at: float = field(default_factory=time.monotonic)


class FileServer:
    """A tiny CDN look-alike. Mutate the public attributes between/while downloads (under ``lock``)."""

    def __init__(
        self, data: bytes, *, etag: str = '"v1"', name: str = "file.bin", tls: ssl.SSLContext | None = None
    ) -> None:
        self.lock = threading.Lock()
        self.data = data
        self.etag = etag
        self.last_modified = "Tue, 06 Oct 2026 10:00:00 GMT"
        self.name = name
        self.ranges = True  # honour Range
        self.advertise_ranges = True  # send Accept-Ranges: bytes
        self.content_length = True
        self.content_type = "application/octet-stream"
        self.status_queue: list[int] = []  # forced statuses for the next requests
        self.status_all: int | None = None  # forced status for every request
        self.retry_after: str | None = None
        self.disconnect_queue: list[int] = []  # next responses: send N body bytes then drop the connection
        self.throttle_bps: float | None = None  # per-response send rate
        self.slow_first_bps: float | None = None  # rate for the first request that starts at offset 0
        self.stall = threading.Event()  # set → responses stall after their first block until released
        self.release = threading.Event()
        self.block_size = 16 * KIB
        self.max_range: int | None = None  # cap every 206 body at this many bytes (short ranges)
        self.records: list[RequestRecord] = []
        self.active = 0
        self.max_active = 0
        self._slow_used = False

        handler = type("Handler", (_Handler,), {"state": self})
        self._tls = tls
        server_class = _QuietServer if tls is None else type("TlsServer", (_TlsServer,), {"tls": tls})
        self._server = server_class(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="test-file-server", daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        scheme = "http" if self._tls is None else "https"
        return f"{scheme}://{host}:{port}/{self.name}"

    def link(self, **overrides: Any) -> ResolvedLink:
        values: dict[str, Any] = {
            "url": self.url,
            "filename": self.name,
            "size": len(self.data),
            "etag": self.etag,
            "last_modified": self.last_modified,
            "accept_ranges": True,
            "content_type": self.content_type,
        }
        values.update(overrides)
        return ResolvedLink(**values)

    def ranged_starts(self) -> list[int]:
        with self.lock:
            return [r.start for r in self.records if r.start is not None]

    def reset_records(self) -> None:
        with self.lock:
            self.records.clear()
            self.max_active = self.active

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request: Any, client_address: Any) -> None:  # client aborts are expected
        pass


class _TlsServer(_QuietServer):
    tls: ssl.SSLContext

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, address = self.socket.accept()
        sock.settimeout(10)  # a client that never finishes the handshake must not block the accept loop
        try:
            return self.tls.wrap_socket(sock, server_side=True), address
        except (OSError, ssl.SSLError):
            sock.close()
            raise


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FileServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
        pass

    def do_GET(self) -> None:
        st = self.state
        with st.lock:
            st.active += 1
            st.max_active = max(st.max_active, st.active)
        try:
            self._serve(st)
        except (ConnectionError, OSError):
            self.close_connection = True
        finally:
            with st.lock:
                st.active -= 1

    def _serve(self, st: FileServer) -> None:
        record = RequestRecord(
            range=self.headers.get("Range"),
            if_range=self.headers.get("If-Range"),
            accept_encoding=self.headers.get("Accept-Encoding"),
        )
        with st.lock:
            st.records.append(record)
            forced = st.status_queue.pop(0) if st.status_queue else st.status_all
            disconnect_after = st.disconnect_queue.pop(0) if st.disconnect_queue else None
            data, etag, last_modified = st.data, st.etag, st.last_modified
            honour_ranges, content_type = st.ranges, st.content_type
            throttle = st.throttle_bps

        if forced is not None:
            record.status = forced
            self.send_response(forced)
            if st.retry_after:
                self.send_header("Retry-After", st.retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        size = len(data)
        start, end = 0, size - 1
        status = 200
        match = _RANGE_RE.match(record.range or "")
        if match and honour_ranges:
            validator = record.if_range
            if validator is None or validator in (etag, last_modified):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                if start >= size:
                    record.status = 416
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
                record.start = start
                if st.max_range:
                    end = min(end, start + st.max_range - 1)
        record.status = status

        with st.lock:
            if st.slow_first_bps and start == 0 and not st._slow_used:
                st._slow_used = True
                throttle = st.slow_first_bps

        body = memoryview(data)[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        if st.advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{st.name}"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if st.content_length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self._send_body(st, body, throttle, disconnect_after)

    def _send_body(self, st: FileServer, body: memoryview, throttle: float | None, disconnect_after: int | None) -> None:
        sent = 0
        block = st.block_size
        first = True
        while sent < len(body):
            limit = len(body) if disconnect_after is None else min(len(body), disconnect_after)
            if sent >= limit:
                # abrupt mid-stream disconnect
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            piece = body[sent : min(sent + block, limit)]
            self.wfile.write(piece)
            sent += len(piece)
            if first and st.stall.is_set():
                self.wfile.flush()
                st.release.wait(15)
            first = False
            if throttle:
                time.sleep(len(piece) / throttle)
        self.wfile.flush()


class SessionHttp:
    """``HttpClient.get`` look-alike for the engine, backed by per-thread ``requests.Session``s."""

    def __init__(self, *, verify: str | bool = True) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[requests.Session] = []
        self._verify = verify
        self.calls = 0

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False  # never route 127.0.0.1 through a proxy
            session.verify = self._verify
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: Any = None,
        stream: bool = False,
        allow_redirects: bool = True,
        retry: bool = True,
        raise_for_status: bool = True,
        token: Any = None,
    ) -> requests.Response:
        assert stream is True and retry is False and raise_for_status is False
        with self._lock:
            self.calls += 1
        return self._session().get(
            url, params=params, headers=headers, timeout=timeout, stream=stream, allow_redirects=allow_redirects
        )

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()


def make_tls_context(directory: Path) -> tuple[ssl.SSLContext, str]:
    """Server context with a fresh self-signed certificate for 127.0.0.1, plus the CA file for clients."""
    import datetime
    import ipaddress

    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "test-cert.pem", directory / "test-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context, str(cert_file)


# --- downloader helpers ---------------------------------------------------------------


def make_downloader(http: Any, limiter: RateLimiter | None = None, **kwargs: Any) -> HttpDownloader:
    options: dict[str, Any] = {
        "connections": 4,
        "min_segment_size": 256 * KIB,
        "chunk_size": 16 * KIB,
        "retry_delays": FAST_RETRIES,
        "stop_timeout": 2.0,
    }
    options.update(kwargs)
    return HttpDownloader(http, limiter or RateLimiter(0), **options)


def make_partial(downloader: HttpDownloader, link: Any, dest: Path, *, at_least: int) -> dict:
    """Start a (throttled) download and pause it once ``at_least`` bytes are reported."""
    token = CancelToken()

    def on_progress(progress: DownloadProgress) -> None:
        if progress.bytes_done >= at_least:
            token.cancel("pause")

    with pytest.raises(OperationCancelled):
        downloader.download(link, str(dest), token=token, on_progress=on_progress)
    return json.loads(Path(f"{dest}.part.json").read_text(encoding="utf-8"))


def part_files(dest: Path) -> tuple[Path, Path]:
    return Path(f"{dest}.part"), Path(f"{dest}.part.json")
