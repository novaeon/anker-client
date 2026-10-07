"""High-level, typed API over ankergames.net.

Thread-safe; every method may be called from any worker thread. Every network
method accepts ``token: CancelToken | None``.

Download protocol (verified 2026-10-06):

1. ``POST /generate-download-url/{download_id}`` with headers
   ``X-CSRF-TOKEN`` (from ``GET /csrf-token`` → ``{"token": …}``; refresh and
   retry once on HTTP 419), ``Accept: application/json``,
   ``X-Requested-With: XMLHttpRequest``, ``Origin``/``Referer`` = site / game page,
   body ``{}``. JSON responses (checked in the site's own order):

   * ``{"success": true, "download_url": "<ticket page url>"}`` → return the URL.
   * ``{"modal_type": "subscribe"}`` → ``AccessDeniedError``; ``{"geo_blocked": true, …}``
     → ``GeoBlockedError``.
   * ``{"show_upgrade": true, "details": …}`` → ``QuotaExceededError`` (details in message).
   * ``{"error": "... wait 42 seconds ..."}`` → ``RateLimitedError(42)`` (minutes/hours understood too).
   * ``{"error": "..."}`` → ``AccessDeniedError`` if it mentions sign in/login/subscription,
     else ``AnkerError(message)``. ``{"success": false, "message": "..."}`` is read the same way.
   * Any other answer is mapped by status — non-JSON bodies and Laravel's own JSON
     errors alike (``{"message": "Unauthenticated."}`` 401, ``"CSRF token mismatch."``
     419, ``"Too Many Attempts."`` 429, ``"Server Error"`` 500): 429 →
     ``RateLimitedError`` (``Retry-After``), redirect to ``/login`` or 401 →
     ``NotLoggedInError``, 403 → ``AccessDeniedError``, 404 → ``NotFoundError``,
     419 → ``AuthError``, 5xx → ``NetworkError``, another 4xx with a JSON
     ``message`` → that message, anything else → ``SiteChangedError``.
2. ``GET <ticket page url>`` → ``parsers.parse_ticket_page``. 401/403/404/410, or a
   redirect away from ``/download/…`` → ``LinkExpiredError`` (tickets are signed, short-lived).
3. ``GET https://ankergames.net/download-file/{ticket}[?cf-turnstile-response=…]``
   redirects to the real file on a CDN host (e.g. ``tunnelN.dlproxy.uk``) that
   supports ``Range`` (206), ``ETag`` and ``Content-Disposition``. When the
   ticket page carries a Turnstile widget the request without a token returns
   an HTML page instead of the file, and ``…/link`` returns 403
   ``{"error": "Please complete the verification challenge and try again."}``.
   Once a token has been accepted for a ticket, later requests for the same
   ticket need no new token. The token is appended exactly like the page script
   does (``url + (url has "?" ? "&" : "?") + "cf-turnstile-response=" +
   encodeURIComponent(token)``) so a signed query string is never re-serialised.
   ``resolve_file_url``/``probe`` ask for ``Range: bytes=0-0`` so only one byte
   of the file is ever transferred.

Torrents: ``POST /generate-torrent-url/{id}`` (same JSON conventions; returns
``torrent_url``) — sign-in required.

Auth: ``GET /login`` → form hidden inputs; ``POST /login`` form-encoded
``_token, email, password, remember=on``; success = final URL not under
``/login`` (Laravel redirects failures back to the form, whose error text becomes
the ``LoginFailedError`` message). The account is then read from the page
(see ``parsers.parse_logged_in_user``); when the page carries no account markup
but the site's ``is_logged_in`` cookie is set, the sign-in still counts and the
display name falls back to the email. Logout: ``POST /logout`` with ``_token``;
local cookies are cleared even when that request fails.

Cloudflare edge-caches guest pages. When the ``is_logged_in`` cookie is present
but a page lacks ``<meta name="page-auth">`` (a cached guest copy), the site's
own JS reloads it with ``?_auth=1``; ``current_user``/``login`` do the same.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
from requests.structures import CaseInsensitiveDict

from anker_client.constants import BASE_URL
from anker_client.core.errors import (
    AccessDeniedError,
    AnkerError,
    AuthError,
    GeoBlockedError,
    LinkExpiredError,
    LoginFailedError,
    NetworkError,
    NotFoundError,
    NotLoggedInError,
    QuotaExceededError,
    RateLimitedError,
    SiteChangedError,
    VerificationError,
)
from anker_client.core.models import (
    GameDetails,
    GameSummary,
    Genre,
    HomeSection,
    ListingPage,
    ResolvedLink,
    SortOrder,
    TicketPage,
    UserInfo,
)
from anker_client.core.tasks import CancelToken
from anker_client.site import parsers
from anker_client.site._common import (
    ajax_headers,
    csrf_headers,
    decode_text,
    fetch_csrf_token,
    is_cloudflare_challenge,
    retry_after_seconds,
)
from anker_client.site._html import form_errors, has_page_auth_meta, page_param, slug_from_url
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

_EXPIRED_STATUSES = frozenset({401, 403, 404, 410})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_WAIT_RE = re.compile(r"wait\s+(\d+)\s*(seconds?|secs?|s\b|minutes?|mins?|hours?|hrs?|h\b)?", re.IGNORECASE)
# encodeURIComponent() leaves these unescaped (quote() always keeps "_.-~" and alphanumerics).
_URI_COMPONENT_SAFE = "!*'()"
_ACCESS_HINT_RE = re.compile(r"sign[\s-]?in|log[\s-]?in|subscri", re.IGNORECASE)
_THROTTLE_RE = re.compile(r"too many (?:login |sign[\s-]?in )?attempts.*?(\d+)\s*(seconds?|minutes?)", re.IGNORECASE)
_VERIFICATION_HINT_RE = re.compile(r"turnstile|verification|challenge|captcha|cf-chl", re.IGNORECASE)
_EXPIRED_HINT_RE = re.compile(r"expired|invalid signature|no longer valid", re.IGNORECASE)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+|\*)", re.IGNORECASE)
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PREVIEW_BYTES = 64 * 1024


class AnkerGamesClient:
    def __init__(self, http: HttpClient, base_url: str = BASE_URL) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._csrf_token = ""
        self._genres: list[Genre] | None = None
        self._account_email = ""

    @property
    def http(self) -> HttpClient:
        return self._http

    # --- browsing -------------------------------------------------------------------
    def browse(
        self,
        *,
        page: int = 1,
        sort: SortOrder = SortOrder.NEWEST,
        genre: str | None = None,
        token: CancelToken | None = None,
    ) -> ListingPage:
        """``/games?page&sort`` or ``/genre/{genre}?page&sort``; ``genre="vr"`` → ``/games/vr``.

        A 404 for page > 1 (or a redirect back to the first page) returns an empty
        final page instead of raising. ``sort=NEWEST`` is the site default and is
        not sent (keeps URLs identical to the site's own, which Cloudflare caches).
        A genre made only of dots raises ``NotFoundError`` without a request (see
        ``_is_dot_segment``).
        """
        page = max(1, int(page))
        slug = (genre or "").strip().strip("/").casefold()
        if not slug:
            path = "/games"
        elif slug == "vr":
            path = "/games/vr"
        elif _is_dot_segment(slug):
            raise NotFoundError(detail=f"Invalid genre {genre!r}")
        else:
            path = f"/genre/{quote(slug, safe='-_.~')}"
        params: dict[str, Any] = {}
        if page > 1:
            params["page"] = page
        sort = SortOrder(sort)
        if sort is not SortOrder.NEWEST:
            params["sort"] = sort.value
        return self._listing(path, params, page, token)

    def search(self, query: str, *, page: int = 1, token: CancelToken | None = None) -> ListingPage:
        """Server search: ``/search/{quote(query)}?page=N`` (fuzzy; results ordered by relevance).

        A 404 on any page — or an answer from outside ``/search/`` — means "no (more)
        results" and yields an empty final page. Blank and dot-only queries return
        no results without a request.
        """
        page = max(1, int(page))
        text = " ".join((query or "").replace("/", " ").split())
        if not text or _is_dot_segment(text):
            return ListingPage(games=[], page=page, has_next=False)
        params = {"page": page} if page > 1 else {}
        return self._listing(
            f"/search/{quote(text, safe='')}", params, page, token, missing_is_empty=True, path_prefix="/search/"
        )

    def top_games(self, *, token: CancelToken | None = None) -> list[GameSummary]:
        html, final_url = self._fetch(self._url("/top-games"), token=token)
        return parsers.parse_listing(html, page=1, url=final_url).games

    def home_sections(self, *, token: CancelToken | None = None) -> list[HomeSection]:
        html, _ = self._fetch(self._url("/"), token=token)
        sections, genres = parsers._parse_home_page(html)
        if genres:
            with self._lock:
                self._genres = self._genres or genres
        return sections

    def genres(self, *, token: CancelToken | None = None) -> list[Genre]:
        """Main genres (cached in memory after the first successful call)."""
        with self._lock:
            if self._genres:
                return list(self._genres)
        html, _ = self._fetch(self._url("/"), token=token)
        genres = parsers.parse_genres(html)
        if not genres:
            raise SiteChangedError(detail="No /genre/ links found on the home page")
        with self._lock:
            self._genres = genres
        return list(genres)

    def game_details(self, slug: str, *, token: CancelToken | None = None) -> GameDetails:
        """Parse ``/game/{slug}``; ``NotFoundError`` when the game was removed."""
        slug = (slug or "").strip().strip("/")
        if not slug or "/" in slug or slug in (".", ".."):
            raise NotFoundError(detail=f"Invalid game slug {slug!r}")
        html, final_url = self._fetch(self._game_url(slug), token=token)
        if not slug_from_url(final_url):
            raise NotFoundError(detail=f"/game/{slug} redirected to {final_url}")
        return parsers.parse_game_page(html, slug=slug)

    # --- downloads ------------------------------------------------------------------
    def mint_download_ticket(
        self, download_id: int, *, referer_slug: str = "", token: CancelToken | None = None
    ) -> str:
        """Step 1 above. Returns the ticket page URL or raises a typed error."""
        path = f"/generate-download-url/{_positive_id(download_id)}"
        status, data, headers, text = self._post_api(path, referer_slug=referer_slug, token=token)
        return self._url_from_api_response(status, data, headers, text, "download_url")

    def mint_torrent_url(self, download_id: int, *, referer_slug: str = "", token: CancelToken | None = None) -> str:
        path = f"/generate-torrent-url/{_positive_id(download_id)}"
        status, data, headers, text = self._post_api(path, referer_slug=referer_slug, token=token)
        return self._url_from_api_response(status, data, headers, text, "torrent_url")

    def fetch_ticket_page(self, ticket_url: str, *, token: CancelToken | None = None) -> TicketPage:
        """Step 2 above."""
        response = self._http.get(
            ticket_url, headers={"Referer": f"{self._base}/"}, raise_for_status=False, token=token
        )
        try:
            status, final_url = response.status_code, response.url
            text = decode_text(response)
            headers = CaseInsensitiveDict(response.headers)
        finally:
            response.close()
        if is_cloudflare_challenge(status, headers, text):
            raise _cloudflare_error(status)
        if status in _EXPIRED_STATUSES:
            raise LinkExpiredError(detail=f"Ticket page answered HTTP {status}")
        if status == 429:
            raise RateLimitedError(int(retry_after_seconds(headers) or 60))
        if status >= 400:
            raise NetworkError(f"AnkerGames answered HTTP {status} for the download page.", status=status)
        if "/download/" not in urlsplit(final_url).path:
            raise LinkExpiredError(detail=f"Ticket page redirected to {final_url}")
        return parsers.parse_ticket_page(text, ticket_url=ticket_url)

    def resolve_file_url(
        self,
        file_url: str,
        *,
        verification_token: str = "",
        token: CancelToken | None = None,
        ticket_url: str = "",
    ) -> ResolvedLink:
        """Step 3 without a browser: follow redirects with a streamed GET (closed after headers).

        Raises ``VerificationError(ticket_url=…)`` if the server answers with an HTML page
        (challenge not satisfied) instead of a file; ``LinkExpiredError`` when the ticket is
        no longer valid. ``ticket_url`` (optional) is reported in the error so the user can
        open the right page in a browser; it defaults to ``file_url``.
        """
        url = file_url
        if verification_token:
            url = _append_query_param(file_url, "cf-turnstile-response", verification_token)
        referer = ticket_url or f"{self._base}/"
        response = self._http.get(
            url,
            headers={"Range": "bytes=0-0", "Referer": referer},
            stream=True,
            raise_for_status=False,
            token=token,
        )
        try:
            status = response.status_code
            content_type = _media_type(response.headers.get("Content-Type", ""))
            if status in _EXPIRED_STATUSES:
                body = _read_preview(response)
                if _VERIFICATION_HINT_RE.search(body):
                    raise VerificationError(ticket_url=ticket_url or file_url, detail=f"HTTP {status}: {body[:200]}")
                raise LinkExpiredError(detail=f"download-file answered HTTP {status}")
            if status == 429:
                raise RateLimitedError(int(retry_after_seconds(response.headers) or 60))
            if status >= 400:
                raise NetworkError(f"The download server answered HTTP {status}.", status=status)
            if content_type in ("text/html", "application/xhtml+xml", "application/json"):
                body = _read_preview(response)
                if _EXPIRED_HINT_RE.search(body) and not _VERIFICATION_HINT_RE.search(body):
                    raise LinkExpiredError(detail=f"download-file returned a {content_type} page mentioning expiry")
                raise VerificationError(
                    ticket_url=ticket_url or file_url,
                    detail=f"Expected a file but got {content_type} from {response.url}",
                )
            return _link_from_response(response)
        finally:
            response.close()

    def probe(self, url: str, *, token: CancelToken | None = None) -> ResolvedLink:
        """Probe a final file URL: ``GET`` with ``Range: bytes=0-0`` (fallback ``HEAD``) →
        size (from ``Content-Range``/``Content-Length``), ``accept_ranges`` (206 or
        ``Accept-Ranges: bytes``), ETag, Last-Modified, filename (``Content-Disposition``,
        RFC 5987 ``filename*`` preferred, else URL basename). 401/403/404/410 → ``LinkExpiredError``.
        """
        response = self._http.get(
            url, headers={"Range": "bytes=0-0"}, stream=True, raise_for_status=False, token=token
        )
        try:
            self._check_probe_status(response)
            if response.status_code in (200, 206):
                return _link_from_response(response)
            log.debug("Ranged probe answered %s; falling back to HEAD", response.status_code)
        finally:
            response.close()
        response = self._http.head(url, raise_for_status=False, token=token)
        try:
            self._check_probe_status(response)
            if response.status_code >= 400:
                raise NetworkError(
                    f"The download server answered HTTP {response.status_code}.", status=response.status_code
                )
            return _link_from_response(response)
        finally:
            response.close()

    @staticmethod
    def _check_probe_status(response: requests.Response) -> None:
        status = response.status_code
        if status in _EXPIRED_STATUSES:
            raise LinkExpiredError(detail=f"Probe answered HTTP {status} for {response.url}")
        if status == 429:
            raise RateLimitedError(int(retry_after_seconds(response.headers) or 30))
        if status >= 500:
            raise NetworkError(f"The download server answered HTTP {status}.", status=status)

    # --- account --------------------------------------------------------------------
    def login(self, email: str, password: str, *, remember: bool = True, token: CancelToken | None = None) -> UserInfo:
        """Raises ``LoginFailedError`` on bad credentials, ``NetworkError``/``SiteChangedError`` otherwise."""
        email = (email or "").strip()
        if not email or not password:
            raise LoginFailedError("Enter your email address and password.")
        login_url = self._url("/login")
        html, final_url = self._fetch(login_url, token=token)
        if not self._is_login_url(final_url):
            # Laravel's guest middleware redirected us: a session is already signed in.
            log.info("A session is already signed in; signing out first")
            self.logout(token=token)
            html, final_url = self._fetch(login_url, token=token)
        fields = parsers.parse_login_form(html)

        status, text, final_url = 0, "", login_url
        headers: CaseInsensitiveDict[str] = CaseInsensitiveDict()
        for attempt in range(2):
            if not fields.get("_token"):
                fields["_token"] = self._csrf_value(token, refresh=True)
            form = {**fields, "email": email, "password": password}
            if remember:
                form["remember"] = "on"
            else:
                form.pop("remember", None)
            response = self._http.post(
                login_url,
                data=form,
                headers={"Referer": login_url, "Origin": self._base},
                raise_for_status=False,
                token=token,
            )
            try:
                status, final_url = response.status_code, response.url
                text = decode_text(response)
                headers = CaseInsensitiveDict(response.headers)
            finally:
                response.close()
            if status == 419 and attempt == 0:
                log.info("Sign-in form token expired (419); reloading the form once")
                fields = parsers.parse_login_form(self._fetch(login_url, token=token)[0])
                continue
            break

        self._check_login_response(status, text, final_url, headers)
        with self._lock:
            self._csrf_token = ""  # Laravel regenerates the session token on sign-in
            self._account_email = email
        user = self._detect_user(text, token)
        if user is None:
            if not self._http.cookie("is_logged_in"):
                log.warning("Signed in (redirected to %s) but found no account markup or cookie", final_url)
            user = UserInfo(display_name=email, email=email)
        return self._complete_user(user)

    def _check_login_response(self, status: int, text: str, final_url: str, headers: Mapping[str, str]) -> None:
        if is_cloudflare_challenge(status, headers, text):
            raise AuthError(
                "AnkerGames asked for a browser check before signing in. "
                "Use “Continue with Discord”, or try again later."
            )
        if status == 429:
            raise RateLimitedError(
                int(retry_after_seconds(headers) or 60), "Too many sign-in attempts. Try again in a minute."
            )
        if status == 419:
            raise AuthError("Your sign-in session expired. Please try again.")
        if status >= 500:
            raise NetworkError(f"AnkerGames could not sign you in right now (HTTP {status}).", status=status)
        path = urlsplit(final_url).path.rstrip("/")
        if self._is_login_url(final_url) or status in (401, 403, 422):
            messages = form_errors(text) if text else []
            joined = " ".join(messages)
            throttle = _THROTTLE_RE.search(joined)
            if throttle:
                raise RateLimitedError(_seconds_from(throttle.group(1), throttle.group(2)), joined)
            raise LoginFailedError(messages[0] if messages else "", detail=f"HTTP {status} at {final_url}")
        if path.startswith("/two-factor"):
            raise AuthError("This account uses two-factor sign-in. Use “Continue with Discord” or the website.")
        if path.startswith(("/email/verify", "/verify-email")):
            raise AuthError("Verify your email address on AnkerGames first, then sign in again.")
        if status >= 400:
            raise NetworkError(f"Unexpected answer from AnkerGames while signing in (HTTP {status}).", status=status)

    def logout(self, *, token: CancelToken | None = None) -> None:
        """Best effort server-side sign-out; local cookies are always cleared."""
        try:
            csrf = self._csrf_value(token, refresh=True)
            headers = {
                "Referer": f"{self._base}/",
                "Origin": self._base,
                **csrf_headers(self._http, csrf),
            }
            response = self._http.post(
                self._url("/logout"),
                data={"_token": csrf} if csrf else {},
                headers=headers,
                allow_redirects=False,
                raise_for_status=False,
                token=token,
            )
            response.close()
            if response.status_code not in (200, 204, 301, 302, 303, 419):
                log.warning("Sign-out answered HTTP %s", response.status_code)
        except (NetworkError, SiteChangedError) as exc:
            log.warning("Sign-out request failed (%s); clearing the local session anyway", exc.detail or exc)
        finally:
            self._http.clear_cookies()
            with self._lock:
                self._csrf_token = ""
                self._account_email = ""

    def current_user(self, *, token: CancelToken | None = None) -> UserInfo | None:
        """Fetch a light page (``/``) and detect the signed-in user from the current cookies."""
        html, _ = self._fetch(self._url("/"), token=token)
        user = self._detect_user(html, token)
        return self._complete_user(user) if user is not None else None

    def _detect_user(self, html: str, token: CancelToken | None) -> UserInfo | None:
        user = parsers.parse_logged_in_user(html)
        if user is None and self._http.cookie("is_logged_in") and not has_page_auth_meta(html):
            # A Cloudflare-cached guest copy: ask the origin, exactly like the site's own script.
            log.debug("Got a cached guest page while signed in; reloading with _auth=1")
            fresh, _ = self._fetch(self._url("/"), params={"_auth": "1"}, token=token)
            user = parsers.parse_logged_in_user(fresh)
        return user

    def _complete_user(self, user: UserInfo) -> UserInfo:
        with self._lock:
            email = self._account_email
        user = user.copy()
        if not user.email and email:
            user.email = email
        if not user.display_name:
            user.display_name = user.email or "AnkerGames user"
        if user.profile_url:
            user.profile_url = urljoin(f"{self._base}/", user.profile_url)
        return user

    # --- internals ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _game_url(self, slug: str) -> str:
        return f"{self._base}/game/{quote(slug, safe='-_.~')}"

    def _is_login_url(self, url: str) -> bool:
        return urlsplit(url).path.rstrip("/") == "/login"

    def _fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        token: CancelToken | None = None,
    ) -> tuple[str, str]:
        """GET a page → (decoded text, final URL after redirects)."""
        response = self._http.get(url, params=params, headers=headers, token=token)
        try:
            return decode_text(response), response.url
        finally:
            response.close()

    def _listing(
        self,
        path: str,
        params: dict[str, Any],
        page: int,
        token: CancelToken | None,
        *,
        missing_is_empty: bool = False,
        path_prefix: str = "",
    ) -> ListingPage:
        try:
            html, final_url = self._fetch(self._url(path), params=params or None, token=token)
        except NotFoundError:
            if page > 1 or missing_is_empty:
                return ListingPage(games=[], page=page, has_next=False)
            raise
        if path_prefix and not urlsplit(final_url).path.startswith(path_prefix):
            # Redirected (or URL-normalised) to another page: its cards are not this listing's.
            log.debug("%s answered from %s; treating as no results", path, final_url)
            return ListingPage(games=[], page=page, has_next=False)
        if page > 1 and (page_param(final_url) or 1) != page:
            log.debug("Page %d of %s redirected to %s; treating as the end", page, path, final_url)
            return ListingPage(games=[], page=page, has_next=False)
        return parsers.parse_listing(html, page=page, url=final_url)

    def _csrf_value(self, token: CancelToken | None, *, refresh: bool = False) -> str:
        with self._lock:
            cached = self._csrf_token
        if cached and not refresh:
            return cached
        fresh = fetch_csrf_token(self._http, self._base, token=token)
        with self._lock:
            self._csrf_token = fresh
        return fresh

    def _post_api(
        self, path: str, *, referer_slug: str, token: CancelToken | None
    ) -> tuple[int, Any, CaseInsensitiveDict[str], str]:
        """POST ``{}`` to a JSON endpoint like the site's ``fetch()``; refresh CSRF and retry once on 419."""
        url = self._url(path)
        referer = self._game_url(referer_slug) if referer_slug else f"{self._base}/"
        status, text, headers = 0, "", CaseInsensitiveDict[str]()
        for attempt in range(2):
            csrf = self._csrf_value(token, refresh=attempt > 0)
            request_headers = {**ajax_headers(self._base, referer), **csrf_headers(self._http, csrf)}
            response = self._http.post(
                url, json={}, headers=request_headers, allow_redirects=False, raise_for_status=False, token=token
            )
            try:
                status = response.status_code
                text = decode_text(response)
                headers = CaseInsensitiveDict(response.headers)
            finally:
                response.close()
            if status == 419 and attempt == 0:
                log.info("CSRF token rejected (419) for %s; refreshing and retrying once", path)
                continue
            break
        data: Any = None
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return status, data, headers, text

    def _url_from_api_response(
        self, status: int, data: Any, headers: Mapping[str, str], text: str, key: str
    ) -> str:
        if not isinstance(data, dict):
            if is_cloudflare_challenge(status, headers, text):
                raise _cloudflare_error(status)
            raise self._status_error(status, headers)
        if data.get("success") and isinstance(data.get(key), str) and data[key].strip():
            return urljoin(f"{self._base}/", data[key].strip())
        message = _text_value(data.get("message"))
        if data.get("modal_type") == "subscribe":
            raise AccessDeniedError(message or "This download is only available to AnkerGames subscribers.")
        if data.get("geo_blocked"):
            raise GeoBlockedError(message)
        if data.get("show_upgrade"):
            details = data.get("details")
            raise QuotaExceededError(_quota_message(details), detail=json.dumps(details, default=str)[:500])
        error = _text_value(data.get("error"))
        if not error and data.get("success") is False and status < 300:
            error = message  # an app-level refusal phrased as {"success": false, "message": …}
        if error:
            raise _error_from_text(error)
        if status >= 300:
            # Laravel's own JSON errors ({"message": "Unauthenticated."} …) carry their meaning in the status.
            raise self._status_error(status, headers, message)
        raise SiteChangedError(detail=f"Unexpected JSON (HTTP {status}) with keys {sorted(data)}")

    def _status_error(self, status: int, headers: Mapping[str, str], message: str = "") -> AnkerError:
        """Typed error for a non-success API answer, from its status (``message``: Laravel's JSON text)."""
        location = headers.get("Location", "")
        detail = f"HTTP {status}: {message}" if message else f"HTTP {status}"
        if status == 429:
            return RateLimitedError(int(retry_after_seconds(headers) or 60), detail=detail)
        if status in _REDIRECT_STATUSES:
            if "/login" in location:
                return NotLoggedInError()
            return SiteChangedError(detail=f"Unexpected redirect (HTTP {status}) to {location}")
        if status == 401:
            return NotLoggedInError(detail=detail)
        if status == 403:
            return AccessDeniedError(message, detail=detail)
        if status == 404:
            return NotFoundError("This download option is no longer available.", detail=detail)
        if status == 419:
            return AuthError("Your AnkerGames session expired. Please try again.", detail=detail)
        if status >= 500:
            return NetworkError(
                f"AnkerGames is having problems right now (HTTP {status}). Try again later.",
                status=status,
                detail=detail,
            )
        if status >= 400 and message:
            return _error_from_text(message)
        return SiteChangedError(detail=f"Unexpected answer ({detail})")


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _cloudflare_error(status: int) -> NetworkError:
    return NetworkError(
        "AnkerGames asked for a browser check (Cloudflare). Try again in a few minutes.", status=status
    )


def _positive_id(download_id: int) -> int:
    value = int(download_id)
    if value <= 0:
        raise ValueError(f"Invalid download id {download_id!r}")
    return value


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list) and value and isinstance(value[0], str):
        return " ".join(value[0].split())
    return ""


