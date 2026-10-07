"""Browser verification for Turnstile-protected downloads (QtWebEngine).

Some AnkerGames downloads are protected by a Cloudflare Turnstile check that
only a real browser can pass. AnkerClient never tries to solve or bypass it:
it shows the site's own ticket page in an embedded Chromium where Cloudflare
runs its normal check (usually invisible, sometimes a click) and the page's
own countdown then starts the file download. The client captures that
download's final URL and hands it to its own downloader.

Flow (``WebEngineVerifier`` implements ``services.downloads.resolver.VerificationProvider``):

1. ``verify(request, token=…)`` runs on a download worker thread. It queues
   the request and wakes the GUI thread through a queued signal, then waits
   on a ``threading.Event`` in short slices, checking ``token`` each slice.
2. On the GUI thread requests are served one at a time, FIFO. For each one
   the app's ankergames.net cookies (``http.export_cookies()``) are synced
   into the persistent browser profile (``paths.webengine_dir``; same user
   agent as the HTTP client) and a non-modal :class:`VerificationDialog`
   loads ``request.ticket_url``. The dialog explains what is happening (naming
   the game), shows the page, a countdown to the timeout, Cancel and
   "Open in browser instead".
3. Outcomes:
   * ``profile.downloadRequested`` from one of the dialog's pages → capture
     ``url()``/``suggestedFileName()``/``totalBytes()``/``mimeType()``, cancel
     the browser download, close → ``VerificationResult(url=…)``.
   * the main frame (or a popup opened with ``window.open``/``target=_blank``,
     loaded in a hidden probe page) commits a page on a host other than
     ankergames.net / the ticket host → ``VerificationResult(external_url=…)``
     (an HTTP error from such a host → ``VerificationError``).
   * "Open in browser instead" → opens the ticket page in the system browser
     → ``VerificationResult(external_url=ticket_url)`` (the job then offers
     "Import archive…").
   * timeout → ``VerificationTimeout(ticket_url=…)``; Cancel/close/Esc →
     ``VerificationCancelled(ticket_url=…)``.
   * the worker's token is cancelled → the dialog closes and ``verify`` raises
     ``OperationCancelled``.
   * ``shutdown()`` → every waiting worker gets ``VerificationCancelled``.
4. A popup that turns out to be a normal ankergames.net page replaces the
   view's page so the user can see and complete it.

Widget-only view (default): the ticket page still loads in full and runs
untouched, but the dialog shows only its Turnstile widget (``[data-ag-turnstile]``)
in a compact card. Isolated-world scripts (the page cannot see them) hide the
page from the moment ``<html>`` exists (``html[data-ac-pending]``), then, once
the widget is in the DOM, hide everything else, pin the widget in the middle of
the view, keep it displayed after it succeeds (the page's ``x-show`` would hide
it) and match its theme and background to the app's palette. Until Turnstile
has drawn into the widget, a "Loading the check…" overlay covers the view (the
view stays visible underneath, so the page keeps running at full speed). The
widget's own iframe is never touched or covered once shown, so whatever
Cloudflare asks for, the user answers in the real widget. The dialog falls back
to the full page (the classic layout) when the widget has not rendered
``widget_wait_seconds`` after the page loaded, on a site error (Cloudflare
serves its full-page check as a 403), when a popup page is shown, or when the
user clicks "Show full page".

The worker also gives up on its own (``VerificationTimeout``) when a started
dialog overruns its timeout by ``grace_seconds`` — a safety net should the
GUI thread stop processing events.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor
from PyQt6.QtWidgets import QApplication, QLabel, QProgressBar, QWidget

from anker_client.constants import SITE_HOST
from anker_client.core.errors import (
    OperationCancelled,
    VerificationCancelled,
    VerificationError,
    VerificationTimeout,
    VerificationUnavailable,
)
from anker_client.core.paths import AppPaths
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.resolver import VerificationRequest, VerificationResult
from anker_client.site.http import HttpClient
from anker_client.ui.dialogs import browser as web
from anker_client.ui.dialogs.browser import BrowserProfile, EmbeddedBrowserDialog, SitePage
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import LoadingOverlay, button, label, repolish
from anker_client.ui.widgets.settings_controls import alive

log = logging.getLogger(__name__)

#: Main-frame hosts that are part of the normal check and never mean "external host".
CHALLENGE_HOSTS = ("challenges.cloudflare.com",)
_MAX_POPUPS = 4
_NET_ERR_ABORTED = -3

# --- widget-only view -------------------------------------------------------------------------
_WIDGET_SCRIPT = "ankerclient-widget-only"
_COMPACT_WIDTH = 480
_WIDGET_PADDING = 20  # around the widget inside the view
_DEFAULT_WIDGET_HEIGHT = 65  # Turnstile "normal" size is 300×65
_REVEAL_DELAY_MS = 250  # let the freshly inserted widget iframe paint before uncovering it
_QWIDGETSIZE_MAX = 16777215

_WIDGET_CSS = """
html[data-ac-pending], html[data-ac-widget-only] { background: %(bg)s !important; overflow: hidden !important; }
html[data-ac-pending] body, html[data-ac-widget-only] body { background: transparent !important; }
html[data-ac-pending] body, html[data-ac-pending] body *,
html[data-ac-widget-only] body, html[data-ac-widget-only] body * { visibility: hidden !important; }
html[data-ac-widget-only] :has([data-ag-turnstile]) {
  display: block !important; transform: none !important; filter: none !important;
  backdrop-filter: none !important; perspective: none !important; contain: none !important;
  container-type: normal !important; will-change: auto !important; content-visibility: visible !important;
}
html[data-ac-widget-only] [data-ag-turnstile],
html[data-ac-widget-only] [data-ag-turnstile] * { visibility: visible !important; }
html[data-ac-widget-only] [data-ag-turnstile] {
  display: block !important; position: fixed !important; inset: 0 !important; margin: auto !important;
  width: fit-content !important; height: fit-content !important; z-index: 2147483647 !important;
}
"""

# Shared by every widget script. Wrapped in an IIFE by _widget_js: scripts of one
# isolated world share a global scope, so top-level declarations would clash.
_WIDGET_PRELUDE = """
function acStyle() {
  const html = document.documentElement;
  if (!html || document.getElementById('ac-widget-only-style')) return;
  const style = document.createElement('style');
  style.id = 'ac-widget-only-style';
  style.textContent = %(css)s;
  (document.head || html).appendChild(style);
}
function acHideUntilReady() {
  const mark = () => {
    const html = document.documentElement;
    if (!html) return false;
    if (!html.hasAttribute('data-ac-full')) {
      acStyle();
      html.setAttribute('data-ac-pending', '');
    }
    return true;
  };
  if (mark()) return;
  // DocumentCreation runs before <html> exists. The observer fires in the parser's own
  // task, right after it inserts <html>, so nothing can be painted before the mark is set.
  const observer = new MutationObserver(() => { if (mark()) observer.disconnect(); });
  observer.observe(document, {childList: true});
}
function acIsolate() {
  const none = {widget: false, rendered: false, w: 0, h: 0};
  const html = document.documentElement;
  if (!html || html.hasAttribute('data-ac-full')) return none;
  acStyle();
  const el = document.querySelector('[data-ag-turnstile]');
  if (!el) return none;
  if (!el.hasAttribute('data-ac-themed')) {
    el.setAttribute('data-ac-themed', '');
    // Only before Turnstile rendered into it: the page reads data-theme when it renders.
    if (!el.firstElementChild && !el.shadowRoot) el.dataset.theme = %(theme)s;
  }
  html.setAttribute('data-ac-widget-only', '');
  html.removeAttribute('data-ac-pending');
  const r = el.getBoundingClientRect();
  const rendered = Boolean(el.firstElementChild || el.shadowRoot) && r.width > 0 && r.height > 0;
  return {widget: true, rendered, w: Math.round(r.width), h: Math.round(r.height)};
}
"""

_REVEAL_JS = """(() => {
  const html = document.documentElement;
  if (!html) return;
  html.removeAttribute('data-ac-pending');
  html.removeAttribute('data-ac-widget-only');
  html.setAttribute('data-ac-full', '');
})()"""


def _widget_js(body: str, colors: palette.Palette) -> str:
    prelude = _WIDGET_PRELUDE % {
        "css": json.dumps(_WIDGET_CSS % {"bg": colors.surface}),
        "theme": json.dumps("dark" if colors.dark else "light"),
    }
    return f"(() => {{{prelude}\n{body}\n}})()"


def webengine_available() -> bool:
    """True when ``PyQt6.QtWebEngineWidgets`` imports (must be imported before QApplication exists)."""
    return web.webengine_available()


def browser_user_agent(paths: AppPaths) -> str:
    """The embedded Chromium UA with the ``QtWebEngine/x.y`` token removed ("" if unavailable)."""
    if not webengine_available() or QApplication.instance() is None:
        return ""
    try:
        return web.shared_browser(paths).user_agent
    except Exception:
        log.warning("Could not read the embedded browser's user agent", exc_info=True)
        return ""


def _format_clock(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# dialog
# ---------------------------------------------------------------------------


class VerificationDialog(EmbeddedBrowserDialog):
    """Shows one ticket page; emits ``finished_with`` exactly once with a
    ``VerificationResult`` or a ``VerificationError``."""

    finished_with = pyqtSignal(object)

    def __init__(
        self,
        browser: BrowserProfile,
        request: VerificationRequest,
        *,
        site_hosts: Iterable[str] = (SITE_HOST,),
        parent: QWidget | None = None,
        clock: Callable[[], float] = time.monotonic,
        widget_only: bool = True,
        widget_wait_seconds: float = 8.0,
    ) -> None:
        title = request.title or "this game"
        super().__init__(
            browser,
            window_title=f"Verify download · {title}",
            heading="Confirm your download",
            message=(
                f"AnkerGames checks that a real person is downloading <b>{html.escape(title)}</b>. "
                "If a check appears below, complete it. The download then starts on its own and "
                "this window closes."
            ),
            icon_name="shield",
            parent=parent,
        )
        self.request = request
        self._site_hosts = tuple(dict.fromkeys(h.lower() for h in site_hosts if h))
        self._clock = clock
        self._timeout = max(1.0, float(request.timeout_seconds or 180.0))
        self._started_at: float | None = None
        self._finished = False
        self._probes: list[SitePage] = []
        self._disconnect_downloads: Callable[[], None] = lambda: None

        self.countdown_label = label("", "muted")
        self.countdown_label.setMinimumWidth(64)
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.countdown_label.setToolTip("Time left before AnkerClient stops waiting for the check")
        clock = QLabel()
        clock.setFixedSize(16, 16)
        self.icons.bind(clock, "clock", "text_muted", 16)
        self.reload_button = button("Reload", "refresh", variant="ghost", size="sm", on_click=self._reload)
        self.reload_button.hide()
        self.status_extra.addWidget(self.reload_button)
        self.status_extra.addWidget(clock)
        self.status_extra.addWidget(self.countdown_label)
        self.time_bar = QProgressBar()
        self.time_bar.setRange(0, 1000)
        self.time_bar.setTextVisible(False)
        self.time_bar.setValue(1000)
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(2, self.time_bar)

        self.full_page_button = button(
            "Show full page",
            "eye",
            variant="ghost",
            tooltip="Show the whole AnkerGames page instead of only the check",
            on_click=self.show_full_page,
        )
        self.icons.bind(self.full_page_button, "eye", "text", 16)
        self.browser_button = button(
            "Open in browser instead",
            "external",
            variant="ghost",
            tooltip="Download with your web browser, then import the archive in AnkerClient",
            on_click=self._open_in_browser,
        )
        self.icons.bind(self.browser_button, "external", "text", 16)
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.footer.addWidget(self.full_page_button)
        self.footer.addWidget(self.browser_button)
        self.footer.addStretch(1)
        self.footer.addWidget(self.cancel_button)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)
        self.page.loadingChanged.connect(lambda info, p=self.page: self._on_loading(p, info))
        self.page.newWindowRequested.connect(self._on_new_window)
        self.set_status("Loading the download page…", "busy")

        self.loading_overlay = LoadingOverlay("Loading the check…")
        self.loading_overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.loading_overlay.setObjectName("verificationOverlay")
        self.loading_overlay.setStyleSheet(f"#verificationOverlay {{ background: {palette.current().surface}; }}")
        self.loading_overlay.hide()
        self.view_stack.addWidget(self.loading_overlay)

        self._widget_only = False
        self._widget_seen = False
        self._widget_rendered = False
        self._reveal_scheduled = False
        self._widget_height = _DEFAULT_WIDGET_HEIGHT
        self._widget_wait = max(0.0, float(widget_wait_seconds))
        self._site_loaded_at: float | None = None
        self._poll_js = ""
        self._widget_timer = QTimer(self)
        self._widget_timer.setInterval(200)
        self._widget_timer.timeout.connect(self._poll_widget)
        if widget_only:
            self._enter_widget_only()
        else:
            self.full_page_button.hide()

    # --- lifecycle ------------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def widget_only(self) -> bool:
        """True while the dialog shows only the page's Turnstile widget."""
        return self._widget_only

    @property
    def widget_seen(self) -> bool:
        return self._widget_seen

    @property
    def widget_rendered(self) -> bool:
        """True once Turnstile drew into the widget and the overlay was removed."""
        return self._widget_rendered

    def start(self) -> None:
        """Begin loading the ticket page and start the timeout clock."""
        self._disconnect_downloads = web.connect_once(self.browser.profile.downloadRequested, self._on_download)
        self._started_at = self._clock()
        self._tick_timer.start()
        self._tick()
        if self._widget_only:
            self._widget_timer.start()
        self.load(self.request.ticket_url)

    def abort(self) -> None:
        """Stop without emitting a result (the waiting worker already gave up)."""
        self._finish(None)

    def remaining(self) -> float:
        if self._started_at is None:
            return self._timeout
        return max(0.0, self._timeout - (self._clock() - self._started_at))

    def reject(self) -> None:
        self._finish(VerificationCancelled(ticket_url=self.request.ticket_url))
        super().reject()

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802
        self._finish(VerificationCancelled(ticket_url=self.request.ticket_url))
        super().closeEvent(event)

    def dispose(self) -> None:
        self._finish(None)
        super().dispose()

    def _finish(self, outcome: VerificationResult | BaseException | None) -> None:
        if self._finished:
            return
        self._finished = True
        self._tick_timer.stop()
        self._widget_timer.stop()
        self._disconnect_downloads()
        for page in self.pages():
            try:
                page.triggerAction(SitePage.WebAction.Stop)
            except RuntimeError:
                pass
        if outcome is not None:
            log.info("Verification for %r finished: %s", self.request.title, _describe(outcome))
            self.finished_with.emit(outcome)

    # --- widget-only view -----------------------------------------------------------------
    def _enter_widget_only(self) -> None:
        colors = palette.current()
        web.add_user_script(self.page, _WIDGET_SCRIPT, _widget_js("acHideUntilReady();", colors), at_creation=True)
        web.add_user_script(self.page, _WIDGET_SCRIPT, _widget_js("acIsolate();", colors))
        self._poll_js = _widget_js("return acIsolate();", colors)
        self.page.setBackgroundColor(QColor(colors.surface))
        self._widget_only = True
        self.loading_overlay.show()
        self.view_stack.setCurrentWidget(self.loading_overlay)  # StackAll: "current" = on top
        self.view.setMinimumHeight(0)
        layout = self.layout()
        if layout is not None:
            layout.setStretchFactor(self.browser_frame, 0)
        self._fit_compact()

    def _fit_compact(self) -> None:
        """Size the view to the widget and the dialog to its content (fixed height)."""
        self.view_holder.setFixedHeight(self._widget_height + 2 * _WIDGET_PADDING)
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        width = max(self.width(), _COMPACT_WIDTH) if self.isVisible() else _COMPACT_WIDTH
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else -1
        if height <= 0:
            height = layout.sizeHint().height()
        self.setMinimumSize(_COMPACT_WIDTH - 60, 0)
        self.setFixedHeight(height)
        self.resize(width, height)

    def show_full_page(self) -> None:
        """Leave the widget-only view and show the whole page (one way)."""
        if not self._widget_only:
            return
        self._widget_only = False
        self._widget_timer.stop()
        self.full_page_button.hide()
        self._remove_overlay()
        if alive(self.page):
            web.remove_user_scripts(self.page, _WIDGET_SCRIPT)
            web.run_isolated_js(self.page, _REVEAL_JS)
        layout = self.layout()
        if layout is not None:
            layout.setStretchFactor(self.browser_frame, 1)
        self.view_holder.setMinimumHeight(0)
        self.view_holder.setMaximumHeight(_QWIDGETSIZE_MAX)
        self.view.setMinimumHeight(self.VIEW_MIN_HEIGHT)
        self.setMinimumSize(*self.MIN_SIZE)
        self.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        center = self.geometry().center()
        self.resize(*self.DEFAULT_SIZE)
        if self.isVisible():
            geometry = self.geometry()
            geometry.moveCenter(center)
            screen = self.screen()
            if screen is not None:
                available = screen.availableGeometry()
                geometry.moveLeft(max(available.left(), min(geometry.left(), available.right() - geometry.width())))
                geometry.moveTop(max(available.top(), min(geometry.top(), available.bottom() - geometry.height())))
            self.move(geometry.topLeft())
        log.debug("Verification for %r shows the full page", self.request.title)

    def _poll_widget(self) -> None:
        if self._finished or not self._widget_only or not alive(self):
            self._widget_timer.stop()
            return
        if self.view.page() is not self.page:
            self.show_full_page()
            return
        web.run_isolated_js(self.page, self._poll_js, self._on_widget_state)
        if not self._widget_rendered and self._widget_overdue():
            log.info(
                "Turnstile widget %s for %r; showing the full page",
                "did not render" if self._widget_seen else "not on the page",
                self.request.title,
            )
            self.show_full_page()

    def _widget_overdue(self) -> bool:
        now = self._clock()
        loaded = self._site_loaded_at
        if loaded is not None:
            return now - loaded > self._widget_wait
        # The page never finished loading: don't keep the user staring at a spinner.
        started = self._started_at
        return started is not None and now - started > self._widget_wait + 15

    def _on_widget_state(self, state: Any) -> None:
        if self._finished or not self._widget_only or not alive(self):
            return
        if not isinstance(state, dict) or not state.get("widget"):
            return
        self._widget_seen = True
        height = int(state.get("h") or 0)
        if height > 0 and height != self._widget_height:
            self._widget_height = height
            self._fit_compact()
        if state.get("rendered") and not self._reveal_scheduled:
            self._reveal_scheduled = True
            QTimer.singleShot(_REVEAL_DELAY_MS, self._reveal_widget)

    def _reveal_widget(self) -> None:
        if self._finished or not self._widget_only or not alive(self):
            return
        self._widget_rendered = True
        self._remove_overlay()

    def _remove_overlay(self) -> None:
        self.loading_overlay.hide()
        self.view_stack.setCurrentWidget(self.view)

    # --- clock ----------------------------------------------------------------------------
    def _tick(self) -> None:
        if self._finished:
            return
        remaining = self.remaining()
        self.countdown_label.setText(f"{_format_clock(remaining)} left")
        self.time_bar.setValue(int(1000 * remaining / self._timeout))
        state = "error" if remaining <= 20 else ""
        if self.time_bar.property("state") != state:
            self.time_bar.setProperty("state", state)
            repolish(self.time_bar)
        if remaining <= 0:
            self._finish(VerificationTimeout(ticket_url=self.request.ticket_url))

    # --- user actions ---------------------------------------------------------------------
    def _open_in_browser(self) -> None:
        url = self.request.ticket_url
        web.open_url(url)
        self._finish(VerificationResult(external_url=url))

    def _reload(self) -> None:
        self.reload_button.hide()
        page = self.view.page()
        if page is not None:
            page.triggerAction(SitePage.WebAction.Reload)

    # --- browser signals --------------------------------------------------------------------
    def _is_site(self, host: str) -> bool:
        return web.host_in(host, self._site_hosts) or web.host_in(host, CHALLENGE_HOSTS)

    def _on_download(self, download: Any) -> None:
        if self._finished or not alive(self):
            return
        page = download.page()
        if page is not None and not self.owns_page(page):
            return  # another browser window's download; Qt cancels unaccepted downloads itself
        if download.isSavePageDownload():
            download.cancel()
            return
        result = VerificationResult(
            url=download.url().toString(),
            filename=download.suggestedFileName() or "",
            size=download.totalBytes() if download.totalBytes() > 0 else None,
            mime_type=download.mimeType() or "",
        )
        download.cancel()  # AnkerClient's own downloader fetches the file
        self.set_status("Download link received.", "success")
        self._finish(result)

    def _on_new_window(self, request: Any) -> None:
        if self._finished:
            return
        if len(self._probes) >= _MAX_POPUPS:
            log.debug("Ignoring extra popup %s", request.requestedUrl().toString())
            return
        probe = SitePage(self.browser.profile, self)
        probe.loadingChanged.connect(lambda info, p=probe: self._on_loading(p, info))
        probe.newWindowRequested.connect(self._on_new_window)
        self._probes.append(probe)
        log.debug("Popup requested: %s", request.requestedUrl().toString())
        request.openIn(probe)

    def _on_loading(self, page: SitePage, info: Any) -> None:
        if self._finished or not alive(self):
            return
        statuses = web.loading_status()
        status = info.status()
        url: QUrl = info.url()
        if info.isDownload() or url.scheme() not in ("http", "https") or status == statuses.LoadStoppedStatus:
            return
        shown = page is self.view.page()
        if status == statuses.LoadStartedStatus:
            if shown:
                self.reload_button.hide()
                self.set_status("Loading the download page…", "busy")
            return
        code = info.errorCode()
        http_error = info.errorDomain() == web.http_status_domain() and code >= 400
        if not self._is_site(url.host()):
            self._on_foreign_load(url, status == statuses.LoadSucceededStatus, code, http_error)
            return
        if status == statuses.LoadSucceededStatus and not http_error:
            if not shown and page in self._probes:
                self.show_full_page()
                self.show_page(page)  # a popup with a real site page: let the user see it
            elif shown and self._site_loaded_at is None:
                self._site_loaded_at = self._clock()
            self.set_status("Waiting for AnkerGames to start the download…", "busy")
        elif shown and code != _NET_ERR_ABORTED:
            self._on_site_error(code, http_error, info.errorString())

    def _on_foreign_load(self, url: QUrl, succeeded: bool, code: int, http_error: bool) -> None:
        if http_error:
            self._finish(VerificationError(
                f"The file server answered with an error (HTTP {code}). Try again later.",
                ticket_url=self.request.ticket_url,
                detail=url.toString(),
            ))
        elif succeeded or code != _NET_ERR_ABORTED:
            self._finish(VerificationResult(external_url=url.toString()))

    def _on_site_error(self, code: int, http_error: bool, error_text: str) -> None:
        self.show_full_page()  # whatever the page says (or asks), the user needs to see it
        if http_error and code in (404, 410):
            self.set_status("This download link has expired. Cancel, then start the download again.", "error")
        elif http_error and code == 429:
            self.set_status("AnkerGames is limiting downloads right now. Wait a moment, then reload.", "warning")
            self.reload_button.show()
        elif http_error:
            # 403 is also how Cloudflare serves its interactive check, so keep waiting.
            self.set_status("Complete the check below if one appears.", "busy")
        else:
            self.set_status(f"The download page could not be loaded ({error_text or code}).", "error")
            self.reload_button.show()


