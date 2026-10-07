"""HTTP layer shared by every network caller (site client, downloader, image cache, updates).

Responsibilities
* One canonical cookie jar + header set. Requests run on pooled worker
  ``requests.Session`` objects cloned from it (``requests.Session`` is not
  thread-safe, so a worker session is only ever used by one request at a time).
  Before a request the worker's jar is refreshed from the canonical jar when
  that changed; afterwards every cookie the worker learned (or deleted) is
  merged back, so a cookie set on one thread is visible to all others.
* Sensible defaults: browser-like headers (``Accept``, ``Accept-Language``),
  ``Accept-Encoding: gzip, deflate`` (NOT br — large chunked brotli responses
  were observed to corrupt), connect/read timeouts ``(10, 30)``.
* Automatic retry with exponential backoff + jitter for idempotent requests
  (GET/HEAD) on connection errors, timeouts and 429/500/502/503/504 (max 3
  attempts; honour ``Retry-After`` up to ``max_retry_after`` (10 s) — a longer
  demand is returned/raised at once so callers can surface it, e.g. as
  ``RateLimitedError``, instead of silently blocking). Never retry POST automatically.
* Polite pacing for the HTML site: at most ~4 requests/second to
  ``ankergames.net`` and its sub-domains (token bucket shared across threads,
  burst = rate). Static assets on those hosts (``/uploads/``, ``/static/``,
  ``/build/``, ``/storage/`` — covers and screenshots, served from Cloudflare's
  cache) have their own bucket (16/s by default) so a 56-cover grid does not take
  14 s to fill. File downloads from CDN hosts are NOT paced. The paced host set
  is configurable (``paced_hosts``; ``None`` = the site, an empty set disables
  pacing) for tests.
* Error mapping at this boundary: ``requests`` exceptions →
  :class:`~anker_client.core.errors.NetworkError` (``status`` set when an
  HTTP status is known); 404 → ``NotFoundError`` when ``raise_for_status``.
* Cookie persistence: ``export_cookies()``/``import_cookies()`` as plain dicts
  (the auth service encrypts them at rest). ``clear_cookies()`` for logout.
* ``user_agent`` can be replaced at runtime (the UI sets it to the embedded
  Chromium UA so both browsers look identical to the site).
* All methods accept ``token: CancelToken | None``. With a token the blocking
  network call runs on a short-lived helper thread so cancellation is
  immediate: ``OperationCancelled`` is raised at once and the abandoned
  response is closed as soon as the helper returns. A token that fires before
  the helper starts guarantees the request is never sent. Without a token the
  call runs inline (and cannot be interrupted).
* After :meth:`close` every new request raises ``OperationCancelled`` (the app
  is shutting down).
"""

from __future__ import annotations

import copy
import json as jsonlib
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar, create_cookie

from anker_client.constants import DEFAULT_USER_AGENT, SITE_HOST
from anker_client.core.errors import (
    AnkerError,
    NetworkError,
    NotFoundError,
    OperationCancelled,
    SiteChangedError,
)
from anker_client.core.tasks import CancelToken
from anker_client.site._common import decode_text, is_cloudflare_challenge, retry_after_seconds

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 30.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_PACED_HOSTS = frozenset({SITE_HOST})
STATIC_PATH_PREFIXES = ("/uploads/", "/static/", "/build/", "/storage/", "/favicon")

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
# Transient transport failures worth another attempt (SSL/certificate errors are not).
_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_IDLE_SESSIONS_MAX = 16

CookieKey = tuple[str, str, str]  # (domain, path, name)
CookieSig = tuple[str | None, int | None, bool]  # (value, expires, secure)


def _cookie_key(cookie: Cookie) -> CookieKey:
    return (cookie.domain, cookie.path, cookie.name)