def _seconds_from(number: str, unit: str) -> int:
    seconds = int(number)
    unit = unit.lower()
    if unit.startswith("h"):
        return seconds * 3600
    return seconds * 60 if unit.startswith("m") else seconds


def _error_from_text(text: str) -> AnkerError:
    """The site's own error sentence → ``RateLimitedError`` ("wait N …"), ``AccessDeniedError`` or ``AnkerError``."""
    wait = _WAIT_RE.search(text)
    if wait:
        return RateLimitedError(_seconds_from(wait.group(1), wait.group(2) or "seconds"), detail=text)
    if _ACCESS_HINT_RE.search(text):
        return AccessDeniedError(text)
    return AnkerError(text)


def _is_dot_segment(text: str) -> bool:
    """Only dots: "." / ".." are dot-segments that URL normalisation (requests/urllib3,
    Cloudflare) removes, so ``/search/..`` would really fetch ``/`` — the home page."""
    return bool(text) and not text.strip(".")


def _quota_message(details: Any) -> str:
    base = QuotaExceededError.default_message()
    if isinstance(details, str) and details.strip():
        return f"{base} ({' '.join(details.split())})"
    if isinstance(details, dict):
        if isinstance(details.get("message"), str) and details["message"].strip():
            return f"{base} ({' '.join(details['message'].split())})"
        parts = [
            f"{str(key).replace('_', ' ')}: {value}"
            for key, value in details.items()
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip()
        ]
        if parts:
            return f"{base} ({', '.join(parts[:6])})"
    return base


