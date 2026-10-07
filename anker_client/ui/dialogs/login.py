"""Sign-in dialog (email/password + "Continue with Discord" via embedded browser).

* Email + password (show/hide toggle), "Remember me" (passed to
  ``auth.login(remember=…)`` and persisted as ``settings.remember_login`` once
  a sign-in succeeds — never on a mere click, because turning the setting off
  makes ``AuthService`` forget the saved password and session at once),
  inline validation and error messages (``LoginFailedError`` → "Incorrect
  email or password."), busy state while ``ctx.auth.login`` runs through
  ``run_async``. The remembered email is pre-filled in the background.
* "Continue with Discord" opens :class:`DiscordSignInDialog`: the embedded
  browser (shared persistent profile, whose ankergames.net cookies are first
  replaced by the app's so an old, signed-out session cannot short-circuit
  the flow) loads ``BASE_URL + "/auth/discord"``;
  when it comes back to ankergames.net on a URL that is not ``/login``,
  ``/auth/…`` or ``/register`` the sign-in is complete, the ankergames.net
  cookies collected from ``QWebEngineCookieStore.cookieAdded`` are handed to
  ``ctx.auth.login_with_cookies``. Disabled (with an explanation) when
  QtWebEngine is unavailable.
* "Create account" (``/register``) and "Forgot password?"
  (``/forgot-password``) open in the system browser.
* ``user`` holds the signed-in ``UserInfo`` after ``accept``; ``signed_in``
  is emitted too.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from anker_client.constants import BASE_URL, SITE_HOST
from anker_client.core.errors import AnkerError, LoginFailedError
from anker_client.core.models import UserInfo
from anker_client.core.paths import resource_path
from anker_client.core.tasks import TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.dialogs import browser as web
from anker_client.ui.dialogs.browser import BrowserProfile, EmbeddedBrowserDialog
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Divider, button, label, repolish
from anker_client.ui.widgets.settings_controls import IconBinder, StatusLine, background, open_url

log = logging.getLogger(__name__)

DISCORD_START_URL = f"{BASE_URL}/auth/discord"
REGISTER_URL = f"{BASE_URL}/register"
FORGOT_PASSWORD_URL = f"{BASE_URL}/forgot-password"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NOT_DONE_PREFIXES = ("/login", "/auth/", "/register", "/forgot-password", "/password")


def is_signin_complete(url: QUrl | str, site_host: str = SITE_HOST) -> bool:
    """True when the browser is back on the site, past the login/OAuth pages."""
    qurl = url if isinstance(url, QUrl) else QUrl(url)
    if qurl.scheme() not in ("http", "https") or not web.host_matches(qurl.host(), site_host):
        return False
    path = (qurl.path() or "/").lower()
    return not (path == "/auth" or path.startswith(_NOT_DONE_PREFIXES))


class DiscordSignInDialog(EmbeddedBrowserDialog):
    """Embedded browser for the site's Discord OAuth flow; emits ``signed_in(cookies)``."""

    signed_in = pyqtSignal(object)  # list[dict] — ankergames.net cookies

    def __init__(
        self,
        browser: BrowserProfile,
        *,
        start_url: str = DISCORD_START_URL,
        site_host: str = SITE_HOST,
        settle_ms: int = 1500,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            browser,
            window_title="Sign in with Discord",
            heading="Continue with Discord",
            message="Sign in to Discord and allow AnkerGames to use your account. "
                    "This window closes when you're done.",
            icon_name="discord",
            parent=parent,
        )
        self.resize(760, 780)
        self._start_url = start_url
        self._site_host = site_host
        self._completed = False
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.footer.addStretch(1)
        self.footer.addWidget(self.cancel_button)
        # When the final page is slow to finish loading, the cookies are already
        # set once the URL changes; finish after a short settle delay.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(settle_ms)
        self._settle.timeout.connect(self._complete)
        self.page.urlChanged.connect(self._on_url)
        self.page.loadingChanged.connect(self._on_loading)

    def start(self) -> None:
        self.set_status("Opening Discord…", "busy")
        self.load(self._start_url)

    @property
    def completed(self) -> bool:
        return self._completed

    def _on_url(self, url: QUrl) -> None:
        if self._completed:
            return
        if is_signin_complete(url, self._site_host):
            self.set_status("Signing you in…", "busy")
            if not self._settle.isActive():
                self._settle.start()
        elif not web.host_matches(url.host(), self._site_host):
            self.set_status("Sign in to Discord below.", "info")

    def _on_loading(self, info: Any) -> None:
        statuses = web.loading_status()
        if self._completed or info.status() != statuses.LoadSucceededStatus:
            return
        if is_signin_complete(info.url(), self._site_host):
            self._complete()

    def _complete(self) -> None:
        if self._completed:
            return
        self._completed = True
        self._settle.stop()
        cookies = self.browser.cookies_for([self._site_host])
        log.info("Discord sign-in returned to the site with %d cookies", len(cookies))
        self.signed_in.emit(cookies)
        self.accept()


