"""Turns a ``DownloadOption`` into a final, downloadable ``ResolvedLink``.

``VerificationProvider`` is implemented by the UI in
``anker_client/ui/dialogs/verification.py``; the container installs it with
:meth:`LinkResolver.set_verifier` once QtWebEngine is up.

Flow::

    ticket_url = client.mint_download_ticket(option.download_id, referer_slug=slug)
    page = client.fetch_ticket_page(ticket_url)
    if page.external_provider:            -> raise ExternalHostError(ticket_url, page.external_provider)
    if not page.requires_verification:
        token.sleep(page.wait_seconds)    # respect the site's countdown
        link = client.resolve_file_url(page.file_url, ticket_url=ticket_url)  # when supported
        # If the server unexpectedly answers with a challenge page
        # (VerificationError) the browser path below is used instead.
    else:
        if verifier is None or not verifier.available:
                                          -> raise VerificationUnavailable(ticket_url=ticket_url)
        result = verifier.verify(VerificationRequest(...), token=token)
        if result.external_url:           -> raise ExternalHostError(result.external_url, ...)
                                             # (ticket/site URL = the user chose "Open in browser")
        link = client.probe(result.url)   # merge filename/size reported by the browser
    return link

``on_state(state, text)`` is called with ``JobState.RESOLVING`` and
``JobState.VERIFYING`` (plus a short human status line) so the manager can
reflect progress in the job.

Errors: everything raised is an ``AnkerError``. ``VerificationError``s always
carry the ticket URL (so the UI can offer "Open in browser"); unexpected
exceptions from the verifier are wrapped in ``VerificationError``.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from anker_client.constants import SITE_HOST
from anker_client.core.errors import (
    AnkerError,
    ExternalHostError,
    OperationCancelled,
    SiteChangedError,
    VerificationError,
    VerificationUnavailable,
)
from anker_client.core.models import DownloadOption, JobState, ResolvedLink, TicketPage
from anker_client.core.tasks import CancelToken
from anker_client.site.client import AnkerGamesClient

log = logging.getLogger(__name__)

#: Upper bound for the site's client-side countdown (protects against a broken page value).
MAX_WAIT_SECONDS = 900


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    ticket_url: str
    title: str
    job_id: str = ""
    timeout_seconds: float = 180.0


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What the browser produced: either the final file URL or an external hand-off URL."""

    url: str = ""
    filename: str = ""
    size: int | None = None
    mime_type: str = ""
    external_url: str = ""


class VerificationProvider(Protocol):
    """Runs the site's browser challenge for a ticket page and captures the file URL.

    Implementations must be callable from a worker thread; they block until done
    and must honour ``token`` (raise ``VerificationCancelled``/``OperationCancelled``)
    and the request timeout (raise ``VerificationTimeout``).
    """

    @property
    def available(self) -> bool: ...

    def verify(self, request: VerificationRequest, *, token: CancelToken) -> VerificationResult: ...


StateCallback = Callable[[JobState, str], None]