def _append_query_param(url: str, name: str, value: str) -> str:
    """Append ``name=value`` like the page script does; the existing query is kept byte for byte.

    (Re-serialising it could change its encoding and break a signed URL's HMAC.)
    """
    base, hash_sign, fragment = url.partition("#")
    joiner = "&" if "?" in base else "?"
    if base.endswith(("?", "&")):
        joiner = ""
    return f"{base}{joiner}{name}={quote(value, safe=_URI_COMPONENT_SAFE)}{hash_sign}{fragment}"


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _read_preview(response: requests.Response) -> str:
    """Up to 64 KiB of a streamed body, decoded leniently (for error classification)."""
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            chunks.append(chunk)
            size += len(chunk)
            if size >= _PREVIEW_BYTES:
                break
    except requests.RequestException:
        pass
    return b"".join(chunks)[:_PREVIEW_BYTES].decode("utf-8", errors="replace")


def _link_from_response(response: requests.Response) -> ResolvedLink:
    headers = response.headers
    status = response.status_code
    accept_ranges = headers.get("Accept-Ranges", "").strip().lower() == "bytes"
    size: int | None = None
    if status == 206:
        accept_ranges = True
        match = _CONTENT_RANGE_RE.search(headers.get("Content-Range", ""))
        if match and match.group(1) != "*":
            size = int(match.group(1))
    else:
        encoding = headers.get("Content-Encoding", "").strip().lower()
        length = headers.get("Content-Length", "").strip()
        if length.isdigit() and encoding in ("", "identity"):
            size = int(length)
    final_url = response.url
    return ResolvedLink(
        url=final_url,
        filename=_disposition_filename(headers.get("Content-Disposition", "")) or _url_filename(final_url),
        size=size,
        etag=headers.get("ETag", "").strip(),
        last_modified=headers.get("Last-Modified", "").strip(),
        accept_ranges=accept_ranges,
        content_type=_media_type(headers.get("Content-Type", "")),
    )


