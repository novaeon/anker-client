"""Embedded Chromium (QtWebEngine) shared by the verification and Discord sign-in dialogs.

* QtWebEngine is imported here at module level inside a guard. It must be
  imported before the ``QApplication`` exists (``app.main`` imports the
  verification module early); when the import fails (missing DLLs, imported
  too late, stripped build) :func:`webengine_available` is False and callers
  fall back to "Open in browser".
* Never touch ``QWebEngineProfile.defaultProfile()`` and never let a
  ``QWebEngineView`` create its own page: both use the default profile, which
  crashed the process on this machine. Every page lives on the persistent
  profile from :func:`shared_browser`.
* :class:`BrowserProfile` — one persistent, named profile per data folder
  (``paths.webengine_dir``: cookies, local storage, HTTP cache), with the
  ``QtWebEngine/x.y`` token removed from its user agent so it matches the
  ``requests`` client, plus a live mirror of its cookie store
  (``cookieAdded``/``cookieRemoved``) to read and sync site cookies.
* :class:`SitePage` — a page that hides save/download/inspect context-menu
  actions (so the user cannot accidentally "download" the wrong thing),
  denies feature permissions and routes console output to the debug log.
* :class:`EmbeddedBrowserDialog` — dialog chrome around a view: heading,
  message, address line (lock/globe + host), status line with spinner, thin
  load bar and a footer the subclasses fill with buttons.
* :func:`add_user_script` / :func:`run_isolated_js` — scripts that run in
  Chromium's isolated "application world": they share the page's DOM but
  never its JavaScript globals, so the page cannot see or tamper with them.

Everything here is GUI-thread only.
"""

from __future__ import annotations

import html
import ipaddress
import logging
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QDateTime, QObject, Qt, QUrl
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.paths import AppPaths
from anker_client.ui.widgets.common import label, repolish
from anker_client.ui.widgets.settings_controls import IconBinder, StatusLine, alive, open_url

log = logging.getLogger(__name__)

try:  # must run before QApplication is created (see module docstring)
    from PyQt6.QtWebEngineCore import (
        QWebEngineLoadingInfo,
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineScript,
        QWebEngineSettings,
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except Exception as _exc:  # ImportError, DLL load failure, or imported after QApplication
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    log.warning("QtWebEngine is unavailable: %s", _IMPORT_ERROR)
    # Placeholders keep the subclasses below definable; they are never instantiated
    # because every caller checks webengine_available() first.
    QWebEnginePage = QObject  # type: ignore[misc,assignment]
    QWebEngineView = QWidget  # type: ignore[misc,assignment]
    QWebEngineLoadingInfo = None  # type: ignore[misc,assignment]
    QWebEngineProfile = None  # type: ignore[misc,assignment]
    QWebEngineScript = None  # type: ignore[misc,assignment]
    QWebEngineSettings = None  # type: ignore[misc,assignment]
else:
    _IMPORT_ERROR = ""

PROFILE_NAME = "AnkerClient"
_QTWEBENGINE_TOKEN = re.compile(r"\s*QtWebEngine/\S+")
_CLOUDFLARE_COOKIE_PREFIXES = ("cf_", "__cf", "_cf")


# ---------------------------------------------------------------------------
# availability / small pure helpers
# ---------------------------------------------------------------------------


def webengine_available() -> bool:
    """True when ``PyQt6.QtWebEngineWidgets`` imported successfully."""
    return not _IMPORT_ERROR


def webengine_import_error() -> str:
    return _IMPORT_ERROR


def clean_user_agent(user_agent: str) -> str:
    """Remove the ``QtWebEngine/x.y`` product token (sites treat it as a bot marker)."""
    return " ".join(_QTWEBENGINE_TOKEN.sub("", user_agent or "").split())


def host_matches(host: str, domain: str) -> bool:
    """``www.ankergames.net`` matches ``ankergames.net`` (and ``.ankergames.net``)."""
    host = (host or "").strip().strip(".").lower()
    domain = (domain or "").strip().lstrip(".").lower()
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def host_in(host: str, domains: Iterable[str]) -> bool:
    return any(host_matches(host, d) for d in domains)


def url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_ip_or_localhost(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def cookie_from_dict(data: dict[str, Any]) -> QNetworkCookie | None:
    """``HttpClient.export_cookies`` entry → ``QNetworkCookie`` (None when unusable)."""
    name = str(data.get("name") or "")
    domain = str(data.get("domain") or "")
    if not name or not domain:
        return None
    cookie = QNetworkCookie(QByteArray(name.encode("utf-8")), QByteArray(str(data.get("value") or "").encode("utf-8")))
    cookie.setDomain(domain)
    cookie.setPath(str(data.get("path") or "/"))
    cookie.setSecure(bool(data.get("secure")))
    expires = data.get("expires")
    if isinstance(expires, int | float) and expires > 0:
        cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(expires)))
    return cookie


