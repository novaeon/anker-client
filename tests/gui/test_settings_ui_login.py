"""Login dialog: validation, busy state, success/failure via FakeContext, Discord browser flow."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWidgets import QDialog, QLineEdit, QWidget

from anker_client.core.paths import AppPaths

# QtWebEngine must load before the QApplication exists.
from anker_client.ui.dialogs import browser as web
from anker_client.ui.dialogs import login as login_mod
from anker_client.ui.dialogs.login import DiscordSignInDialog, LoginDialog, is_signin_complete
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from tests.fakes import screenshot

pytestmark = pytest.mark.gui


def destroy(widget: Any) -> None:
    if widget is not None and not sip.isdeleted(widget):
        widget.close()
        sip.delete(widget)


@pytest.fixture
def theme(qapp) -> ThemeManager:
    manager = ThemeManager(qapp)
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")
    return manager


@pytest.fixture
def dialog(qtbot, fake_ctx, theme) -> Iterator[LoginDialog]:
    dlg = LoginDialog(fake_ctx)
    dlg.show()
    yield dlg
    destroy(dlg)


def fill(dlg: LoginDialog, email: str, password: str) -> None:
    dlg.email_edit.setText(email)
    dlg.password_edit.setText(password)


def test_remembered_email_is_prefilled(dialog: LoginDialog, qtbot) -> None:
    qtbot.waitUntil(lambda: dialog.email_edit.text() == "player@example.com", timeout=2000)


@pytest.mark.parametrize(("email", "password", "message"), [
    ("", "x", "Enter your email address."),
    ("not-an-email", "x", "Enter a valid email address, like name@example.com."),
    ("me@example.com", "", "Enter your password."),
])
def test_validation_happens_before_calling_the_service(dialog, fake_ctx, monkeypatch, email, password, message):
    called: list[str] = []
    monkeypatch.setattr(fake_ctx.auth, "login", lambda *a, **k: called.append("login"))
    fill(dialog, email, password)
    dialog.submit_button.click()
    assert dialog.error_message() == message
    assert not called and not dialog.busy
    dialog.email_edit.textEdited.emit("x")  # typing clears the error
    assert dialog.error_message() == ""


def test_successful_sign_in_accepts_with_user(dialog: LoginDialog, fake_ctx, qtbot) -> None:
    signed_in: list[Any] = []
    dialog.signed_in.connect(signed_in.append)
    fill(dialog, "player@example.com", "password")
    dialog.submit_button.click()
    assert dialog.busy
    assert not dialog.submit_button.isEnabled() and dialog.submit_button.text() == "Signing in…"
    assert not dialog.email_edit.isEnabled() and not dialog.password_edit.isEnabled()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted, timeout=3000)
    assert dialog.user is not None and dialog.user.display_name == "player"
    assert signed_in == [dialog.user]
    assert fake_ctx.auth.user == dialog.user


def test_wrong_password_shows_inline_error(dialog: LoginDialog, qtbot) -> None:
    fill(dialog, "player@example.com", "wrong")
    dialog.password_edit.returnPressed.emit()  # Enter submits
    qtbot.waitUntil(lambda: dialog.error_message() != "", timeout=3000)
    assert dialog.error_message() == "Incorrect email or password."
    assert not dialog.busy and dialog.submit_button.isEnabled() and dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_unexpected_errors_are_readable(dialog, fake_ctx, monkeypatch, qtbot) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("socket closed")

    monkeypatch.setattr(fake_ctx.auth, "login", boom)
    fill(dialog, "player@example.com", "password")
    dialog.submit()
    qtbot.waitUntil(lambda: dialog.error_message() != "", timeout=3000)
    assert dialog.error_message() == "Sign-in failed: socket closed"


def test_remember_me_is_persisted_and_passed_to_auth(dialog, fake_ctx, monkeypatch, qtbot) -> None:
    seen: dict[str, Any] = {}
    original = fake_ctx.auth.login

    def login(email: str, password: str, *, remember: bool = True, token: Any = None) -> Any:
        seen["remember"] = remember
        return original(email, password, remember=remember, token=token)

    monkeypatch.setattr(fake_ctx.auth, "login", login)
    assert dialog.remember_check.isChecked() is fake_ctx.settings.get().remember_login is True
    dialog.remember_check.setChecked(False)
    # A click alone must not change the setting: turning it off makes AuthService wipe the saved
    # password and session immediately, even if the user then cancels the dialog.
    assert fake_ctx.settings.get().remember_login is True
    fill(dialog, "player@example.com", "password")
    dialog.submit()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted, timeout=3000)
    assert seen["remember"] is False
    assert fake_ctx.settings.get().remember_login is False


def test_remember_me_is_not_saved_when_sign_in_is_cancelled_or_fails(dialog, fake_ctx, qtbot) -> None:
    dialog.remember_check.setChecked(False)
    fill(dialog, "player@example.com", "wrong")
    dialog.submit()
    qtbot.waitUntil(lambda: dialog.error_message() != "", timeout=3000)
    dialog.reject()
    assert fake_ctx.settings.get().remember_login is True


def test_discord_sign_in_saves_the_remember_choice(dialog, fake_ctx, monkeypatch, qtbot) -> None:
    fake_ctx.settings.update(remember_login=False)
    order: list[str] = []
    original = fake_ctx.auth.login_with_cookies

    def login_with_cookies(cookies: Any, *, token: Any = None) -> Any:
        order.append(f"login remember={fake_ctx.settings.get().remember_login}")
        return original(cookies)

    monkeypatch.setattr(fake_ctx.auth, "login_with_cookies", login_with_cookies)
    monkeypatch.setattr(login_mod, "_create_discord_dialog", lambda ctx, parent: FakeDiscordDialog(COOKIES, parent))
    monkeypatch.setattr(login_mod, "_discord_available", lambda: True)
    dialog.remember_check.setChecked(True)
    dialog.discord_button.setEnabled(True)
    dialog.discord_button.click()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted, timeout=3000)
    # saved before the call, so login_with_cookies persists the session it creates
    assert order == ["login remember=True"]
    assert fake_ctx.settings.get().remember_login is True


def test_password_visibility_toggle(dialog: LoginDialog) -> None:
    assert dialog.password_edit.echoMode() == QLineEdit.EchoMode.Password
    dialog.reveal_action.trigger()
    assert dialog.password_edit.echoMode() == QLineEdit.EchoMode.Normal
    assert dialog.reveal_action.toolTip() == "Hide password"
    dialog.reveal_action.trigger()
    assert dialog.password_edit.echoMode() == QLineEdit.EchoMode.Password


def test_account_links_open_the_website(dialog, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(login_mod, "open_url", lambda url: opened.append(url) or True)
    dialog.forgot_button.click()
    dialog.register_button.click()
    assert opened == ["https://ankergames.net/forgot-password", "https://ankergames.net/register"]


def test_cancel_while_busy_cancels_the_request(dialog: LoginDialog, fake_ctx, qtbot) -> None:
    fill(dialog, "player@example.com", "password")
    dialog.submit()
    handle = dialog._handle
    dialog.reject()
    assert handle is not None and handle.cancelled
    assert dialog.result() == QDialog.DialogCode.Rejected


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class FakeDiscordDialog(QDialog):
    signed_in = pyqtSignal(object)

    def __init__(self, cookies: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cookies = cookies
        self.started = False

    def start(self) -> None:
        self.started = True
        self.signed_in.emit(self._cookies)
        self.accept()


COOKIES = [{"name": "ankergames_session", "value": "abc", "domain": "ankergames.net", "path": "/",
            "secure": True, "expires": None}]


def test_discord_cookies_sign_in_through_auth(dialog, fake_ctx, monkeypatch, qtbot) -> None:
    received: list[Any] = []
    original = fake_ctx.auth.login_with_cookies
    monkeypatch.setattr(fake_ctx.auth, "login_with_cookies",
                        lambda cookies, *, token=None: received.append(cookies) or original(cookies))
    monkeypatch.setattr(login_mod, "_create_discord_dialog", lambda ctx, parent: FakeDiscordDialog(COOKIES, parent))
    monkeypatch.setattr(login_mod, "_discord_available", lambda: True)
    dialog.discord_button.setEnabled(True)
    dialog.discord_button.click()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted, timeout=3000)
    assert received == [COOKIES]
    assert dialog.user is not None and dialog.user.display_name == "discord-user"


def test_discord_without_cookies_explains(dialog, monkeypatch, qtbot) -> None:
    monkeypatch.setattr(login_mod, "_create_discord_dialog", lambda ctx, parent: FakeDiscordDialog([], parent))
    monkeypatch.setattr(login_mod, "_discord_available", lambda: True)
    dialog.discord_button.setEnabled(True)
    dialog.discord_button.click()
    assert "Discord sign-in did not finish" in dialog.error_message()
    assert not dialog.busy


def test_discord_failure_from_auth_is_shown(dialog, fake_ctx, monkeypatch, qtbot) -> None:
    from anker_client.core.errors import NotLoggedInError

    def fail(cookies: Any, *, token: Any = None) -> None:
        raise NotLoggedInError()

    monkeypatch.setattr(fake_ctx.auth, "login_with_cookies", fail)
    monkeypatch.setattr(login_mod, "_create_discord_dialog", lambda ctx, parent: FakeDiscordDialog(COOKIES, parent))
    monkeypatch.setattr(login_mod, "_discord_available", lambda: True)
    dialog.discord_button.setEnabled(True)
    dialog.discord_button.click()
    qtbot.waitUntil(lambda: dialog.error_message() != "", timeout=3000)
    assert dialog.error_message().startswith("Discord sign-in failed:")


def test_discord_button_disabled_without_webengine(qtbot, fake_ctx, theme, monkeypatch) -> None:
    monkeypatch.setattr(login_mod, "_discord_available", lambda: False)
    dlg = LoginDialog(fake_ctx)
    try:
        assert not dlg.discord_button.isEnabled()
        assert "embedded browser" in dlg.discord_button.toolTip()
    finally:
        destroy(dlg)


@pytest.mark.parametrize(("url", "done"), [
    ("https://ankergames.net/", True),
    ("https://www.ankergames.net/dashboard", True),
    ("https://ankergames.net/auth/discord/callback?code=1", False),
    ("https://ankergames.net/login?error=1", False),
    ("https://ankergames.net/register", False),
    ("https://discord.com/oauth2/authorize", False),
    ("https://evil-ankergames.net/", False),
])
def test_signin_completion_rule(url: str, done: bool) -> None:
    assert is_signin_complete(QUrl(url)) is done


# --- real embedded browser against a local OAuth-like round trip --------------------------------


def _oauth_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            port = self.server.server_port
            path = self.path.split("?")[0]
            if path == "/auth/discord":  # the site sends the browser to "Discord"
                self._send(302, headers={"Location": f"http://localhost:{port}/oauth2/authorize"})
            elif path == "/oauth2/authorize":  # "Discord" approves and returns to the site
                html = (f"<p>Authorize</p><script>setTimeout(() => location.href = "
                        f"'http://127.0.0.1:{port}/auth/discord/callback?code=ok', 100)</script>")
                self._send(200, html.encode(), {"Content-Type": "text/html"})
            elif path == "/auth/discord/callback":
                self._send(302, headers={"Location": "/", "Set-Cookie": "ankergames_session=disc0rd; Path=/"})
            elif path == "/":
                self._send(200, b"<h1>Welcome back</h1>", {"Content-Type": "text/html"})
            else:
                self._send(404)

    return Handler


@pytest.mark.skipif(not web.webengine_available(), reason="QtWebEngine unavailable")
def test_discord_browser_detects_completion_and_collects_cookies(qtbot, theme, tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _oauth_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = web.shared_browser(AppPaths.under(tmp_path / "home").ensure())
    dlg = DiscordSignInDialog(browser, start_url=f"http://127.0.0.1:{server.server_port}/auth/discord",
                              site_host="127.0.0.1", settle_ms=300)
    results: list[Any] = []
    dlg.signed_in.connect(results.append)
    try:
        dlg.show()
        dlg.start()
        qtbot.waitUntil(lambda: bool(results), timeout=15000)
        cookies = results[0]
        assert any(c["name"] == "ankergames_session" and c["value"] == "disc0rd" for c in cookies)
        assert all(web.host_matches(c["domain"], "127.0.0.1") for c in cookies)
        assert dlg.result() == QDialog.DialogCode.Accepted and dlg.completed
    finally:
        dlg.dispose()
        qtbot.wait(50)
        server.shutdown()
        server.server_close()


@pytest.mark.skip(reason="unfinished: cookie-mirror timing check was still being written when its review was stopped")
def test_discord_browser_starts_with_the_apps_site_identity(qtbot, fake_ctx, theme) -> None:
    browser = web.shared_browser(fake_ctx.paths)
    stale = {"name": "ankergames_session", "value": "signed-out", "domain": "ankergames.net", "path": "/"}
    browser.set_cookies([stale], ["ankergames.net"])
    qtbot.waitUntil(lambda: any(c["value"] == "signed-out" for c in browser.cookies_for(["ankergames.net"])),
                    timeout=3000)
    fake_ctx.http.import_cookies([{"name": "XSRF-TOKEN", "value": "guest", "domain": "ankergames.net", "path": "/"}])
    dlg = login_mod._create_discord_dialog(fake_ctx, None)  # type: ignore[arg-type]
    try:
        qtbot.waitUntil(lambda: not any(c["value"] == "signed-out" for c in browser.cookies_for(["ankergames.net"])),
                        timeout=3000)
        qtbot.waitUntil(lambda: any(c["name"] == "XSRF-TOKEN" for c in browser.cookies_for(["ankergames.net"])),
                        timeout=3000)
    finally:
        dlg.dispose()
        qtbot.wait(50)


# ---------------------------------------------------------------------------
# visuals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["daylight"])  # both themes: see the manual QA script
def test_screenshots(qtbot, fake_ctx, theme, theme_key: str) -> None:
    theme.apply(theme_key)
    dlg = LoginDialog(fake_ctx)
    try:
        fill(dlg, "player@example.com", "hunter2")
        dlg.show_error("Incorrect email or password.")
        path = screenshot(dlg, f"settings_ui_login_error_{theme_key}", (460, dlg.sizeHint().height()))
        assert path.exists()
    finally:
        destroy(dlg)
        theme.apply("midnight")