def _describe(outcome: object) -> str:
    if isinstance(outcome, VerificationResult):
        return "external host" if outcome.external_url else f"file {outcome.filename or '(unnamed)'}"
    return type(outcome).__name__


# ---------------------------------------------------------------------------
# verifier
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Pending:
    request: VerificationRequest
    done: threading.Event = field(default_factory=threading.Event)
    result: VerificationResult | None = None
    error: BaseException | None = None
    started_at: float | None = None  # monotonic time its dialog was shown
    completed: bool = False


class WebEngineVerifier(QObject):
    """``VerificationProvider`` implementation. Lives on the GUI thread; ``verify`` is called
    from download worker threads and blocks them until the dialog finishes."""

    #: A verification window opened (request) — e.g. to raise the main window or notify.
    request_started = pyqtSignal(object)
    #: A verification finished (request, VerificationResult | exception).
    request_finished = pyqtSignal(object, object)

    _wake = pyqtSignal()
    _close_abandoned = pyqtSignal()

    def __init__(
        self,
        http: HttpClient,
        paths: AppPaths,
        parent_window: QWidget | None = None,
        *,
        site_hosts: Iterable[str] = (SITE_HOST,),
        wait_slice: float = 0.1,
        grace_seconds: float = 30.0,
        widget_only: bool = True,
        widget_wait_seconds: float = 8.0,
    ) -> None:
        super().__init__()
        self._widget_only = widget_only
        self._widget_wait = widget_wait_seconds
        self._http = http
        self._paths = paths
        self._parent_window = parent_window
        self._site_hosts = tuple(site_hosts)
        self._wait_slice = max(0.01, wait_slice)
        self._grace = max(0.0, grace_seconds)
        self._gui_thread = threading.get_ident()
        self._lock = threading.Lock()
        self._queue: deque[_Pending] = deque()
        self._active: _Pending | None = None
        self._dialog: VerificationDialog | None = None
        self._closed = False
        self._wake.connect(self._pump, Qt.ConnectionType.QueuedConnection)
        self._close_abandoned.connect(self._on_close_abandoned, Qt.ConnectionType.QueuedConnection)

    # --- public API ----------------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return webengine_available() and QApplication.instance() is not None and not self._closed

    def set_parent_window(self, window: QWidget | None) -> None:
        self._parent_window = window

    def active_dialog(self) -> VerificationDialog | None:
        """The dialog currently shown (GUI thread; for tests and the shell)."""
        return self._dialog if alive(self._dialog) else None

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue) + (1 if self._active is not None else 0)

    def verify(self, request: VerificationRequest, *, token: CancelToken) -> VerificationResult:
        if not self.available:
            raise VerificationUnavailable(ticket_url=request.ticket_url)
        if threading.get_ident() == self._gui_thread:
            raise VerificationError(ticket_url=request.ticket_url, detail="verify() was called on the GUI thread")
        token.raise_if_cancelled()
        pending = _Pending(request)
        with self._lock:
            if self._closed:
                raise VerificationCancelled(ticket_url=request.ticket_url)
            self._queue.append(pending)
        log.info("Browser verification requested for %r", request.title)
        self._wake.emit()
        while not pending.done.wait(self._wait_slice):
            if token.cancelled:
                self._abandon(pending, OperationCancelled())
            elif self._overdue(pending):
                log.warning("Verification window for %r did not finish in time; giving up", request.title)
                self._abandon(pending, VerificationTimeout(ticket_url=request.ticket_url))
        if pending.error is not None:
            raise pending.error
        if pending.result is None:  # defensive: completion always sets one of the two
            raise VerificationError(ticket_url=request.ticket_url, detail="no result")
        return pending.result

    def shutdown(self) -> None:
        """Cancel any pending verification (unblocks waiting workers)."""
        with self._lock:
            self._closed = True
            waiting = list(self._queue)
            self._queue.clear()
            if self._active is not None:
                waiting.append(self._active)
        for pending in waiting:
            self._complete(pending, error=VerificationCancelled(
                "AnkerClient is closing.", ticket_url=pending.request.ticket_url
            ))
        if threading.get_ident() == self._gui_thread:
            self._close_dialog()
            self._active = None
        else:
            self._close_abandoned.emit()

    # --- worker-side helpers ---------------------------------------------------------------------
    def _complete(
        self, pending: _Pending, *, result: VerificationResult | None = None, error: BaseException | None = None
    ) -> bool:
        """Record the outcome once; returns False when another outcome won the race."""
        with self._lock:
            if pending.completed:
                return False
            pending.completed = True
            pending.result = result
            pending.error = error
        pending.done.set()
        return True

    def _abandon(self, pending: _Pending, error: BaseException) -> None:
        if self._complete(pending, error=error):
            self._close_abandoned.emit()

    def _overdue(self, pending: _Pending) -> bool:
        started = pending.started_at
        if started is None:
            return False
        limit = max(1.0, float(pending.request.timeout_seconds or 180.0)) + self._grace
        return time.monotonic() - started > limit

    # --- GUI-side -------------------------------------------------------------------------------
    def _next_pending(self) -> _Pending | None:
        with self._lock:
            if self._closed or self._active is not None:
                return None
            while self._queue:
                pending = self._queue.popleft()
                if not pending.completed:
                    pending.started_at = time.monotonic()
                    self._active = pending
                    return pending
        return None

    def _pump(self) -> None:
        pending = self._next_pending()
        if pending is None:
            return
        try:
            self._dialog = self._open_dialog(pending)
        except Exception as exc:
            log.exception("Could not open the verification window")
            self._complete(pending, error=VerificationError(
                "The verification window could not be opened.",
                ticket_url=pending.request.ticket_url,
                detail=str(exc),
            ))
            self._finish_active(pending)

    def _open_dialog(self, pending: _Pending) -> VerificationDialog:
        request = pending.request
        browser = web.shared_browser(self._paths)
        hosts = tuple(dict.fromkeys([*self._site_hosts, web.url_host(request.ticket_url)]))
        try:
            copied = browser.sync_site_cookies(self._http.export_cookies(), hosts)
            log.debug("Copied %d site cookies into the verification browser", copied)
        except Exception:
            log.warning("Could not copy cookies into the verification browser", exc_info=True)
        if browser.user_agent and browser.user_agent != getattr(self._http, "user_agent", browser.user_agent):
            log.debug("HTTP client and verification browser use different user agents")
        window = self._parent_window
        # Windows hides owned windows while their owner is minimized or in the tray: only parent
        # the dialog to a main window the user can actually see.
        parent = window if alive(window) and window.isVisible() and not window.isMinimized() else None
        dialog = VerificationDialog(
            browser,
            request,
            site_hosts=hosts,
            parent=parent,
            widget_only=self._widget_only,
            widget_wait_seconds=self._widget_wait,
        )
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        dialog.finished_with.connect(lambda outcome, p=pending: self._on_dialog_finished(p, outcome))
        dialog.start()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QApplication.alert(dialog)
        self.request_started.emit(request)
        return dialog

    def _on_dialog_finished(self, pending: _Pending, outcome: object) -> None:
        if isinstance(outcome, VerificationResult):
            self._complete(pending, result=outcome)
        elif isinstance(outcome, BaseException):
            self._complete(pending, error=outcome)
        if self._active is pending:
            # Defer closing: we are inside one of the dialog's own signal handlers.
            QTimer.singleShot(0, lambda p=pending: self._finish_active(p))
        self.request_finished.emit(pending.request, outcome)

    def _on_close_abandoned(self) -> None:
        active = self._active
        if active is not None and active.completed:
            if self._dialog is not None and alive(self._dialog):
                self._dialog.abort()
            self._finish_active(active)
        elif active is None:
            self._close_dialog()

    def _finish_active(self, pending: _Pending) -> None:
        if self._active is not pending:
            return  # already finished (e.g. abandoned and closed meanwhile)
        self._close_dialog()
        self._active = None
        if not self._closed:
            QTimer.singleShot(0, self._pump)

    def _close_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is not None and not sip.isdeleted(dialog):
            dialog.dispose()