def cookie_to_dict(cookie: QNetworkCookie) -> dict[str, Any]:
    """``QNetworkCookie`` → the dict shape ``HttpClient.import_cookies`` accepts."""
    expiry = cookie.expirationDate()
    return {
        "name": bytes(cookie.name().data()).decode("utf-8", "replace"),
        "value": bytes(cookie.value().data()).decode("utf-8", "replace"),
        "domain": cookie.domain(),
        "path": cookie.path() or "/",
        "secure": cookie.isSecure(),
        "expires": expiry.toSecsSinceEpoch() if (not cookie.isSessionCookie() and expiry.isValid()) else None,
    }


def cookie_origin(cookie: QNetworkCookie) -> QUrl:
    host = cookie.domain().lstrip(".")
    scheme = "https" if cookie.isSecure() or not _is_ip_or_localhost(host) else "http"
    return QUrl(f"{scheme}://{host}/")


def _is_cloudflare_cookie(name: str) -> bool:
    return name.lower().startswith(_CLOUDFLARE_COOKIE_PREFIXES)


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class BrowserProfile(QObject):
    """The persistent embedded-browser profile plus a mirror of its cookies."""

    def __init__(self, storage_dir: Path, parent: QObject | None = None, *, name: str = PROFILE_NAME) -> None:
        super().__init__(parent)
        if not webengine_available():
            raise RuntimeError(f"QtWebEngine is unavailable: {_IMPORT_ERROR}")
        storage_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir = storage_dir
        profile = QWebEngineProfile(name, self)
        profile.setPersistentStoragePath(str(storage_dir / "Profile"))
        profile.setCachePath(str(storage_dir / "Cache"))
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setHttpCacheMaximumSize(64 * 1024 * 1024)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        profile.setSpellCheckEnabled(False)
        profile.setHttpUserAgent(clean_user_agent(profile.httpUserAgent()))
        attrs = QWebEngineSettings.WebAttribute
        settings = profile.settings()
        settings.setAttribute(attrs.PluginsEnabled, False)
        settings.setAttribute(attrs.FullScreenSupportEnabled, False)
        settings.setAttribute(attrs.PlaybackRequiresUserGesture, True)
        settings.setAttribute(attrs.JavascriptCanOpenWindows, True)  # popups → newWindowRequested
        self.profile = profile
        self._cookies: dict[tuple[str, str, str], QNetworkCookie] = {}
        store = profile.cookieStore()
        store.cookieAdded.connect(self._on_cookie_added)
        store.cookieRemoved.connect(self._on_cookie_removed)
        store.loadAllCookies()  # async: existing cookies arrive through cookieAdded

    # --- user agent -------------------------------------------------------------------
    @property
    def user_agent(self) -> str:
        return clean_user_agent(self.profile.httpUserAgent())

    # --- cookies ----------------------------------------------------------------------
    @staticmethod
    def _key(cookie: QNetworkCookie) -> tuple[str, str, str]:
        return (bytes(cookie.name().data()).decode("utf-8", "replace"), cookie.domain().lower(), cookie.path() or "/")

    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        self._cookies[self._key(cookie)] = QNetworkCookie(cookie)

    def _on_cookie_removed(self, cookie: QNetworkCookie) -> None:
        self._cookies.pop(self._key(cookie), None)

    def cookies_for(self, domains: Iterable[str]) -> list[dict[str, Any]]:
        """Cookies (as ``HttpClient`` dicts) whose domain belongs to one of ``domains``."""
        wanted = list(domains)
        return [cookie_to_dict(c) for c in self._cookies.values() if host_in(c.domain(), wanted)]

    def set_cookies(self, cookies: Iterable[dict[str, Any]], domains: Iterable[str]) -> int:
        """Copy ``HttpClient``-style cookies for ``domains`` into the browser. Returns the count."""
        wanted = list(domains)
        store = self.profile.cookieStore()
        count = 0
        for data in cookies:
            cookie = cookie_from_dict(data)
            if cookie is None or not host_in(cookie.domain(), wanted):
                continue
            store.setCookie(cookie, cookie_origin(cookie))
            count += 1
        return count

    def sync_site_cookies(self, cookies: list[dict[str, Any]], domains: Iterable[str]) -> int:
        """Make the browser's site identity match the app's: drop site cookies the app does not
        have (e.g. after sign-out; Cloudflare's own cookies are kept), then copy the app's."""
        wanted = list(domains)
        keep = {(str(c.get("name")), str(c.get("domain", "")).lstrip(".").lower()) for c in cookies}
        store = self.profile.cookieStore()
        for cookie in list(self._cookies.values()):
            name, domain, _path = self._key(cookie)
            if not host_in(domain, wanted) or _is_cloudflare_cookie(name):
                continue
            if (name, domain.lstrip(".")) not in keep:
                store.deleteCookie(cookie, cookie_origin(cookie))
        return self.set_cookies(cookies, wanted)


