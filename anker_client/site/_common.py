"""Small helpers shared by the site modules (HTTP response decoding, headers, CSRF)."""

from __future__ import annotations

import email.utils
import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import requests

from anker_client.core.errors import NetworkError, NotFoundError, SiteChangedError
from anker_client.core.tasks import CancelToken

if TYPE_CHECKING:
    from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

JSON_ACCEPT = "application/json, text/plain, */*"


def decode_text(response: requests.Response) -> str:
    """Body as text: the charset from ``Content-Type`` when declared, else UTF-8 (never Latin-1).

    A leading byte-order mark is dropped (a stray BOM in a PHP file is a classic
    way for a server to break ``json.loads`` on every answer).
    """
    content = response.content or b""
    charset = _declared_charset(response.headers.get("Content-Type", ""))
    text: str | None = None
    if charset:
        try:
            text = content.decode(charset, errors="replace")
        except LookupError:
            log.debug("Unknown charset %r, falling back to UTF-8", charset)
    if text is None:
        text = content.decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


def _declared_charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip("\"'")
    return ""


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date). ``None`` when absent/invalid."""
    value = (headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if math.isnan(seconds) or math.isinf(seconds):
        return None
    return max(0.0, seconds)


def is_cloudflare_challenge(status: int, headers: Mapping[str, str], body: str = "") -> bool:
    """A Cloudflare interstitial ("Just a moment…") instead of the site's own answer.

    (``/cdn-cgi/challenge-platform/`` alone is NOT a signal: Cloudflare injects it into normal pages.)
    """
    if (headers.get("cf-mitigated") or "").strip().lower() == "challenge":
        return True
    if status in (403, 429, 503) and body:
        head = body[:8192]
        return "_cf_chl_opt" in head or "cf-chl-" in head or "<title>Just a moment" in head
    return False


def ajax_headers(base_url: str, referer: str = "") -> dict[str, str]:
    """Headers the site's own ``fetch()`` calls send (JSON endpoints)."""
    return {
        "Accept": JSON_ACCEPT,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base_url,
        "Referer": referer or f"{base_url}/",
    }


def fetch_csrf_token(http: HttpClient, base_url: str, *, token: CancelToken | None = None) -> str:
    """``GET /csrf-token`` → ``{"token": …}``.

    Guest pages are served from Cloudflare's cache with ``Set-Cookie`` stripped, so a
    ``<meta name="csrf-token">`` baked into them belongs to some other visitor. This
    endpoint both establishes our session cookies and returns the matching token.
    Returns "" when the endpoint is unavailable (callers then rely on the XSRF cookie).
    """
    try:
        data: Any = http.get_json(f"{base_url}/csrf-token", headers=ajax_headers(base_url), token=token)
    except (NotFoundError, SiteChangedError) as exc:
        log.warning("CSRF endpoint unavailable: %s", exc.detail or exc)
        return ""
    except NetworkError as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            log.warning("CSRF endpoint answered HTTP %s", exc.status)
            return ""
        raise
    value = data.get("token") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def csrf_headers(http: HttpClient, csrf_token: str) -> dict[str, str]:
    """``X-CSRF-TOKEN`` plus ``X-XSRF-TOKEN`` (Laravel accepts either; X-CSRF wins when both are valid)."""
    headers: dict[str, str] = {}
    if csrf_token:
        headers["X-CSRF-TOKEN"] = csrf_token
    xsrf = http.cookie("XSRF-TOKEN")
    if xsrf:
        headers["X-XSRF-TOKEN"] = unquote(xsrf)
    return headers