class LinkResolver:
    def __init__(
        self,
        client: AnkerGamesClient,
        verifier: VerificationProvider | None = None,
        *,
        verification_timeout: Callable[[], float] = lambda: 180.0,
    ) -> None:
        self._client = client
        self._verifier = verifier
        self._verification_timeout = verification_timeout
        self._lock = threading.Lock()
        self._passes_ticket_url = _accepts_keyword(client.resolve_file_url, "ticket_url")

    @property
    def verifier(self) -> VerificationProvider | None:
        with self._lock:
            return self._verifier

    def set_verifier(self, verifier: VerificationProvider | None) -> None:
        with self._lock:
            self._verifier = verifier

    def resolve(
        self,
        option: DownloadOption,
        *,
        slug: str,
        title: str,
        job_id: str = "",
        token: CancelToken,
        on_state: Callable[[JobState, str], None] | None = None,
    ) -> ResolvedLink:
        notify = _safe_callback(on_state)
        token.raise_if_cancelled()
        notify(JobState.RESOLVING, "Requesting download link…")
        ticket_url = self._client.mint_download_ticket(option.download_id, referer_slug=slug, token=token)
        token.raise_if_cancelled()
        page = self._client.fetch_ticket_page(ticket_url, token=token)
        token.raise_if_cancelled()
        log.debug(
            "Ticket page for %s: verification=%s wait=%ss external=%r",
            slug, page.requires_verification, page.wait_seconds, page.external_provider,
        )
        if page.external_provider:
            raise ExternalHostError(ticket_url, page.external_provider)
        if page.requires_verification:
            return self._resolve_with_browser(page, ticket_url, title=title, job_id=job_id, token=token, notify=notify)
        return self._resolve_direct(page, ticket_url, title=title, job_id=job_id, token=token, notify=notify)

    # --- paths ------------------------------------------------------------------------
    def _resolve_direct(
        self,
        page: TicketPage,
        ticket_url: str,
        *,
        title: str,
        job_id: str,
        token: CancelToken,
        notify: StateCallback,
    ) -> ResolvedLink:
        wait = max(0, min(int(page.wait_seconds or 0), MAX_WAIT_SECONDS))
        if wait:
            notify(JobState.RESOLVING, f"Waiting {wait}s for the download to unlock…")
            token.sleep(wait)
        notify(JobState.RESOLVING, "Requesting download link…")
        if not page.file_url:
            raise SiteChangedError(detail=f"no file URL on ticket page {ticket_url}")
        try:
            if self._passes_ticket_url:
                # Referer = the ticket page, exactly like the site's own JavaScript.
                link = self._client.resolve_file_url(page.file_url, token=token, ticket_url=ticket_url)
            else:
                link = self._client.resolve_file_url(page.file_url, token=token)
        except VerificationError as exc:
            # The page did not show a challenge but the server still demanded one:
            # fall back to the browser when we have one.
            verifier = self.verifier
            if verifier is None or not _is_available(verifier):
                raise VerificationUnavailable(ticket_url=ticket_url, detail=exc.detail or str(exc)) from exc
            log.info("Server requested verification for %s; falling back to the browser", ticket_url)
            return self._resolve_with_browser(page, ticket_url, title=title, job_id=job_id, token=token, notify=notify)
        return _require_url(link, ticket_url)

    def _resolve_with_browser(
        self,
        page: TicketPage,
        ticket_url: str,
        *,
        title: str,
        job_id: str,
        token: CancelToken,
        notify: StateCallback,
    ) -> ResolvedLink:
        verifier = self.verifier
        if verifier is None or not _is_available(verifier):
            raise VerificationUnavailable(ticket_url=ticket_url)
        notify(JobState.VERIFYING, "Waiting for browser verification…")
        request = VerificationRequest(
            ticket_url=ticket_url,
            title=title,
            job_id=job_id,
            timeout_seconds=self._timeout_seconds(),
        )
        result = self._run_verifier(verifier, request, token=token)
        token.raise_if_cancelled()
        if result.external_url:
            raise _external_host_error(result.external_url, ticket_url, page.external_provider)
        if not result.url:
            raise VerificationError("The browser did not provide a download link.", ticket_url=ticket_url)
        notify(JobState.RESOLVING, "Checking download link…")
        probed = self._client.probe(result.url, token=token)
        return _require_url(merge_verification_result(probed, result), ticket_url)

    def _run_verifier(
        self, verifier: VerificationProvider, request: VerificationRequest, *, token: CancelToken
    ) -> VerificationResult:
        try:
            result = verifier.verify(request, token=token)
        except OperationCancelled:
            raise
        except VerificationError as exc:
            if not exc.ticket_url:
                exc.ticket_url = request.ticket_url
            raise
        except AnkerError:
            raise
        except Exception as exc:
            log.exception("Verification provider failed for %s", request.ticket_url)
            raise VerificationError(ticket_url=request.ticket_url, detail=repr(exc)) from exc
        if not isinstance(result, VerificationResult):
            raise VerificationError(
                "The browser did not provide a download link.",
                ticket_url=request.ticket_url,
                detail=f"verifier returned {type(result).__name__}",
            )
        return result

    def _timeout_seconds(self) -> float:
        try:
            value = float(self._verification_timeout())
        except Exception:
            log.warning("verification_timeout callable failed; using 180 s", exc_info=True)
            return 180.0
        return value if value > 0 else 180.0


# --- helpers ----------------------------------------------------------------------------


def merge_verification_result(link: ResolvedLink, result: VerificationResult) -> ResolvedLink:
    """Combine the probe of the captured URL with what the browser reported.

    The browser's suggested filename wins (Chromium already applied
    ``Content-Disposition``); the probe's size wins because it is exact
    (``Content-Range``), the browser's is only a fallback (also when the probe
    reported a meaningless 0, e.g. a ``HEAD`` with ``Content-Length: 0``).
    """
    return ResolvedLink(
        url=link.url or result.url,
        filename=result.filename or link.filename,
        size=link.size if link.size else (result.size if result.size else None),
        etag=link.etag,
        last_modified=link.last_modified,
        accept_ranges=link.accept_ranges,
        content_type=link.content_type or result.mime_type,
    )


def _external_host_error(external_url: str, ticket_url: str, provider: str) -> ExternalHostError:
    """The hand-off reported by the browser.

    The verification dialog reports the ticket page itself (or another
    ankergames.net page) when the user chose "Open in browser instead"; naming
    ankergames.net as an "external file host" would be wrong there.
    """
    host = _host_of(external_url)
    if external_url == ticket_url or _is_site_host(host):
        error = ExternalHostError(external_url, provider)
        error.message = "The download continues in your browser. When it has finished, import the archive."
        error.args = (error.message,)
        return error
    return ExternalHostError(external_url, provider or host)


def _is_site_host(host: str) -> bool:
    host = host.casefold()
    return host == SITE_HOST or host.endswith("." + SITE_HOST)


def _require_url(link: ResolvedLink, ticket_url: str) -> ResolvedLink:
    if not link.url:
        raise SiteChangedError(detail=f"empty download URL for ticket {ticket_url}")
    return link


def _is_available(verifier: VerificationProvider) -> bool:
    try:
        return bool(verifier.available)
    except Exception:
        log.warning("Verification provider availability check failed", exc_info=True)
        return False


def _accepts_keyword(func: Callable[..., object], name: str) -> bool:
    """Whether ``func`` takes keyword ``name`` (optional extensions of the site client)."""
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        (p.name == name and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)) or p.kind is p.VAR_KEYWORD
        for p in parameters
    )


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _safe_callback(on_state: StateCallback | None) -> StateCallback:
    def notify(state: JobState, text: str) -> None:
        if on_state is None:
            return
        try:
            on_state(state, text)
        except Exception:
            log.exception("on_state callback failed")

    return notify