_browsers: dict[str, BrowserProfile] = {}


def shared_browser(paths: AppPaths) -> BrowserProfile:
    """The process-wide :class:`BrowserProfile` for ``paths.webengine_dir`` (created on first use).

    GUI thread only; requires a ``QApplication`` and :func:`webengine_available`.
    """
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("shared_browser() needs a QApplication")
    key = os.path.normcase(os.path.abspath(paths.webengine_dir))
    browser = _browsers.get(key)
    if browser is not None and alive(browser):
        return browser
    browser = BrowserProfile(Path(paths.webengine_dir), app)
    _browsers[key] = browser
    log.debug("Created embedded browser profile in %s", paths.webengine_dir)
    return browser


def release_browsers() -> None:
    """Schedule deletion of every profile (call after all browser windows are gone)."""
    for browser in list(_browsers.values()):
        if alive(browser):
            browser.deleteLater()
    _browsers.clear()


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def _hidden_actions() -> tuple[Any, ...]:
    actions = QWebEnginePage.WebAction
    return (
        actions.SavePage,
        actions.DownloadLinkToDisk,
        actions.DownloadImageToDisk,
        actions.DownloadMediaToDisk,
        actions.ViewSource,
        actions.InspectElement,
        actions.OpenLinkInNewWindow,
        actions.OpenLinkInNewTab,
        actions.OpenLinkInNewBackgroundTab,
    )


class SitePage(QWebEnginePage):
    """A page on the shared profile with a reduced context menu and no feature permissions."""

    def __init__(self, profile: Any, parent: QObject | None = None) -> None:
        super().__init__(profile, parent)
        for action_id in _hidden_actions():
            action = self.action(action_id)
            if action is not None:
                action.setVisible(False)
                action.setEnabled(False)
        signal = getattr(self, "permissionRequested", None)
        if signal is not None:
            signal.connect(self._deny_permission)

    @staticmethod
    def _deny_permission(permission: Any) -> None:
        try:
            permission.deny()
        except Exception:
            log.debug("Could not deny a browser permission request", exc_info=True)

    def javaScriptConsoleMessage(self, level: Any, message: str | None, line: int, source: str | None) -> None:  # noqa: N802
        log.debug("[browser console] %s:%s %s", source, line, message)


# ---------------------------------------------------------------------------
# dialog chrome
# ---------------------------------------------------------------------------


