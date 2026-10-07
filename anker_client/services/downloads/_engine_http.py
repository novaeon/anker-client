"""HTTP details of the download engine: header parsing, validators, aborting responses.

Also defines the engine's internal control-flow exceptions; they never leave
``HttpDownloader.download`` (which converts them to ``core.errors`` types).
"""

from __future__ import annotations

import email.utils
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: Statuses meaning "this signed URL is no longer valid" → ``LinkExpiredError``.
EXPIRED_STATUSES = frozenset({401, 403, 404, 410})

_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/\s*(\d+|\*)\s*$", re.IGNORECASE)
_UNSATISFIED_RANGE_RE = re.compile(r"^\s*bytes\s+\*\s*/\s*(\d+)\s*$", re.IGNORECASE)
_MAX_RETRY_AFTER = 120.0


# --- internal control flow --------------------------------------------------------------


class TransientError(Exception):
    """A connection-level failure worth retrying (reset, timeout, short read, 5xx, 429)."""

    def __init__(self, message: str, *, retry_after: float = 0.0, status: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class RestartRequired(Exception):
    """The transfer cannot continue with its current plan and must start again from byte 0."""

    def __init__(self, reason: str, *, remote: RemoteInfo | None, single_connection: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remote = remote
        self.single_connection = single_connection


# --- response metadata ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContentRange:
    start: int
    end: int  # inclusive
    total: int | None


def parse_content_range(value: str | None) -> ContentRange | None:
    if not value:
        return None
    match = _CONTENT_RANGE_RE.match(value)
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return ContentRange(start, end, total)


@dataclass(frozen=True, slots=True)
class RemoteInfo:
    """What a response says about the file behind the URL."""

    size: int | None
    etag: str = ""
    last_modified: str = ""
    accept_ranges: bool = False
    content_type: str = ""

    @classmethod
    def from_response(cls, resp: Any) -> RemoteInfo:
        headers = resp.headers
        status = resp.status_code
        size: int | None = None
        accept_ranges = "bytes" in headers.get("Accept-Ranges", "").lower()
        if status == 206:
            content_range = parse_content_range(headers.get("Content-Range"))
            size = content_range.total if content_range else None
            accept_ranges = True
        elif status == 416:
            match = _UNSATISFIED_RANGE_RE.match(headers.get("Content-Range", ""))
            size = int(match.group(1)) if match else None
        elif status == 200 and not is_encoded(resp):
            size = parse_int(headers.get("Content-Length"))
        return cls(
            size=size,
            etag=headers.get("ETag", "").strip(),
            last_modified=headers.get("Last-Modified", "").strip(),
            accept_ranges=accept_ranges,
            content_type=headers.get("Content-Type", "").strip(),
        )


def parse_int(value: str | None) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def is_encoded(resp: Any) -> bool:
    """True when the body is content-encoded (byte offsets would not match the file)."""
    return resp.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity")


def retry_after_seconds(resp: Any) -> float:
    value = resp.headers.get("Retry-After", "")
    if not value:
        return 0.0
    seconds = parse_int(value)
    if seconds is None:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0.0
        seconds = int(parsed.timestamp() - time.time())
    return float(max(0, min(seconds, int(_MAX_RETRY_AFTER))))


# --- validators -------------------------------------------------------------------------


def normalize_etag(etag: str) -> str:
    value = etag.strip()
    if value[:2].upper() == "W/":
        value = value[2:]
    return value.strip().strip('"')


def is_weak_etag(etag: str) -> bool:
    return etag.strip()[:2].upper() == "W/"


def same_etag(a: str, b: str) -> bool:
    """Lenient comparison (ignores weak markers and quoting differences)."""
    return normalize_etag(a) == normalize_etag(b)


def same_last_modified(a: str, b: str) -> bool:
    """Whether two ``Last-Modified`` values name the same instant (text first, then parsed dates)."""
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    try:
        return email.utils.parsedate_to_datetime(a) == email.utils.parsedate_to_datetime(b)
    except (TypeError, ValueError):
        return False


def validator_conflict(saved_etag: str, saved_modified: str, new_etag: str, new_modified: str) -> str:
    """Why saved and new validators describe different files ("" when they may be the same file).

    A matching *strong* ETag is authoritative. Otherwise (weak or missing on
    either side) Last-Modified is compared too when both sides have it.
    """
    if saved_etag and new_etag:
        if not same_etag(saved_etag, new_etag):
            return "ETag"
        if not (is_weak_etag(saved_etag) or is_weak_etag(new_etag)):
            return ""
    if saved_modified and new_modified and not same_last_modified(saved_modified, new_modified):
        return "Last-Modified"
    return ""


def if_range_value(etag: str, last_modified: str) -> str:
    """Validator for ``If-Range``: a strong ETag, else Last-Modified, else ""."""
    value = etag.strip()
    if value and value[:2].upper() != "W/":
        return value if value.startswith('"') else f'"{value}"'
    return last_modified.strip()


# --- misc -------------------------------------------------------------------------------


def redact_url(url: str) -> str:
    """``https://host/.../file.rar`` — never log signed query strings."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    name = parts.path.rsplit("/", 1)[-1]
    return f"{parts.scheme}://{parts.netloc}/.../{name}" if name else f"{parts.scheme}://{parts.netloc}/"


def close_quietly(resp: Any) -> None:
    try:
        resp.close()
    except Exception as exc:  # closing a half-aborted response may raise anything
        log.debug("Closing response failed: %r", exc)


def abort_response(resp: Any) -> None:
    """Interrupt a read that another thread may be blocked in, then close ``resp``.

    On Windows neither ``shutdown()`` nor ``Response.close()`` wakes a thread
    blocked in a socket read (``makefile`` references keep the handle alive),
    so the read would only end at the read timeout. Detaching the handle from
    the Python socket object and closing it makes the blocked read fail at
    once; detaching first guarantees the handle is never closed twice.
    """
    sock = _find_socket(resp)
    if sock is not None:
        try:
            handle = socket.socket.detach(sock)
        except (OSError, ValueError):
            handle = -1
        if handle is not None and handle >= 0:
            try:
                socket.close(handle)
            except OSError:
                pass
    close_quietly(resp)


def _find_socket(resp: Any) -> socket.socket | None:
    raw = getattr(resp, "raw", None)
    candidates = (
        getattr(getattr(raw, "connection", None), "sock", None),
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None),
    )
    for candidate in candidates:
        if isinstance(candidate, socket.socket):
            return candidate
    return None