def _cookie_sig(cookie: Cookie) -> CookieSig:
    return (cookie.value, cookie.expires, cookie.secure)


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class _TokenBucket:
    """Thread-safe request pacing. ``reserve()`` books a slot and returns the wait before using it."""

    def __init__(self, rate: float, *, clock: Callable[[], float]) -> None:
        self._rate = rate
        self._capacity = max(1.0, rate)
        self._tokens = self._capacity
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def reserve(self) -> float:
        if self._rate <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            # Tokens may go negative: later callers queue up behind earlier reservations.
            self._tokens -= 1.0
            return 0.0 if self._tokens >= 0 else -self._tokens / self._rate


@dataclass(eq=False)
class _Worker:
    """A pooled ``requests.Session`` plus what it last synchronised with the canonical state."""

    session: requests.Session
    header_gen: int = -1
    cookie_gen: int = -1
    snapshot: dict[CookieKey, CookieSig] = field(default_factory=dict)


@dataclass(eq=False)
class _Outcome:
    """Hand-over between a caller and the helper thread running its request."""

    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    response: requests.Response | None = None
    error: BaseException | None = None
    finished: bool = False
    abandoned: bool = False


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        site_rate_per_second: float = 4.0,
        paced_hosts: Iterable[str] | None = None,
        asset_rate_per_second: float = 16.0,
        max_attempts: int = 3,
        backoff_base: float = 0.75,
        max_retry_after: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._header_gen = 0
        self._jar = RequestsCookieJar()
        self._cookie_gen = 0
        self._idle: list[_Worker] = []
        self._closed = False
        # ``None`` → the site; an explicit empty set disables pacing altogether.
        hosts = DEFAULT_PACED_HOSTS if paced_hosts is None else paced_hosts
        self._paced_hosts = frozenset(h.lower().strip().lstrip(".") for h in hosts if h and h.strip())
        self._bucket = _TokenBucket(site_rate_per_second, clock=clock)
        self._asset_bucket = _TokenBucket(asset_rate_per_second, clock=clock)
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = max(0.0, backoff_base)
        self._max_retry_after = max(0.0, max_retry_after)

    # --- configuration ----------------------------------------------------------
    @property
    def user_agent(self) -> str:
        with self._lock:
            return self._user_agent

    def set_user_agent(self, user_agent: str) -> None:
        user_agent = (user_agent or "").strip()
        if not user_agent:
            return
        with self._lock:
            if user_agent != self._user_agent:
                self._user_agent = user_agent
                self._header_gen += 1

    def _is_paced(self, url: str) -> bool:
        """Whether requests to ``url`` go through the site's polite pacing."""
        host = _host_of(url)
        return any(host == h or host.endswith("." + h) for h in self._paced_hosts)

    # --- requests ---------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        retry: bool = True,
        raise_for_status: bool = True,
        token: CancelToken | None = None,
    ) -> requests.Response:
        """GET with retries/pacing/error mapping. With ``stream=True`` the caller must close the response."""
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
            retry=retry,
            raise_for_status=raise_for_status,
            token=token,
        )

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        """HEAD (follows redirects by default, unlike ``requests.head``)."""
        kwargs.setdefault("allow_redirects", True)
        return self._request("HEAD", url, **kwargs)

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        allow_redirects: bool = True,
        raise_for_status: bool = True,
        token: CancelToken | None = None,
    ) -> requests.Response:
        """POST (never retried automatically)."""
        return self._request(
            "POST",
            url,
            data=data,
            json=json,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            retry=False,
            raise_for_status=raise_for_status,
            token=token,
        )

    def get_text(self, url: str, **kwargs: Any) -> str:
        """GET and return decoded text (UTF-8 fallback)."""
        kwargs["stream"] = False
        response = self.get(url, **kwargs)
        try:
            return decode_text(response)
        finally:
            response.close()

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET and decode JSON; malformed JSON → ``SiteChangedError``."""
        headers = {"Accept": "application/json", **dict(kwargs.pop("headers", None) or {})}
        text = self.get_text(url, headers=headers, **kwargs)
        try:
            return jsonlib.loads(text)
        except ValueError as exc:
            raise SiteChangedError(detail=f"Invalid JSON from {url}: {exc}; body starts {text[:120]!r}") from exc

    # --- cookies ----------------------------------------------------------------
    def cookie(self, name: str, domain: str | None = None) -> str | None:
        wanted = (domain or "").lower().lstrip(".")
        with self._lock:
            for item in self._jar:
                if item.name != name:
                    continue
                have = item.domain.lower().lstrip(".")
                if not wanted or have == wanted or wanted.endswith("." + have) or have.endswith("." + wanted):
                    return item.value
        return None

    def export_cookies(self) -> list[dict[str, Any]]:
        """``[{name, value, domain, path, secure, expires}]`` for every cookie in the canonical jar."""
        with self._lock:
            return [
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "secure": bool(c.secure),
                    "expires": c.expires,
                }
                for c in self._jar
            ]

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Replace/merge cookies (same dict shape as ``export_cookies``). Bumps the session version.

        Entries without a name or domain and already-expired cookies are skipped.
        """
        now = time.time()
        parsed: list[Cookie] = []
        for entry in cookies or []:
            item = self._cookie_from_dict(entry, now)
            if item is not None:
                parsed.append(item)
        with self._lock:
            for item in parsed:
                self._jar.set_cookie(item)
            self._cookie_gen += 1
        log.debug("Imported %d cookie(s)", len(parsed))

    @staticmethod
    def _cookie_from_dict(entry: Mapping[str, Any], now: float) -> Cookie | None:
        name = str(entry.get("name") or "").strip()
        domain = str(entry.get("domain") or "").strip()
        # A cookie without a domain would be sent to every host (CDNs included) — never import one.
        if not name or not domain:
            return None
        expires_raw = entry.get("expires")
        expires: int | None
        try:
            expires = int(float(expires_raw)) if expires_raw not in (None, "", 0) else None
        except (TypeError, ValueError):
            expires = None
        if expires is not None and expires <= now:
            return None
        return create_cookie(
            name,
            "" if entry.get("value") is None else str(entry.get("value")),
            domain=domain,
            path=str(entry.get("path") or "/"),
            secure=bool(entry.get("secure")),
            expires=expires,
        )

    def clear_cookies(self) -> None:
        with self._lock:
            self._jar.clear()
            self._cookie_gen += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for worker in idle:
            worker.session.close()

    # --- internals: request pipeline -------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        retry: bool = True,
        raise_for_status: bool = True,
        token: CancelToken | None = None,
    ) -> requests.Response:
        method = method.upper()
        attempts = self._max_attempts if retry and method in IDEMPOTENT_METHODS else 1
        kwargs: dict[str, Any] = {
            "params": dict(params) if params else None,
            "data": data,
            "json": json,
            "headers": dict(headers) if headers else None,
            "timeout": timeout if timeout is not None else DEFAULT_TIMEOUT,
            "stream": stream,
            "allow_redirects": allow_redirects,
        }
        for attempt in range(1, attempts + 1):
            self._check_usable(token)
            self._pace(url, token)
            started = time.monotonic()
            try:
                response = self._send(method, url, kwargs, token)
            except AnkerError:
                raise
            except requests.RequestException as exc:
                if attempt < attempts and isinstance(exc, _TRANSIENT_EXCEPTIONS) and not _is_ssl_error(exc):
                    delay = self._backoff(attempt)
                    log.debug("%s %s failed (%s); retry %d in %.2fs", method, url, type(exc).__name__, attempt, delay)
                    self._sleep(delay, token)
                    continue
                raise self._map_exception(exc, url) from exc
            elapsed_ms = (time.monotonic() - started) * 1000
            log.debug("%s %s -> %s (%.0f ms)", method, url, response.status_code, elapsed_ms)

            if response.status_code in RETRY_STATUSES and attempt < attempts:
                delay = self._retry_delay(response, attempt)
                if delay is not None:
                    response.close()
                    log.debug("%s %s answered %s; retry %d in %.2fs", method, url, response.status_code, attempt, delay)
                    self._sleep(delay, token)
                    continue
            if raise_for_status and response.status_code >= 400:
                error = self._status_error(response)
                response.close()
                raise error
            return response
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises

    def _check_usable(self, token: CancelToken | None) -> None:
        if token is not None:
            token.raise_if_cancelled()
        with self._lock:
            if self._closed:
                raise OperationCancelled(detail="HTTP client is closed")

    def _pace(self, url: str, token: CancelToken | None) -> None:
        if not self._is_paced(url):
            return
        bucket = self._asset_bucket if _is_static_asset(url) else self._bucket
        delay = bucket.reserve()
        if delay > 0:
            self._sleep(delay, token)

    @staticmethod
    def _sleep(seconds: float, token: CancelToken | None) -> None:
        if seconds <= 0:
            return
        if token is None:
            time.sleep(seconds)  # no token: the caller opted out of cancellation (never the GUI thread)
        else:
            token.sleep(seconds)

    def _backoff(self, attempt: int) -> float:
        base = self._backoff_base * (2 ** (attempt - 1))
        return base + random.uniform(0, base / 2) if base > 0 else 0.0

    def _retry_delay(self, response: requests.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or ``None`` when the server asks for longer than we accept."""
        requested = retry_after_seconds(response.headers)
        if requested is None:
            return self._backoff(attempt)
        if requested > self._max_retry_after:
            return None
        return requested

    # --- internals: sessions ----------------------------------------------------
    def _send(
        self, method: str, url: str, kwargs: dict[str, Any], token: CancelToken | None
    ) -> requests.Response:
        if token is None:
            return self._send_inline(method, url, kwargs)
        return self._send_cancellable(method, url, kwargs, token)

    def _send_cancellable(
        self, method: str, url: str, kwargs: dict[str, Any], token: CancelToken
    ) -> requests.Response:
        outcome = _Outcome()

        def run() -> None:
            response: requests.Response | None = None
            error: BaseException | None = None
            try:
                response = self._send_inline(method, url, kwargs)
            except BaseException as exc:
                error = exc
            with outcome.lock:
                outcome.finished = True
                if outcome.abandoned:
                    if response is not None:
                        response.close()
                    return
                outcome.response, outcome.error = response, error
            outcome.done.set()

        unregister = token.on_cancel(outcome.done.set)
        try:
            # Re-check after registering: a token cancelled since ``_check_usable``/``_pace``
            # must never let the request (e.g. a quota-consuming POST) leave this machine.
            if token.cancelled:
                raise OperationCancelled()
            threading.Thread(target=run, name="anker-http", daemon=True).start()
            outcome.done.wait()
        finally:
            unregister()
        with outcome.lock:
            if not outcome.finished:
                outcome.abandoned = True
                raise OperationCancelled()
        if token.cancelled:
            if outcome.response is not None:
                outcome.response.close()
            raise OperationCancelled()
        if outcome.error is not None:
            raise outcome.error
        assert outcome.response is not None
        return outcome.response

    def _send_inline(self, method: str, url: str, kwargs: dict[str, Any]) -> requests.Response:
        worker = self._checkout()
        try:
            return worker.session.request(method, url, **kwargs)
        finally:
            self._sync_out(worker)
            self._checkin(worker)

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _checkout(self) -> _Worker:
        with self._lock:
            worker = self._idle.pop() if self._idle else None
            header_gen = self._header_gen
            user_agent = self._user_agent
        if worker is None:
            worker = _Worker(self._new_session())
        if worker.header_gen != header_gen:
            worker.session.headers.clear()
            worker.session.headers.update(_BASE_HEADERS)
            worker.session.headers["User-Agent"] = user_agent
            worker.header_gen = header_gen
        self._sync_in(worker)
        return worker

    def _checkin(self, worker: _Worker) -> None:
        with self._lock:
            if not self._closed and len(self._idle) < _IDLE_SESSIONS_MAX:
                self._idle.append(worker)
                return
        worker.session.close()

    def _sync_in(self, worker: _Worker) -> None:
        """Copy the canonical jar into the worker's jar when it changed since the last sync."""
        with self._lock:
            if worker.cookie_gen == self._cookie_gen:
                return
            cookies = [copy.copy(c) for c in self._jar]
            generation = self._cookie_gen
        jar = worker.session.cookies
        jar.clear()
        for item in cookies:
            jar.set_cookie(item)
        worker.cookie_gen = generation
        worker.snapshot = {_cookie_key(c): _cookie_sig(c) for c in cookies}

    def _sync_out(self, worker: _Worker) -> None:
        """Merge cookies the worker learned (or the server deleted) back into the canonical jar."""
        after = {_cookie_key(c): c for c in list(worker.session.cookies)}
        with self._lock:
            stale = worker.cookie_gen != self._cookie_gen
            changed = False
            for key, item in after.items():
                if worker.snapshot.get(key) != _cookie_sig(item):
                    self._jar.set_cookie(copy.copy(item))
                    changed = True
            for key in worker.snapshot.keys() - after.keys():
                try:
                    self._jar.clear(*key)
                    changed = True
                except KeyError:
                    pass
            if changed:
                self._cookie_gen += 1
            # If someone else changed the canonical jar meanwhile, force a full re-sync next time.
            worker.cookie_gen = -1 if stale else self._cookie_gen
            worker.snapshot = {key: _cookie_sig(item) for key, item in after.items()}

    # --- internals: error mapping ----------------------------------------------
    def _site_label(self, url: str) -> str:
        return "AnkerGames" if self._is_paced(url) else (_host_of(url) or "the server")

    def _map_exception(self, exc: requests.RequestException, url: str) -> NetworkError:
        label = self._site_label(url)
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, requests.exceptions.Timeout):
            return NetworkError(f"{label} did not respond in time. Check your connection and try again.", detail=detail)
        if isinstance(exc, requests.exceptions.SSLError):
            return NetworkError(f"A secure connection to {label} could not be established.", detail=detail)
        if isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.ChunkedEncodingError):
            if label == "AnkerGames":
                return NetworkError(detail=detail)
            return NetworkError(f"Could not reach {label}. Check your internet connection.", detail=detail)
        if isinstance(exc, requests.exceptions.TooManyRedirects):
            return NetworkError(f"{label} redirected too many times.", detail=detail)
        return NetworkError(f"The request to {label} failed.", detail=detail)

    def _status_error(self, response: requests.Response) -> AnkerError:
        status = response.status_code
        url = response.url or ""
        label = self._site_label(url)
        detail = f"HTTP {status} for {url}"
        if is_cloudflare_challenge(status, response.headers):
            return NetworkError(
                f"{label} asked for a browser check (Cloudflare). Try again in a few minutes.",
                status=status,
                detail=detail,
            )
        if status == 404:
            return NotFoundError(detail=detail)
        if status == 429:
            return NetworkError(
                f"{label} is receiving too many requests. Try again in a minute.", status=status, detail=detail
            )
        if status >= 500:
            return NetworkError(
                f"{label} is having problems right now (HTTP {status}). Try again later.", status=status, detail=detail
            )
        if status in (401, 403):
            return NetworkError(f"{label} denied access (HTTP {status}).", status=status, detail=detail)
        return NetworkError(f"Unexpected response from {label} (HTTP {status}).", status=status, detail=detail)


def _is_static_asset(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return path.startswith(STATIC_PATH_PREFIXES)


def _is_ssl_error(exc: BaseException) -> bool:
    return isinstance(exc, requests.exceptions.SSLError)