class EmbeddedBrowserDialog(QDialog):
    """Heading + message, address line, status, the web view and a footer for buttons.

    Subclasses call :meth:`load`, update :meth:`set_status` and add buttons to
    ``footer``. :meth:`dispose` stops loading and deletes the dialog.
    """

    MIN_SIZE = (720, 560)
    DEFAULT_SIZE = (980, 760)
    VIEW_MIN_HEIGHT = 320

    def __init__(
        self,
        browser: BrowserProfile,
        *,
        window_title: str,
        heading: str,
        message: str = "",
        icon_name: str = "globe",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setObjectName("embeddedBrowserDialog")
        self.setMinimumSize(*self.MIN_SIZE)
        self.resize(*self.DEFAULT_SIZE)
        self.browser = browser
        self.icons = IconBinder()
        self._disposed = False

        self.view = QWebEngineView(self)
        self.page = SitePage(browser.profile, self.view)
        self.view.setPage(self.page)
        self.view.setMinimumHeight(self.VIEW_MIN_HEIGHT)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        # heading
        head_icon = QLabel()
        head_icon.setFixedSize(36, 36)
        head_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icons.bind(head_icon, icon_name, "accent", 28)
        self.heading_label = label(heading, "title")
        self.message_label = label(message, "muted", wrap=True)
        self.message_label.setTextFormat(Qt.TextFormat.RichText)
        self.message_label.setVisible(bool(message))
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.addWidget(self.heading_label)
        text_col.addWidget(self.message_label)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(head_icon, 0, Qt.AlignmentFlag.AlignTop)
        head.addLayout(text_col, 1)
        root.addLayout(head)

        # status + extra (e.g. countdown) row
        self.status_line = StatusLine("", "busy")
        self.status_extra = QHBoxLayout()
        self.status_extra.setSpacing(8)
        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        # Centred: the row grows when a button (e.g. Reload) appears next to the status text.
        status_row.addWidget(self.status_line, 1, Qt.AlignmentFlag.AlignVCenter)
        status_row.addLayout(self.status_extra)
        root.addLayout(status_row)

        # browser frame: address line + load bar + view
        frame = QFrame()
        frame.setProperty("role", "card")
        self.browser_frame = frame
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.setSpacing(0)
        address = QWidget()
        address.setProperty("role", "transparent")
        address_layout = QHBoxLayout(address)
        address_layout.setContentsMargins(12, 8, 12, 8)
        address_layout.setSpacing(8)
        self._lock_icon = QLabel()
        self._lock_icon.setFixedSize(14, 14)
        self.host_label = label("", "muted")
        self.host_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        address_layout.addWidget(self._lock_icon)
        address_layout.addWidget(self.host_label, 1)
        frame_layout.addWidget(address)
        self.load_bar = QProgressBar()
        self.load_bar.setRange(0, 100)
        self.load_bar.setTextVisible(False)
        self.load_bar.setFixedHeight(2)
        self.load_bar.setStyleSheet("QProgressBar { max-height: 2px; min-height: 2px; border-radius: 0; }")
        frame_layout.addWidget(self.load_bar)
        # All children stay visible (StackAll): an overlay added by a subclass covers the view
        # without hiding it, so Chromium keeps rendering and running the page underneath.
        self.view_holder = QWidget()
        self.view_stack = QStackedLayout(self.view_holder)
        self.view_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.view_stack.addWidget(self.view)
        frame_layout.addWidget(self.view_holder, 1)
        root.addWidget(frame, 1)

        # footer
        self.footer = QHBoxLayout()
        self.footer.setSpacing(8)
        root.addLayout(self.footer)

        self.page.loadProgress.connect(self._on_progress)
        self.page.urlChanged.connect(self._on_url_changed)
        self._update_address(QUrl())

    # --- helpers for subclasses ------------------------------------------------------------
    def load(self, url: str) -> None:
        self._update_address(QUrl(url))
        self.page.load(QUrl(url))

    def set_status(self, text: str, kind: str = "busy") -> None:
        self.status_line.set_status(text, kind)

    def set_message(self, html_text: str) -> None:
        self.message_label.setText(html_text)
        self.message_label.setVisible(bool(html_text))

    def current_url(self) -> str:
        page = self.view.page()
        return page.url().toString() if page is not None else ""

    def open_current_in_system_browser(self, fallback: str = "") -> str:
        url = self.current_url()
        if not url.startswith(("http://", "https://")):
            url = fallback
        if url:
            open_url(url)
        return url

    def show_page(self, page: SitePage) -> None:
        """Show another page (e.g. a popup that turned out to be a real page) in the view."""
        if self.view.page() is page:
            return
        try:
            self.view.page().loadProgress.disconnect(self._on_progress)
            self.view.page().urlChanged.disconnect(self._on_url_changed)
        except (TypeError, RuntimeError):
            pass
        self.view.setPage(page)
        page.loadProgress.connect(self._on_progress)
        page.urlChanged.connect(self._on_url_changed)
        self._update_address(page.url())

    def pages(self) -> list[SitePage]:
        return [p for p in self.findChildren(SitePage) if alive(p)]

    def owns_page(self, page: Any) -> bool:
        if page is None or not alive(page):
            return False
        address = sip.unwrapinstance(page)
        return any(sip.unwrapinstance(p) == address for p in self.pages())

    def dispose(self) -> None:
        """Stop every page, hide and delete the dialog (safe to call twice)."""
        if self._disposed:
            return
        self._disposed = True
        for page in self.pages():
            try:
                page.triggerAction(QWebEnginePage.WebAction.Stop)
            except RuntimeError:
                pass
        self.hide()
        self.deleteLater()

    @property
    def disposed(self) -> bool:
        return self._disposed

    # --- internals -------------------------------------------------------------------------
    def _on_progress(self, value: int) -> None:
        self.load_bar.setValue(value)
        self.load_bar.setVisible(0 < value < 100)

    def _on_url_changed(self, url: QUrl) -> None:
        self._update_address(url)

    def _update_address(self, url: QUrl) -> None:
        host = url.host()
        secure = url.scheme() == "https"
        self.icons.bind(self._lock_icon, "shield" if secure else "globe", "success" if secure else "text_muted", 14)
        if host:
            path = url.path() if url.path() not in ("", "/") else ""
            if len(path) > 48:
                path = path[:45] + "…"
            self.host_label.setText(f"<b>{html.escape(host)}</b>{html.escape(path)}")
            self.host_label.setToolTip(url.toString())
        else:
            self.host_label.setText("Loading…")
            self.host_label.setToolTip("")
        repolish(self.host_label)


def connect_once(signal: Any, slot: Callable[..., Any]) -> Callable[[], None]:
    """Connect ``slot`` and return a function that disconnects it (idempotent, never raises)."""
    signal.connect(slot)
    state = {"connected": True}

    def disconnect() -> None:
        if not state["connected"]:
            return
        state["connected"] = False
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    return disconnect


def loading_status() -> Any:
    """``QWebEngineLoadingInfo.LoadStatus`` (None when WebEngine is unavailable)."""
    return QWebEngineLoadingInfo.LoadStatus if QWebEngineLoadingInfo is not None else None


def http_status_domain() -> Any:
    return QWebEngineLoadingInfo.ErrorDomain.HttpStatusCodeDomain if QWebEngineLoadingInfo is not None else None


def add_user_script(page: Any, name: str, source: str, *, at_creation: bool = False) -> None:
    """Run ``source`` in the isolated world of every main-frame document ``page`` loads.

    ``at_creation`` runs it as soon as ``<html>`` exists (before anything is
    painted); otherwise it runs at ``DOMContentLoaded``.
    """
    points = QWebEngineScript.InjectionPoint
    script = QWebEngineScript()
    script.setName(name)
    script.setSourceCode(source)
    script.setInjectionPoint(points.DocumentCreation if at_creation else points.DocumentReady)
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld.value)
    script.setRunsOnSubFrames(False)
    page.scripts().insert(script)


def remove_user_scripts(page: Any, name: str) -> None:
    collection = page.scripts()
    for script in collection.find(name):
        collection.remove(script)


def run_isolated_js(page: Any, source: str, callback: Callable[[Any], None] | None = None) -> None:
    """``page.runJavaScript`` in the isolated world (same DOM, none of the page's globals)."""
    world = QWebEngineScript.ScriptWorldId.ApplicationWorld.value
    if callback is None:
        page.runJavaScript(source, world)
    else:
        page.runJavaScript(source, world, callback)