def _disposition_filename(value: str) -> str:
    """Filename from ``Content-Disposition`` — RFC 5987 ``filename*`` wins over ``filename``."""
    if not value:
        return ""
    star = re.search(r"filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if star:
        raw = star.group(1).strip().strip('"')
        charset, sep, rest = raw.partition("'")
        if sep:
            _language, _, encoded = rest.partition("'")
            try:
                name = unquote(encoded, encoding=charset or "utf-8", errors="replace")
            except LookupError:
                name = unquote(encoded, encoding="utf-8", errors="replace")
        else:
            name = unquote(raw)
        safe = _safe_filename(name)
        if safe:
            return safe
    plain = re.search(r'filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))', value, re.IGNORECASE)
    if plain:
        name = plain.group(1) if plain.group(1) is not None else plain.group(2).strip()
        # Only \" and \\ are escapes in practice; a lone backslash is a (Windows) path separator.
        return _safe_filename(re.sub(r'\\(["\\])', r"\1", name))
    return ""


def _url_filename(url: str) -> str:
    return _safe_filename(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))


def _safe_filename(name: str) -> str:
    """Basename only, Windows-safe characters, no trailing dots/spaces."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _INVALID_FILENAME_CHARS_RE.sub("_", base).strip().rstrip(". ")
    return "" if base in ("", ".", "..") else base
