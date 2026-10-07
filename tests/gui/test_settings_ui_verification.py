"""WebEngineVerifier end-to-end against a local HTTP server (real QtWebEngine, offscreen)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

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

# Imported at module level on purpose: QtWebEngine must load before the QApplication exists.
from anker_client.ui.dialogs import browser as web
from anker_client.ui.dialogs import verification as ver

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(not web.webengine_available(), reason=f"QtWebEngine unavailable: {web.webengine_import_error()}"),
]

FILE_BODY = b"PK" + b"\x00" * 4094


# ---------------------------------------------------------------------------
# local "site": ticket pages on 127.0.0.1, an "external host" on localhost
# ---------------------------------------------------------------------------


@dataclass
class SiteServer:
    server: ThreadingHTTPServer
    requests: list[tuple[str, str]] = field(default_factory=list)  # (path, Cookie header)

    @property
    def port(self) -> int:
        return self.server.server_port

    def site(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def external(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"

    def cookies_sent_to(self, path: str) -> list[str]:
        return [cookie for p, cookie in self.requests if p == path]


def _page(script: str, body: str = "") -> str:
    return (
        "<!doctype html><html><head><title>Download</title></head>"
        "<body style='font-family:sans-serif;padding:24px'>"
        "<h2>Your download is ready</h2><p>Preparing your file…</p>"
        f"{body}<script>{script}</script></body></html>"
    )


# Mirrors the real ticket page: the widget sits inside an Alpine ``x-show`` wrapper that is
# hidden by default, inside a card with ``backdrop-filter`` (which would make a fixed
# child position against the card), and Turnstile renders into it only after load.
_WIDGET_BODY = (
    "<div style='backdrop-filter:blur(4px);transform:translateX(0);margin-top:400px;padding:16px'>"
    "<div x-show='needsChallenge' style='display:none'>"
    "<div class='ag-turnstile' data-ag-turnstile data-sitekey='0xTEST' data-theme='auto'></div>"
    "</div></div>"
)
_RENDER_WIDGET = (
    "setTimeout(() => { document.querySelector('[data-ag-turnstile]').innerHTML ="
    " \"<div style='box-sizing:border-box;width:300px;height:65px;border:1px solid #888;border-radius:4px'>Verify you are human</div>\";"
    " }, 300);"
)


def _make_handler(state: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str) -> None:
            self._send(200, text.encode(), {"Content-Type": "text/html; charset=utf-8"})

        def do_GET(self) -> None:
            port = self.server.server_port
            state["requests"].append((self.path.split("?")[0], self.headers.get("Cookie") or ""))
            path = self.path.split("?")[0]
            external = f"http://localhost:{port}"
            if path == "/ticket/click":
                self._html(_page("setTimeout(() => document.getElementById('go').click(), 150);",
                                 "<a id='go' href='/download-file/abc'>Download now</a>"))
            elif path == "/ticket/navigate":
                self._html(_page("setTimeout(() => { location.href = '/download-file/xyz'; }, 150);"))
            elif path == "/ticket/external":
                self._html(_page(f"setTimeout(() => {{ location.href = '{external}/provider/file'; }}, 150);"))
            elif path == "/ticket/popup-download":
                self._html(_page(f"setTimeout(() => window.open('{external}/cdn/popup.7z'), 150);"))
            elif path == "/ticket/popup-external":
                self._html(_page(f"setTimeout(() => window.open('{external}/provider/page'), 150);"))
            elif path == "/ticket/popup-site":
                self._html(_page("setTimeout(() => window.open('/ticket/click'), 150);"))
            elif path == "/ticket/cdn-elsewhere":  # the real CDN lives on another host than the site
                self._html(_page("setTimeout(() => { location.href = '/download-file/far'; }, 150);"))
            elif path == "/ticket/external-error":
                self._html(_page(f"setTimeout(() => {{ location.href = '{external}/gone'; }}, 150);"))
            elif path == "/ticket/wait":
                self._html(_page("", "<div style='height:65px;width:300px;border:1px solid #ccc;border-radius:4px;"
                                     "display:flex;align-items:center;padding-left:12px'>Verify you are human</div>"))
            elif path == "/ticket/widget":
                self._html(_page(_RENDER_WIDGET, _WIDGET_BODY))
            elif path == "/ticket/widget-slow":  # the page keeps loading for a while before the widget
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><html class='dark'><head><title>Download</title></head><body>"
                                 b"<h2>Your download is ready</h2>" + b"<p>Preparing your file</p>" * 40)
                self.wfile.flush()
                time.sleep(1.5)
                self.wfile.write((_WIDGET_BODY + f"<script>{_RENDER_WIDGET}</script></body></html>").encode())
            elif path == "/ticket/widget-never":  # widget markup, but Turnstile never renders
                self._html(_page("", _WIDGET_BODY))
            elif path == "/ticket/widget-download":
                self._html(_page(_RENDER_WIDGET + "setTimeout(() => { location.href = '/download-file/wd'; }, 1500);",
                                 _WIDGET_BODY))
            elif path == "/download-file/far":
                self._send(302, b"", {"Location": f"{external}/cdn/far.7z?sig=cafe"})
            elif path.startswith("/download-file/"):
                self._send(302, b"", {"Location": f"/cdn/game.zip?sig=deadbeef&ticket={path.rsplit('/', 1)[-1]}"})
            elif path.startswith("/cdn/"):
                name = path.rsplit("/", 1)[-1]
                self._send(200, FILE_BODY, {
                    "Content-Type": "application/zip",
                    "Content-Disposition": f'attachment; filename="{name}"',
                })
            elif path.startswith("/provider/"):
                self._html("<html><body><h1>External file host</h1></body></html>")
            else:
                self._send(404, b"not found", {"Content-Type": "text/plain"})

    return Handler


@pytest.fixture(scope="module")
def site() -> Iterator[SiteServer]:
    state: dict[str, Any] = {"requests": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="test-site", daemon=True)
    thread.start()
    yield SiteServer(server, state["requests"])
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def web_paths(tmp_path_factory: pytest.TempPathFactory) -> AppPaths:
    # One profile for the whole module (each profile spins up Chromium storage).
    return AppPaths.under(Path(tmp_path_factory.mktemp("webengine-home"))).ensure()


class RecordingHttp:
    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self.cookies = cookies or []
        self.user_agent = ""

    def export_cookies(self) -> list[dict[str, Any]]:
        return list(self.cookies)


@pytest.fixture
def verifier(qtbot, web_paths: AppPaths) -> Iterator[ver.WebEngineVerifier]:
    http = RecordingHttp([{"name": "ankergames_session", "value": "s3cret", "domain": "127.0.0.1", "path": "/",
                           "secure": False, "expires": None}])
    verifier = ver.WebEngineVerifier(http, web_paths)  # type: ignore[arg-type]
    yield verifier
    verifier.shutdown()
    qtbot.wait(100)  # let deleteLater() of dialogs/pages run


class VerifyCall:
    """Runs ``verify`` on a worker thread like the download manager does."""

    def __init__(self, verifier: ver.WebEngineVerifier, request: VerificationRequest,
                 token: CancelToken | None = None) -> None:
        self.token = token or CancelToken()
        self.result: VerificationResult | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(verifier, request), daemon=True)
        self.thread.start()

    def _run(self, verifier: ver.WebEngineVerifier, request: VerificationRequest) -> None:
        try:
            self.result = verifier.verify(request, token=self.token)
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()

    def wait(self, qtbot, timeout: int = 25_000) -> None:
        qtbot.waitUntil(self.done.is_set, timeout=timeout)
        self.thread.join(timeout=5)


def request_for(url: str, *, timeout: float = 60.0, title: str = "Hollow Knight") -> VerificationRequest:
    return VerificationRequest(ticket_url=url, title=title, job_id="job-1", timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["click", "navigate"])
def test_download_started_by_page_is_captured(qtbot, verifier, site: SiteServer, kind: str) -> None:
    call = VerifyCall(verifier, request_for(site.site(f"/ticket/{kind}")))
    call.wait(qtbot)

    assert call.error is None, call.error
    assert call.result is not None
    assert call.result.url.startswith(site.site("/cdn/game.zip?sig=deadbeef"))
    assert call.result.filename == "game.zip"
    assert call.result.size == len(FILE_BODY)
    assert call.result.mime_type == "application/zip"
    assert call.result.external_url == ""
    # the app's session cookie was copied into the browser before the ticket page loaded
    assert any("ankergames_session=s3cret" in c for c in site.cookies_sent_to(f"/ticket/{kind}"))
    qtbot.waitUntil(lambda: verifier.active_dialog() is None, timeout=3000)


def test_redirect_to_a_cdn_on_another_host_is_a_download_not_an_external_host(qtbot, verifier, site) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/cdn-elsewhere")))
    call.wait(qtbot)
    assert call.error is None, call.error
    assert call.result is not None
    assert call.result.url == site.external("/cdn/far.7z?sig=cafe")
    assert call.result.filename == "far.7z" and call.result.external_url == ""


def test_http_error_from_a_foreign_host_is_a_verification_error(qtbot, verifier, site: SiteServer) -> None:
    url = site.site("/ticket/external-error")
    call = VerifyCall(verifier, request_for(url))
    call.wait(qtbot)
    assert isinstance(call.error, VerificationError) and not isinstance(call.error, VerificationTimeout)
    assert "404" in call.error.message and call.error.ticket_url == url


def test_cancelling_a_queued_request_never_opens_its_window(qtbot, verifier, site: SiteServer) -> None:
    started: list[str] = []
    verifier.request_started.connect(lambda req: started.append(req.title))
    active = VerifyCall(verifier, request_for(site.site("/ticket/wait"), title="Active"))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    queued = VerifyCall(verifier, request_for(site.site("/ticket/wait"), title="Queued"))
    qtbot.waitUntil(lambda: verifier.pending_count() == 2, timeout=3000)
    queued.token.cancel()
    queued.wait(qtbot, timeout=3000)
    assert isinstance(queued.error, OperationCancelled)
    assert verifier.active_dialog() is not None and verifier.active_dialog().request.title == "Active"
    active.token.cancel()
    active.wait(qtbot)
    qtbot.wait(200)
    assert started == ["Active"] and verifier.active_dialog() is None and verifier.pending_count() == 0


def test_navigation_to_external_host_reports_external_url(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/external")))
    call.wait(qtbot)
    assert call.error is None, call.error
    assert call.result == VerificationResult(external_url=site.external("/provider/file"))


def test_popup_download_is_captured_from_probe_page(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/popup-download")))
    call.wait(qtbot)
    assert call.error is None, call.error
    assert call.result is not None
    assert call.result.url == site.external("/cdn/popup.7z")
    assert call.result.filename == "popup.7z"


def test_popup_to_external_page_reports_external_url(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/popup-external")))
    call.wait(qtbot)
    assert call.error is None, call.error
    assert call.result == VerificationResult(external_url=site.external("/provider/page"))


def test_popup_with_site_page_is_shown_and_its_download_captured(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/popup-site")))
    call.wait(qtbot)
    assert call.error is None, call.error
    assert call.result is not None and call.result.filename == "game.zip"


def test_timeout_raises_verification_timeout(qtbot, verifier, site: SiteServer) -> None:
    url = site.site("/ticket/wait")
    call = VerifyCall(verifier, request_for(url, timeout=1.0))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    dialog = verifier.active_dialog()
    assert dialog is not None and "left" in dialog.countdown_label.text()
    call.wait(qtbot)
    assert isinstance(call.error, VerificationTimeout)
    assert call.error.ticket_url == url
    qtbot.waitUntil(lambda: verifier.active_dialog() is None, timeout=3000)


def test_cancelling_the_worker_token_closes_the_dialog(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/wait")))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    dialog = verifier.active_dialog()
    call.token.cancel("pause")
    call.wait(qtbot)
    assert isinstance(call.error, OperationCancelled)
    qtbot.waitUntil(lambda: verifier.active_dialog() is None, timeout=3000)
    assert dialog is not None and dialog.finished


def test_cancel_button_raises_verification_cancelled(qtbot, verifier, site: SiteServer) -> None:
    url = site.site("/ticket/wait")
    call = VerifyCall(verifier, request_for(url))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    verifier.active_dialog().cancel_button.click()
    call.wait(qtbot)
    assert isinstance(call.error, VerificationCancelled)
    assert call.error.ticket_url == url


def test_open_in_browser_hands_off_the_ticket(qtbot, verifier, site: SiteServer, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url: opened.append(url) or True)
    url = site.site("/ticket/wait")
    call = VerifyCall(verifier, request_for(url))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    verifier.active_dialog().browser_button.click()
    call.wait(qtbot)
    assert call.result == VerificationResult(external_url=url)
    assert opened == [url]


def test_requests_are_serialised_fifo(qtbot, verifier, site: SiteServer) -> None:
    started: list[str] = []
    concurrent: list[int] = []
    verifier.request_started.connect(lambda req: (started.append(req.title), concurrent.append(
        sum(1 for w in ver.QApplication.topLevelWidgets() if isinstance(w, ver.VerificationDialog) and w.isVisible()))))
    first = VerifyCall(verifier, request_for(site.site("/ticket/click"), title="First"))
    qtbot.waitUntil(lambda: verifier.pending_count() == 1, timeout=3000)
    second = VerifyCall(verifier, request_for(site.site("/ticket/navigate"), title="Second"))
    first.wait(qtbot)
    second.wait(qtbot)
    assert first.error is None and second.error is None
    assert started == ["First", "Second"]
    assert max(concurrent) == 1


def test_shutdown_unblocks_every_waiter(qtbot, verifier, site: SiteServer) -> None:
    active = VerifyCall(verifier, request_for(site.site("/ticket/wait"), title="Active"))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    queued = VerifyCall(verifier, request_for(site.site("/ticket/wait"), title="Queued"))
    qtbot.waitUntil(lambda: verifier.pending_count() == 2, timeout=3000)
    verifier.shutdown()
    active.wait(qtbot)
    queued.wait(qtbot)
    assert isinstance(active.error, VerificationCancelled)
    assert isinstance(queued.error, VerificationCancelled)
    assert verifier.active_dialog() is None
    # after shutdown new requests fail fast
    with pytest.raises(VerificationUnavailable):
        verifier.verify(request_for(site.site("/ticket/wait")), token=CancelToken())


def test_stale_site_cookies_are_removed_before_loading(qtbot, web_paths: AppPaths, site: SiteServer) -> None:
    browser = web.shared_browser(web_paths)
    browser.set_cookies([{"name": "stale_session", "value": "old", "domain": "127.0.0.1", "path": "/"}], ["127.0.0.1"])
    qtbot.waitUntil(lambda: any(c["name"] == "stale_session" for c in browser.cookies_for(["127.0.0.1"])), timeout=3000)
    verifier = ver.WebEngineVerifier(RecordingHttp([]), web_paths)  # type: ignore[arg-type]
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/navigate")))
        call.wait(qtbot)
        assert call.error is None
        assert not any("stale_session" in c for c in site.cookies_sent_to("/ticket/navigate"))
    finally:
        verifier.shutdown()
        qtbot.wait(50)


def test_verify_on_gui_thread_is_refused(qtbot, verifier, site: SiteServer) -> None:
    with pytest.raises(VerificationError):
        verifier.verify(request_for(site.site("/ticket/wait")), token=CancelToken())


def test_unavailable_webengine_raises(qtbot, verifier, monkeypatch) -> None:
    monkeypatch.setattr(web, "_IMPORT_ERROR", "ImportError: simulated")
    assert not ver.webengine_available()
    assert not verifier.available
    with pytest.raises(VerificationUnavailable) as info:
        verifier.verify(request_for("https://ankergames.net/download/x/y"), token=CancelToken())
    assert info.value.ticket_url == "https://ankergames.net/download/x/y"
    assert ver.browser_user_agent(AppPaths.default()) == ""


def test_browser_user_agent_matches_chromium_without_qtwebengine_token(qtbot, web_paths: AppPaths) -> None:
    ua = ver.browser_user_agent(web_paths)
    assert "QtWebEngine" not in ua
    assert "Chrome/" in ua and ua.startswith("Mozilla/5.0")
    assert web.shared_browser(web_paths).profile.httpUserAgent() == ua


def test_worker_gives_up_when_the_dialog_overruns(qtbot, web_paths: AppPaths, site: SiteServer, monkeypatch) -> None:
    verifier = ver.WebEngineVerifier(RecordingHttp(), web_paths, grace_seconds=0.0)  # type: ignore[arg-type]
    # A dialog whose own clock never advances simulates a stuck GUI-side timer.
    monkeypatch.setattr(ver.VerificationDialog, "remaining", lambda self: 99.0)
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/wait"), timeout=1.0))
        call.wait(qtbot, timeout=10_000)
        assert isinstance(call.error, VerificationTimeout)
        qtbot.waitUntil(lambda: verifier.active_dialog() is None, timeout=3000)
    finally:
        verifier.shutdown()
        qtbot.wait(50)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_clean_user_agent_removes_only_the_qtwebengine_token() -> None:
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "QtWebEngine/6.10.2 Chrome/134.0.0.0 Safari/537.36")
    assert web.clean_user_agent(ua) == ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")


@pytest.mark.parametrize(("host", "domain", "expected"), [
    ("ankergames.net", "ankergames.net", True),
    ("www.ankergames.net", ".ankergames.net", True),
    ("evilankergames.net", "ankergames.net", False),
    ("ankergames.net.evil.com", "ankergames.net", False),
    ("", "ankergames.net", False),
])
def test_host_matches(host: str, domain: str, expected: bool) -> None:
    assert web.host_matches(host, domain) is expected


def test_cookie_dict_round_trip() -> None:
    data = {"name": "XSRF-TOKEN", "value": "abc%3D", "domain": ".ankergames.net", "path": "/", "secure": True,
            "expires": 2_000_000_000}
    cookie = web.cookie_from_dict(data)
    assert cookie is not None
    assert web.cookie_to_dict(cookie) == data
    assert web.cookie_origin(cookie).toString() == "https://ankergames.net/"
    assert web.cookie_from_dict({"name": "", "domain": "x"}) is None


# ---------------------------------------------------------------------------
# widget-only view
# ---------------------------------------------------------------------------


def run_js(qtbot, page: Any, source: str) -> Any:
    box: dict[str, Any] = {}
    web.run_isolated_js(page, source, lambda value: box.setdefault("value", value))
    qtbot.waitUntil(lambda: "value" in box, timeout=5000)
    return box["value"]


_LAYOUT_JS = """(() => {
  const el = document.querySelector('[data-ag-turnstile]');
  const r = el ? el.getBoundingClientRect() : {left: 0, top: 0, width: 0, height: 0};
  return {
    widget: el ? getComputedStyle(el.firstElementChild || el).visibility : '',
    heading: getComputedStyle(document.querySelector('h2')).visibility,
    theme: el ? el.dataset.theme : '',
    full: document.documentElement.hasAttribute('data-ac-full'),
    cx: r.left + r.width / 2, cy: r.top + r.height / 2, w: r.width, h: r.height,
    vw: innerWidth, vh: innerHeight,
  };
})()"""


def test_widget_only_view_shows_just_the_centred_widget(qtbot, verifier, site: SiteServer) -> None:
    from anker_client.ui.theme import palette

    call = VerifyCall(verifier, request_for(site.site("/ticket/widget")))
    try:
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        assert dialog.loading_overlay.isVisible()  # covers the view until Turnstile has drawn
        qtbot.waitUntil(lambda: dialog.widget_rendered, timeout=10_000)
        assert not dialog.loading_overlay.isVisible()
        state = run_js(qtbot, dialog.page, _LAYOUT_JS)
        assert state["h"] == 65
        assert dialog.widget_only and dialog.full_page_button.isVisible()
        assert state["widget"] == "visible"
        assert state["heading"] == "hidden"
        assert state["theme"] == ("dark" if palette.current().dark else "light")
        assert abs(state["cx"] - state["vw"] / 2) <= 2
        assert abs(state["cy"] - state["vh"] / 2) <= 2
        qtbot.waitUntil(lambda: dialog.view.height() == 65 + 2 * ver._WIDGET_PADDING, timeout=3000)
        assert dialog.height() < dialog.MIN_SIZE[1]
    finally:
        call.token.cancel()
        call.wait(qtbot)


def test_page_is_hidden_while_it_is_still_loading(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/widget-slow")))
    try:
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        probe = """(() => {
          const h2 = document.querySelector('h2');
          return {
            loading: document.readyState === 'loading',
            pending: document.documentElement.hasAttribute('data-ac-pending'),
            heading: h2 ? getComputedStyle(h2).visibility : '',
          };
        })()"""
        qtbot.waitUntil(lambda: run_js(qtbot, dialog.page, probe)["heading"] != "", timeout=5000)
        state = run_js(qtbot, dialog.page, probe)
        assert state == {"loading": True, "pending": True, "heading": "hidden"}
        assert dialog.loading_overlay.isVisible()
        qtbot.waitUntil(lambda: dialog.widget_rendered, timeout=10_000)
    finally:
        call.token.cancel()
        call.wait(qtbot)


def test_widget_that_never_renders_falls_back_to_the_full_page(qtbot, web_paths: AppPaths, site: SiteServer) -> None:
    verifier = ver.WebEngineVerifier(RecordingHttp(), web_paths, widget_wait_seconds=0.5)  # type: ignore[arg-type]
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/widget-never")))
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        qtbot.waitUntil(lambda: not dialog.widget_only, timeout=10_000)
        assert dialog.widget_seen and not dialog.widget_rendered
        assert not dialog.loading_overlay.isVisible()
        qtbot.waitUntil(lambda: run_js(qtbot, dialog.page, _LAYOUT_JS)["heading"] == "visible", timeout=5000)
        call.token.cancel()
        call.wait(qtbot)
    finally:
        verifier.shutdown()
        qtbot.wait(100)


def test_widget_only_view_still_captures_the_download(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/widget-download")))
    qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
    dialog = verifier.active_dialog()
    qtbot.waitUntil(lambda: dialog.widget_seen or call.done.is_set(), timeout=10_000)
    assert dialog.widget_only
    call.wait(qtbot)
    assert call.error is None
    assert call.result is not None and "/cdn/game.zip" in call.result.url


def test_show_full_page_reveals_the_whole_page(qtbot, verifier, site: SiteServer) -> None:
    call = VerifyCall(verifier, request_for(site.site("/ticket/widget")))
    try:
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        qtbot.waitUntil(lambda: dialog.widget_seen, timeout=10_000)
        dialog.full_page_button.click()
        assert not dialog.widget_only
        assert not dialog.full_page_button.isVisible()
        assert not dialog.loading_overlay.isVisible()
        qtbot.waitUntil(lambda: run_js(qtbot, dialog.page, _LAYOUT_JS)["heading"] == "visible", timeout=5000)
        assert run_js(qtbot, dialog.page, _LAYOUT_JS)["full"]
        assert dialog.width() >= dialog.MIN_SIZE[0] and dialog.height() >= dialog.MIN_SIZE[1]
    finally:
        call.token.cancel()
        call.wait(qtbot)


def test_page_without_widget_falls_back_to_the_full_page(qtbot, web_paths: AppPaths, site: SiteServer) -> None:
    verifier = ver.WebEngineVerifier(RecordingHttp(), web_paths, widget_wait_seconds=0.3)  # type: ignore[arg-type]
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/wait")))
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        qtbot.waitUntil(lambda: not dialog.widget_only, timeout=10_000)
        assert not dialog.widget_seen
        qtbot.waitUntil(lambda: run_js(qtbot, dialog.page, _LAYOUT_JS)["heading"] == "visible", timeout=5000)
        assert dialog.width() >= dialog.MIN_SIZE[0]
        call.token.cancel()
        call.wait(qtbot)
    finally:
        verifier.shutdown()
        qtbot.wait(100)


def test_full_page_mode_can_be_chosen_up_front(qtbot, web_paths: AppPaths, site: SiteServer) -> None:
    verifier = ver.WebEngineVerifier(RecordingHttp(), web_paths, widget_only=False)  # type: ignore[arg-type]
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/widget")))
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        assert not dialog.widget_only and not dialog.full_page_button.isVisible()
        assert not dialog.loading_overlay.isVisible()
        qtbot.waitUntil(lambda: "Waiting" in dialog.status_line.text(), timeout=5000)
        assert run_js(qtbot, dialog.page, _LAYOUT_JS)["heading"] == "visible"
        call.token.cancel()
        call.wait(qtbot)
    finally:
        verifier.shutdown()
        qtbot.wait(100)


def test_format_clock() -> None:
    assert ver._format_clock(185) == "3:05"
    assert ver._format_clock(-3) == "0:00"


# ---------------------------------------------------------------------------
# visuals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["midnight", "daylight"])
def test_screenshots(qtbot, verifier, site: SiteServer, theme_key: str) -> None:
    from anker_client.ui.theme.manager import ThemeManager
    from tests.fakes import screenshot

    theme = ThemeManager(ver.QApplication.instance())
    theme.apply(theme_key)
    loading = VerifyCall(verifier, request_for(site.site("/ticket/widget-never"), title="Hollow Knight: Silksong"))
    try:
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        dialog = verifier.active_dialog()
        qtbot.waitUntil(lambda: dialog.widget_seen, timeout=10_000)
        size = (dialog.width(), dialog.height())
        assert screenshot(dialog, f"settings_ui_verification_loading_{theme_key}", size).exists()
        assert dialog.loading_overlay.isVisible()
    finally:
        loading.token.cancel()
        loading.wait(qtbot)
    call = VerifyCall(verifier, request_for(site.site("/ticket/widget"), title="Hollow Knight: Silksong"))
    try:
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None and not verifier.active_dialog().finished,
                        timeout=5000)
        dialog = verifier.active_dialog()
        qtbot.waitUntil(lambda: dialog.widget_rendered and "Waiting" in dialog.status_line.text(), timeout=10_000)
        size = (dialog.width(), dialog.height())
        assert screenshot(dialog, f"settings_ui_verification_widget_{theme_key}", size).exists()
        dialog.show_full_page()
        assert screenshot(dialog, f"settings_ui_verification_{theme_key}", (980, 760)).exists()
    finally:
        call.token.cancel()
        call.wait(qtbot)
        theme.apply("midnight")


def test_dialog_is_not_owned_by_a_minimized_window(qtbot, web_paths: AppPaths, site: SiteServer) -> None:
    from PyQt6.QtWidgets import QMainWindow

    window = QMainWindow()
    window.show()
    window.showMinimized()
    verifier = ver.WebEngineVerifier(RecordingHttp(), web_paths, window)  # type: ignore[arg-type]
    try:
        call = VerifyCall(verifier, request_for(site.site("/ticket/wait")))
        qtbot.waitUntil(lambda: verifier.active_dialog() is not None, timeout=5000)
        assert verifier.active_dialog().parent() is None
        call.token.cancel()
        call.wait(qtbot)
    finally:
        verifier.shutdown()
        qtbot.wait(50)
        window.close()
        window.deleteLater()
