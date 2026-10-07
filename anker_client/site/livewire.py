"""Minimal Livewire v3/v4 component client (optional fast paths).

The site renders Livewire components whose snapshots appear in
``wire:snapshot="…"`` attributes; the per-deploy update endpoint is announced in
``window.livewireScriptConfig = {"csrf": …, "uri": "https://ankergames.net/livewire-<hash>/update", …}``.
An update call is ``POST <uri>`` with JSON
``{"_token": csrf, "components": [{"snapshot": "<raw snapshot json>", "updates": {...}, "calls": [...]}]}``
and headers ``X-Livewire: 1``, ``Content-Type: application/json``,
``X-CSRF-TOKEN``. The response is ``{"components": [{"snapshot": "…", "effects": {"html": "…"}}]}``.

Pages are fetched at most once per ``page_ttl`` (10 min) and their snapshots
reused unchanged (the components we call are stateless lookups). The CSRF token
comes from ``GET /csrf-token`` — the one in ``livewireScriptConfig`` is stale on
Cloudflare-cached guest pages. A 404/419/500 answer (new deploy → new update URI,
expired session, stale snapshot checksum) reloads the page and token and
retries once.

Used for ``quick_search`` (component ``search-component`` on ``/games``,
property ``q``, minimum 2 characters) which returns up to ~5 best matches with a
rendered HTML fragment of result links. Everything here fails soft: any
protocol mismatch raises ``SiteChangedError`` so callers fall back to the plain
HTML endpoints (network problems still raise ``NetworkError``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anker_client.constants import BASE_URL
from anker_client.core.errors import NetworkError, NotFoundError, RateLimitedError, SiteChangedError
from anker_client.core.models import GameSummary
from anker_client.core.tasks import CancelToken
from anker_client.site import parsers
from anker_client.site._common import (
    ajax_headers,
    csrf_headers,
    decode_text,
    fetch_csrf_token,
    retry_after_seconds,
)
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

_RETRY_ONCE_STATUSES = frozenset({404, 419, 500})
_MIN_QUERY_LENGTH = 2


@dataclass(frozen=True, slots=True)
class _PageState:
    fetched_at: float
    update_uri: str
    snapshots: dict[str, str]


class LivewireClient:
    def __init__(
        self,
        http: HttpClient,
        base_url: str = BASE_URL,
        *,
        page_ttl: float = 600.0,
        search_page_path: str = "/games",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._page_ttl = page_ttl
        self._search_page = f"{self._base}{search_page_path}"
        self._clock = clock
        self._lock = threading.Lock()
        self._pages: dict[str, _PageState] = {}
        self._csrf = ""

    def call(
        self,
        page_url: str,
        component: str,
        *,
        updates: dict[str, Any] | None = None,
        calls: list[dict[str, Any]] | None = None,
        token: CancelToken | None = None,
    ) -> dict[str, Any]:
        """Load ``page_url`` (cached ≤10 min), find ``component``'s snapshot, POST an update,
        return the component's response dict (``snapshot`` parsed to a dict + ``effects``)."""
        status, text = 0, ""
        for attempt in range(2):
            refresh = attempt > 0
            state = self._page_state(page_url, token=token, refresh=refresh)
            snapshot = state.snapshots.get(component)
            if snapshot is None:
                raise SiteChangedError(detail=f"Livewire component {component!r} not found on {page_url}")
            csrf = self._csrf_token(token=token, refresh=refresh)
            payload = {
                "_token": csrf,
                "components": [{"snapshot": snapshot, "updates": updates or {}, "calls": calls or []}],
            }
            headers = {
                **ajax_headers(self._base, page_url),
                "X-Livewire": "1",
                "Content-Type": "application/json",
                **csrf_headers(self._http, csrf),
            }
            response = self._http.post(
                state.update_uri,
                json=payload,
                headers=headers,
                allow_redirects=False,
                raise_for_status=False,
                token=token,
            )
            try:
                status = response.status_code
                text = decode_text(response)
                retry_after = retry_after_seconds(response.headers)
            finally:
                response.close()
            if status in _RETRY_ONCE_STATUSES and attempt == 0:
                log.debug("Livewire update answered %s; reloading %s and retrying once", status, page_url)
                continue
            break
        if status == 429:
            raise RateLimitedError(int(retry_after or 30))
        if status >= 500:
            raise NetworkError(f"AnkerGames search is unavailable right now (HTTP {status}).", status=status)
        if status != 200:
            raise SiteChangedError(detail=f"Livewire update for {component!r} answered HTTP {status}")
        return _component_result(text, component)

    def quick_search(self, query: str, *, token: CancelToken | None = None) -> list[GameSummary]:
        text = " ".join((query or "").split())
        if len(text) < _MIN_QUERY_LENGTH:
            return []
        result = self.call(self._search_page, "search-component", updates={"q": text}, token=token)
        html = result["effects"].get("html")
        if not isinstance(html, str):
            raise SiteChangedError(detail="search-component returned no HTML")
        return parsers.parse_cards(html)

    # --- internals ------------------------------------------------------------------
    def _page_state(self, page_url: str, *, token: CancelToken | None, refresh: bool) -> _PageState:
        now = self._clock()
        with self._lock:
            cached = self._pages.get(page_url)
        if cached is not None and not refresh and now - cached.fetched_at < self._page_ttl:
            return cached
        try:
            html = self._http.get_text(page_url, token=token)
        except NotFoundError as exc:  # the component's page moved: a protocol change, not a missing game
            raise SiteChangedError(detail=f"Livewire page {page_url} answered 404") from exc
        config = parsers.parse_livewire_config(html)
        uri = config.get("uri", "")
        if not uri.startswith(("http://", "https://")):
            raise SiteChangedError(detail=f"No livewireScriptConfig update URI on {page_url}")
        state = _PageState(fetched_at=now, update_uri=uri, snapshots=parsers.parse_livewire_snapshots(html))
        with self._lock:
            self._pages[page_url] = state
        return state

    def _csrf_token(self, *, token: CancelToken | None, refresh: bool) -> str:
        with self._lock:
            cached = self._csrf
        if cached and not refresh:
            return cached
        fresh = fetch_csrf_token(self._http, self._base, token=token)
        with self._lock:
            self._csrf = fresh
        return fresh


def _component_result(text: str, component: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        entry = data["components"][0]
        raw_snapshot = entry.get("snapshot")
        snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
        effects = entry.get("effects") or {}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise SiteChangedError(detail=f"Unexpected Livewire response for {component!r}: {exc}") from exc
    if not isinstance(snapshot, dict) or not isinstance(effects, dict):
        raise SiteChangedError(detail=f"Unexpected Livewire response shape for {component!r}")
    return {"snapshot": snapshot, "effects": effects}