def _create_discord_dialog(ctx: AppContext, parent: QWidget) -> DiscordSignInDialog:
    """Factory (patched in tests).

    The persistent browser profile may still hold a site session the app has since signed
    out of (and the site invalidated): the OAuth round trip would then end at once with dead
    cookies. Give the browser the app's site identity first, as the verifier does.
    """
    browser = web.shared_browser(ctx.paths)
    try:
        browser.sync_site_cookies(ctx.http.export_cookies(), [SITE_HOST])
    except Exception:
        log.warning("Could not sync the sign-in browser's cookies", exc_info=True)
    return DiscordSignInDialog(browser, parent=parent)


def _discord_available() -> bool:
    return web.webengine_available() and QApplication.instance() is not None


class LoginDialog(QDialog):
    signed_in = pyqtSignal(object)  # UserInfo

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.user: UserInfo | None = None
        self._handle: TaskHandle[Any] | None = None
        self._busy = False
        self._discord_dialog: DiscordSignInDialog | None = None
        self._icons = IconBinder()
        self.setWindowTitle("Sign in to AnkerGames")
        self.setModal(True)
        self.setFixedWidth(460)
        self._build()
        self._prefill()

    # --- layout -------------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 32, 36, 28)
        root.setSpacing(0)

        logo = QLabel()
        logo.setFixedSize(48, 48)
        pixmap = QPixmap(str(resource_path("icon.png")))
        if not pixmap.isNull():
            ratio = self.devicePixelRatioF() or 1.0
            scaled = pixmap.scaled(QSize(int(48 * ratio), int(48 * ratio)), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            logo.setPixmap(scaled)
        root.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addSpacing(14)
        heading = label("Sign in to AnkerGames", "title")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(heading)
        root.addSpacing(4)
        sub = label("Use the account you created on ankergames.net.", "muted", wrap=True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(sub)
        root.addSpacing(22)

        self.discord_button = button("Continue with Discord", "discord", size="lg", on_click=self._start_discord)
        self._icons.bind(self.discord_button, "discord", "text", 20)
        if not _discord_available():
            self.discord_button.setEnabled(False)
            self.discord_button.setToolTip(
                "Discord sign-in needs the embedded browser, which is not available on this system."
            )
        root.addWidget(self.discord_button)
        root.addSpacing(18)

        or_row = QHBoxLayout()
        or_row.setSpacing(12)
        or_row.addWidget(Divider(), 1)
        or_label = label("or sign in with email", "caption")
        or_row.addWidget(or_label)
        or_row.addWidget(Divider(), 1)
        root.addLayout(or_row)
        root.addSpacing(18)

        root.addWidget(label("Email"))
        root.addSpacing(6)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.email_edit.setClearButtonEnabled(True)
        self.email_edit.returnPressed.connect(self.password_focus)
        self.email_edit.textEdited.connect(lambda _t: self._clear_error())
        root.addWidget(self.email_edit)
        root.addSpacing(14)

        password_row = QHBoxLayout()
        password_row.addWidget(label("Password"))
        password_row.addStretch(1)
        self.forgot_button = button("Forgot password?", variant="link", on_click=lambda: open_url(FORGOT_PASSWORD_URL))
        password_row.addWidget(self.forgot_button)
        root.addLayout(password_row)
        root.addSpacing(6)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Your password")
        self.password_edit.returnPressed.connect(self.submit)
        self.password_edit.textEdited.connect(lambda _t: self._clear_error())
        self.reveal_action = QAction(self)
        self.reveal_action.setCheckable(True)
        self.reveal_action.toggled.connect(self._set_password_visible)
        self.password_edit.addAction(self.reveal_action, QLineEdit.ActionPosition.TrailingPosition)
        self._set_password_visible(False)
        root.addWidget(self.password_edit)
        root.addSpacing(12)

        self.remember_check = QCheckBox("Remember me on this PC")
        self.remember_check.setChecked(self._ctx.settings.get().remember_login)
        self.remember_check.setToolTip("Stores your sign-in in Windows Credential Manager so AnkerClient "
                                       "signs you in automatically.")
        root.addWidget(self.remember_check)
        root.addSpacing(14)

        self.error_line = StatusLine("", "error")
        self.error_line.hide()
        root.addWidget(self.error_line)
        self.error_spacer = QWidget()
        self.error_spacer.setFixedHeight(10)
        self.error_spacer.hide()
        root.addWidget(self.error_spacer)

        self.submit_button = button("Sign in", variant="primary", size="lg", on_click=self.submit)
        self.submit_button.setDefault(True)
        self.submit_button.setAutoDefault(True)
        root.addWidget(self.submit_button)
        root.addSpacing(18)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        footer.addStretch(1)
        footer.addWidget(label("New to AnkerGames?", "muted"))
        self.register_button = button("Create an account", variant="link", on_click=lambda: open_url(REGISTER_URL))
        footer.addWidget(self.register_button)
        footer.addStretch(1)
        root.addLayout(footer)

        self.discord_button.setAutoDefault(False)
        self.forgot_button.setAutoDefault(False)
        self.register_button.setAutoDefault(False)
        self.email_edit.setFocus()

    def _prefill(self) -> None:
        def fill(email: object) -> None:
            if isinstance(email, str) and email and not self.email_edit.text():
                self.email_edit.setText(email)
                self.password_edit.setFocus()

        run_async(self, self._ctx.runner, background(self._ctx.auth.remembered_email), on_result=fill,
                  on_error=lambda exc: log.debug("No remembered email: %s", exc))

    # --- state helpers --------------------------------------------------------------------
    def password_focus(self) -> None:
        self.password_edit.setFocus()

    def _set_password_visible(self, visible: bool) -> None:
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.reveal_action.setIcon(icons.icon("eye_off" if visible else "eye", palette.current().text_muted))
        self.reveal_action.setToolTip("Hide password" if visible else "Show password")
        self.reveal_action.setText(self.reveal_action.toolTip())

    def _persist_remember(self, remember: bool) -> None:
        try:
            if self._ctx.settings.get().remember_login != remember:
                self._ctx.settings.update(remember_login=remember)
        except (OSError, ValueError, KeyError):
            log.warning("Could not save the remember-me preference", exc_info=True)

    def show_error(self, message: str) -> None:
        self.error_line.set_status(message, "error")
        self.error_line.show()
        self.error_spacer.show()

    def error_message(self) -> str:
        """The inline error currently shown ("" when none)."""
        return "" if self.error_line.isHidden() else self.error_line.text()

    def _clear_error(self) -> None:
        if not self.error_line.isHidden():
            self.error_line.hide()
            self.error_spacer.hide()

    @property
    def busy(self) -> bool:
        return self._busy

    def _set_busy(self, busy: bool, text: str = "Signing in…") -> None:
        self._busy = busy
        for widget in (self.email_edit, self.password_edit, self.remember_check, self.discord_button,
                       self.forgot_button, self.register_button):
            widget.setEnabled(not busy and (widget is not self.discord_button or _discord_available()))
        self.submit_button.setEnabled(not busy)
        self.submit_button.setText(text if busy else "Sign in")
        repolish(self.submit_button)

    # --- email / password --------------------------------------------------------------------
    def _validate(self) -> str:
        email = self.email_edit.text().strip()
        if not email:
            self.email_edit.setFocus()
            return "Enter your email address."
        if not _EMAIL_RE.match(email):
            self.email_edit.setFocus()
            return "Enter a valid email address, like name@example.com."
        if not self.password_edit.text():
            self.password_edit.setFocus()
            return "Enter your password."
        return ""

    def submit(self) -> None:
        if self._busy:
            return
        problem = self._validate()
        if problem:
            self.show_error(problem)
            return
        self._clear_error()
        self._set_busy(True)
        self._handle = run_async(
            self,
            self._ctx.runner,
            self._ctx.auth.login,
            self.email_edit.text().strip(),
            self.password_edit.text(),
            remember=self.remember_check.isChecked(),
            on_result=self._on_signed_in,
            on_error=self._on_login_error,
        )

    def _on_login_error(self, exc: BaseException) -> None:
        self._handle = None
        self._set_busy(False)
        if isinstance(exc, LoginFailedError):
            self.show_error(exc.user_message)
            self.password_edit.setFocus()
            self.password_edit.selectAll()
        elif isinstance(exc, AnkerError):
            self.show_error(exc.user_message)
        else:
            log.error("Unexpected sign-in failure", exc_info=exc)
            self.show_error(f"Sign-in failed: {error_text(exc)}")

    def _on_signed_in(self, user: object) -> None:
        self._handle = None
        self._set_busy(False)
        # auth.login mirrors the choice itself; the Discord path relies on this. Turning it off
        # here (after the sign-in) makes AuthService drop the session it just saved — as asked.
        self._persist_remember(self.remember_check.isChecked())
        self.user = user if isinstance(user, UserInfo) else None
        self.signed_in.emit(self.user)
        self.accept()

    # --- Discord ---------------------------------------------------------------------------------
    def _start_discord(self) -> None:
        if self._busy or not _discord_available():
            return
        self._clear_error()
        try:
            dialog = _create_discord_dialog(self._ctx, self)
        except Exception as exc:
            log.exception("Could not open the Discord sign-in window")
            self.show_error(f"The sign-in browser could not start ({error_text(exc)}).")
            return
        self._discord_dialog = dialog
        dialog.signed_in.connect(self._on_discord_cookies)
        dialog.finished.connect(lambda _code, d=dialog: self._on_discord_closed(d))
        dialog.open()
        dialog.start()

    def _on_discord_closed(self, dialog: Any) -> None:
        if self._discord_dialog is dialog:
            self._discord_dialog = None
        dispose = getattr(dialog, "dispose", None)
        if callable(dispose):
            QTimer.singleShot(0, dispose)

    def _on_discord_cookies(self, cookies: object) -> None:
        cookie_list = list(cookies) if isinstance(cookies, list | tuple) else []
        if not cookie_list:
            self.show_error("Discord sign-in did not finish. Try again, or sign in with your email and password.")
            return
        if self.remember_check.isChecked():
            # Before the call, so login_with_cookies saves the session; turning it on wipes nothing.
            self._persist_remember(True)
        self._set_busy(True, "Finishing sign-in…")
        self._handle = run_async(
            self,
            self._ctx.runner,
            self._ctx.auth.login_with_cookies,
            cookie_list,
            on_result=self._on_signed_in,
            on_error=self._on_discord_error,
        )

    def _on_discord_error(self, exc: BaseException) -> None:
        self._handle = None
        self._set_busy(False)
        message = exc.user_message if isinstance(exc, AnkerError) else error_text(exc)
        self.show_error(f"Discord sign-in failed: {message}")

    # --- closing -----------------------------------------------------------------------------------
    def reject(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        super().reject()

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        super().closeEvent(event)
