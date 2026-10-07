"""First-run setup wizard (library folder, 7-Zip, preferences, optional sign-in).

Steps: Welcome (unofficial-client disclaimer, must be acknowledged) →
Library folder (validated off the GUI thread: free space, writable, not a
drive root/system folder) → 7-Zip (detect / browse / download link; optional)
→ Preferences (theme cards applied live, shortcut toggles, close behaviour) →
Account (optional ``LoginDialog``) → Finish (summary).

Nothing is persisted until "Get started": then the choices are written with
one ``ctx.settings.update(...)`` including ``first_run_completed=True`` and
the library folder is created in the background. Cancelling restores the
theme that was active before the wizard opened and leaves settings untouched
(so the wizard shows again next launch).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from anker_client.core.settings import CLOSE_ASK, CLOSE_QUIT, CLOSE_TRAY
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.dialogs.login import LoginDialog
from anker_client.ui.theme import palette as palettes
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.common import button, label, repolish
from anker_client.ui.widgets.settings_controls import IconBinder, SettingRow, SettingsGroup, StatusLine, ToggleSwitch
from anker_client.ui.widgets.settings_folders import FolderCheck, check_library_folder, overlapping_folder
from anker_client.ui.widgets.settings_sevenzip import SevenZipInfo, SevenZipPanel
from anker_client.ui.widgets.theme_card import ThemePicker

log = logging.getLogger(__name__)

STEPS = ("Welcome", "Library", "7-Zip", "Preferences", "Account", "Finish")
LOW_SPACE_BYTES = 30 * 1024**3
CLOSE_CHOICES = (
    (CLOSE_TRAY, "Keep running in the tray"),
    (CLOSE_QUIT, "Quit AnkerClient"),
    (CLOSE_ASK, "Ask me every time"),
)
DISCLAIMER = (
    "AnkerClient is an unofficial app. It is not affiliated with, endorsed by, or sponsored by "
    "AnkerGames. Use it with your own account and follow the AnkerGames terms and applicable law."
)


def _user_name(user: Any) -> str:
    return getattr(user, "display_name", "") or getattr(user, "email", "") or "your AnkerGames account"


def _page_header(title: str, text: str) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(6)
    layout.addWidget(label(title, "display"))
    if text:
        layout.addWidget(label(text, "muted", wrap=True))
    return layout


def _base_layout(page: QWizardPage, title: str, text: str) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 4, 8, 4)
    layout.setSpacing(18)
    layout.addLayout(_page_header(title, text))
    return layout


@dataclass(frozen=True, slots=True)
class _LibraryProbe:
    check: FolderCheck
    kept_dirs: tuple[str, ...]  # the other configured library folders that stay configured


def _probe_library(path: str, previous: list[str], *, keep_missing: bool = False,
                   token: CancelToken) -> _LibraryProbe:
    """Validate the chosen folder against the library folders that will be kept next to it.

    On a true first run, configured folders that do not exist (the built-in ``C:\\Games``
    default) are dropped; when the wizard is run again they are kept (e.g. an unplugged drive).
    """
    check = check_library_folder(path)
    token.raise_if_cancelled()
    chosen = os.path.normcase(os.path.normpath(check.path)) if check.path else ""
    kept = tuple(
        d for d in previous
        if os.path.normcase(os.path.normpath(d)) != chosen and (keep_missing or os.path.isdir(d))
    )
    if check.ok:
        other = overlapping_folder(check.path, kept)
        if other is not None:
            check = FolderCheck(check.path, False, f"This folder overlaps your library folder {other}. "
                                                   "Choose a folder outside it, or that folder itself.")
    return _LibraryProbe(check, kept)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


class _WelcomePage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(18)
        logo = QLabel()
        logo.setFixedSize(64, 64)
        pixmap = QPixmap(str(wizard.resource("icon.png")))
        if not pixmap.isNull():
            scaled = pixmap.scaled(QSize(128, 128), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo.setPixmap(scaled)
        layout.addWidget(logo)
        layout.addLayout(_page_header(
            "Welcome to AnkerClient",
            "Browse the AnkerGames store, download with a fast, resumable downloader and keep every game "
            "in one library. Setup takes about a minute.",
        ))
        card = QFrame()
        card.setProperty("role", "card")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(14)
        icon = QLabel()
        icon.setFixedSize(22, 22)
        wizard.icons.bind(icon, "info", "info", 22)
        card_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(label("Before you start", "heading"))
        text.addWidget(label(DISCLAIMER, "muted", wrap=True))
        card_layout.addLayout(text, 1)
        layout.addWidget(card)
        self.ack = QCheckBox("I understand that AnkerClient is an unofficial client")
        layout.addWidget(self.ack)
        layout.addStretch(1)
        self.registerField("acknowledged*", self.ack)


class _LibraryPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        self._handle: TaskHandle[Any] | None = None
        self._probe: _LibraryProbe | None = None
        layout = _base_layout(
            self,
            "Where should games go?",
            "Each game gets its own folder here. Pick a drive with plenty of free space; you can add more "
            "folders later in Settings.",
        )
        card = SettingsGroup()
        body = QWidget()
        body.setProperty("role", "transparent")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 16, 18, 16)
        body_layout.setSpacing(10)
        body_layout.addWidget(label("Library folder", "heading"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path_edit = QLineEdit(wizard.initial.default_library)
        self.path_edit.setPlaceholderText("D:\\Games")
        self.path_edit.textChanged.connect(self._schedule_check)
        self.browse_button = button("Browse…", "folder", on_click=self._browse)
        wizard.icons.bind(self.browse_button, "folder", "text", 16)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.browse_button)
        body_layout.addLayout(row)
        self.status = StatusLine("Checking…", "busy")
        body_layout.addWidget(self.status)
        card.add_row(body)
        layout.addWidget(card)
        tip = label("Tip: games are often 20–150 GB. A separate SSD works best.", "caption", wrap=True)
        layout.addWidget(tip)
        layout.addStretch(1)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.run_check)

    def initializePage(self) -> None:  # noqa: N802
        if self._probe is None:
            self.run_check()

    def _browse(self) -> None:
        start = self.path_edit.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a library folder", start)
        if chosen:
            self.path_edit.setText(os.path.normpath(chosen))

    def _schedule_check(self) -> None:
        self._probe = None
        self.status.set_status("Checking…", "busy")
        self.completeChanged.emit()
        self._debounce.start()

    def run_check(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        path = self.path_edit.text()
        initial = self._wizard.initial
        self._handle = run_async(self, self._wizard.ctx.runner, _probe_library, path,
                                 list(initial.library_dirs), keep_missing=initial.first_run_completed,
                                 on_result=lambda probe, p=path: self._on_probe(p, probe),
                                 on_error=self._on_probe_error)

    def _on_probe(self, path: str, probe: _LibraryProbe) -> None:
        self._handle = None
        if path != self.path_edit.text():
            return  # stale
        self._probe = probe
        check = probe.check
        if not check.ok:
            self.status.set_status(check.message, "error")
        elif check.free_bytes is not None and check.free_bytes < LOW_SPACE_BYTES:
            self.status.set_status(f"{check.message}. That is not much room for games.", "warning")
        else:
            self.status.set_status(check.message, "success")
        self.completeChanged.emit()

    def _on_probe_error(self, exc: BaseException) -> None:
        self._handle = None
        self._probe = None
        self.status.set_status(f"Could not check this folder: {error_text(exc)}", "error")
        self.completeChanged.emit()

    def isComplete(self) -> bool:  # noqa: N802
        return self._probe is not None and self._probe.check.ok

    def probe(self) -> _LibraryProbe | None:
        return self._probe


class _SevenZipPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        self.custom_path = wizard.initial.seven_zip_path
        self._started = False
        layout = _base_layout(
            self,
            "Unpacking games",
            "AnkerClient uses the free 7-Zip program to unpack downloaded games. "
            "If it is not installed yet you can continue and install it later.",
        )
        card = SettingsGroup()
        body = QWidget()
        body.setProperty("role", "transparent")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 16, 18, 16)
        body_layout.setSpacing(8)
        body_layout.addWidget(label("7-Zip", "heading"))
        self.panel = SevenZipPanel(wizard.ctx.runner)
        self.panel.path_chosen.connect(self._on_path_chosen)
        self.panel.auto_detect_requested.connect(self._on_auto_detect)
        body_layout.addWidget(self.panel)
        card.add_row(body)
        layout.addWidget(card)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        if not self._started:
            self._started = True
            self.panel.detect(self.custom_path)

    def _on_path_chosen(self, path: str) -> None:
        self.custom_path = path

    def _on_auto_detect(self) -> None:
        self.custom_path = ""
        self.panel.detect("")

    def info(self) -> SevenZipInfo | None:
        return self.panel.info()


class _PreferencesPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        initial = wizard.initial
        self.theme_key = wizard.theme.current_key
        layout = _base_layout(self, "Make it yours", "Pick a look and how AnkerClient should behave. "
                                                    "Everything can be changed later in Settings.")
        layout.setSpacing(14)
        layout.addWidget(label("Theme", "heading"))
        self.picker = ThemePicker(self.theme_key, columns=3, compact=True)
        self.picker.theme_selected.connect(self._on_theme)
        layout.addWidget(self.picker)
        layout.addWidget(label("Behaviour", "heading"))
        group = SettingsGroup()
        self.desktop_toggle = ToggleSwitch(checked=initial.create_desktop_shortcut)
        self.start_menu_toggle = ToggleSwitch(checked=initial.create_start_menu_shortcut)
        self.close_combo = QComboBox()
        for value, text in CLOSE_CHOICES:
            self.close_combo.addItem(text, value)
        index = self.close_combo.findData(initial.close_behavior)
        self.close_combo.setCurrentIndex(max(0, index))
        self.close_combo.setMinimumWidth(220)
        group.add_row(SettingRow("Desktop shortcuts", "Add a desktop shortcut for each game you install.",
                                 self.desktop_toggle))
        group.add_row(SettingRow("Start menu shortcuts", "Add installed games to the Start menu.",
                                 self.start_menu_toggle))
        group.add_row(SettingRow("When I close the window", "", self.close_combo))
        layout.addWidget(group)
        layout.addStretch(1)

    def _on_theme(self, key: str) -> None:
        self.theme_key = key
        self._wizard.preview_theme(key)


class _AccountPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        self._dialog: LoginDialog | None = None
        layout = _base_layout(
            self,
            "Sign in (optional)",
            "Signing in lets AnkerClient download with your AnkerGames account. "
            "You can skip this and sign in later from the sidebar.",
        )
        group = SettingsGroup()
        self.status = StatusLine("", "muted")
        self.sign_in_button = button("Sign in…", "login", variant="primary", on_click=self.open_login)
        wizard.icons.bind(self.sign_in_button, "login", "accent_text", 16)
        row = SettingRow("AnkerGames account", "")
        row.description_label.hide()
        row.add_control(self.sign_in_button)
        group.add_row(row)
        body = QWidget()
        body.setProperty("role", "transparent")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 0, 18, 14)
        body_layout.addWidget(self.status)
        group.add_row(body, divider=False)
        layout.addWidget(group)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        self.refresh()

    def refresh(self) -> None:
        user = self._wizard.current_user()
        if user is not None:
            self.status.set_status(f"Signed in as {_user_name(user)}", "success")
            self.sign_in_button.setText("Signed in")
            self.sign_in_button.setEnabled(False)
        else:
            self.status.set_status("Not signed in. You can still browse the store and download as a guest.", "info")
            self.sign_in_button.setText("Sign in…")
            self.sign_in_button.setEnabled(True)
        repolish(self.sign_in_button)

    def open_login(self) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            return
        dialog = LoginDialog(self._wizard.ctx, self._wizard)
        self._dialog = dialog
        dialog.signed_in.connect(lambda _user: self.refresh())
        dialog.finished.connect(lambda _code, d=dialog: self._on_login_closed(d))
        dialog.open()

    def _on_login_closed(self, dialog: LoginDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()  # parented to the wizard: would otherwise live as long as it
        self.refresh()

    def login_dialog(self) -> LoginDialog | None:
        return self._dialog


class _FinishPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        layout = _base_layout(self, "You're all set", "Here is what AnkerClient will use. "
                                                     "Click Get started to open your library.")
        self.group = SettingsGroup()
        self.lines: dict[str, StatusLine] = {}
        for key, title in (("library", "Library"), ("sevenzip", "7-Zip"), ("theme", "Theme"),
                           ("account", "Account")):
            line = StatusLine("", "success")
            self.lines[key] = line
            # A narrow title column and a wide value column: long library paths stay on one line.
            row = QWidget()
            row.setProperty("role", "transparent")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(18, 12, 18, 12)
            row_layout.setSpacing(16)
            name = label(title)
            name.setFixedWidth(96)
            row_layout.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
            row_layout.addWidget(line, 1)
            self.group.add_row(row)
        layout.addWidget(self.group)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        w = self._wizard
        probe = w.library_page.probe()
        self.lines["library"].set_status(probe.check.path if probe else w.initial.default_library, "success")
        info = w.seven_zip_page.info()
        if info is not None and info.found:
            self.lines["sevenzip"].set_status(info.version, "success")
        else:
            self.lines["sevenzip"].set_status("Not installed yet. Install it before your first download.", "warning")
        self.lines["theme"].set_status(palettes.get(w.preferences_page.theme_key).name, "success")
        user = w.current_user()
        if user is not None:
            self.lines["account"].set_status(f"Signed in as {_user_name(user)}", "success")
        else:
            self.lines["account"].set_status("Not signed in", "info")


# ---------------------------------------------------------------------------
# step list
# ---------------------------------------------------------------------------


class _StepList(QFrame):
    def __init__(self, icons_: IconBinder) -> None:
        super().__init__()
        self.setProperty("role", "panel")
        self.setFixedWidth(190)
        self._icons = icons_
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(4)
        title = label("Setup", "caption")
        layout.addWidget(title)
        layout.addSpacing(8)
        self._rows: list[tuple[QLabel, QLabel]] = []
        for step in STEPS:
            row = QHBoxLayout()
            row.setSpacing(10)
            marker = QLabel()
            marker.setFixedSize(18, 18)
            text = label(step, "muted")
            row.addWidget(marker)
            row.addWidget(text, 1)
            holder = QWidget()
            holder.setProperty("role", "transparent")
            holder.setLayout(row)
            holder.setFixedHeight(34)
            layout.addWidget(holder)
            self._rows.append((marker, text))
        layout.addStretch(1)
        self.set_current(0)

    def set_current(self, index: int) -> None:
        for i, (marker, text) in enumerate(self._rows):
            if i < index:
                self._icons.bind(marker, "check_circle", "success", 18)
                role = "muted"
            elif i == index:
                self._icons.bind(marker, "chevron_right", "accent", 18)
                role = "heading"
            else:
                self._icons.bind(marker, "minus", "text_faint", 18)
                role = "faint"
            text.setProperty("role", role)
            repolish(text)


# ---------------------------------------------------------------------------
# wizard
# ---------------------------------------------------------------------------


class FirstRunWizard(QWizard):
    def __init__(self, ctx: AppContext, theme: ThemeManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.theme = theme
        self.initial = ctx.settings.get()
        self._original_theme = theme.current_key
        self._applied = False
        self.icons = IconBinder()
        self.setWindowTitle("Set up AnkerClient")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.NoCancelButtonOnLastPage, True)
        self.setButtonText(QWizard.WizardButton.NextButton, "Continue")
        self.setButtonText(QWizard.WizardButton.BackButton, "Back")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Get started")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Set up later")
        self._style_buttons()
        self.setMinimumSize(900, 640)
        self.resize(960, 700)

        self.steps = _StepList(self.icons)
        self.setSideWidget(self.steps)
        self.welcome_page = _WelcomePage(self)
        self.library_page = _LibraryPage(self)
        self.seven_zip_page = _SevenZipPage(self)
        self.preferences_page = _PreferencesPage(self)
        self.account_page = _AccountPage(self)
        self.finish_page = _FinishPage(self)
        for page in (self.welcome_page, self.library_page, self.seven_zip_page, self.preferences_page,
                     self.account_page, self.finish_page):
            self.addPage(page)
        self.currentIdChanged.connect(self._on_page_changed)
        theme.theme_changed.connect(self._on_theme_changed)

    # --- helpers used by pages ---------------------------------------------------------------
    @staticmethod
    def resource(name: str) -> Any:
        from anker_client.core.paths import resource_path

        return resource_path(name)

    def current_user(self) -> Any:
        try:
            return self.ctx.auth.user
        except Exception:
            log.debug("Could not read the signed-in user", exc_info=True)
            return None

    def preview_theme(self, key: str) -> None:
        self.theme.apply(key)

    # --- events --------------------------------------------------------------------------------
    def _on_page_changed(self, page_id: int) -> None:
        self.steps.set_current(max(0, self.pageIds().index(page_id)) if page_id in self.pageIds() else 0)
        self._style_buttons()

    def _on_theme_changed(self, _key: str) -> None:
        self.icons.retint()
        for page in (self.library_page, self.seven_zip_page, self.account_page, self.finish_page):
            for line in page.findChildren(StatusLine):
                line.retint()
        self.seven_zip_page.panel.retint()
        self.steps.set_current(self.steps_index())
        self._style_buttons()

    def _style_buttons(self) -> None:
        for which in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            btn = self.button(which)
            if btn is not None:
                btn.setProperty("variant", "primary")
                repolish(btn)

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._style_buttons()  # QWizard polishes its buttons late; re-apply the primary look

    def steps_index(self) -> int:
        ids = self.pageIds()
        return ids.index(self.currentId()) if self.currentId() in ids else 0

    # --- finish / cancel ----------------------------------------------------------------------------
    def settings_changes(self) -> dict[str, Any]:
        """The settings the wizard will write on "Get started"."""
        probe = self.library_page.probe()
        library = probe.check.path if probe is not None and probe.check.ok else self.initial.default_library
        kept = list(probe.kept_dirs) if probe is not None else [
            d for d in self.initial.library_dirs if os.path.normcase(d) != os.path.normcase(library)
        ]
        return {
            "library_dirs": [library, *kept],
            "default_library": library,
            "seven_zip_path": self.seven_zip_page.custom_path,
            "theme": self.preferences_page.theme_key,
            "create_desktop_shortcut": self.preferences_page.desktop_toggle.isChecked(),
            "create_start_menu_shortcut": self.preferences_page.start_menu_toggle.isChecked(),
            "close_behavior": self.preferences_page.close_combo.currentData(),
            "first_run_completed": True,
        }

    def accept(self) -> None:
        changes = self.settings_changes()
        try:
            self.ctx.settings.update(**changes)
        except (OSError, ValueError, KeyError) as exc:
            log.exception("Could not save the setup choices")
            self.finish_page.lines["library"].set_status(f"Could not save settings: {error_text(exc)}", "error")
            return
        self._applied = True
        library = changes["default_library"]
        self.ctx.runner.submit(_create_folder, library, name="create-library-folder")
        log.info("First-run setup completed (library %s)", library)
        self._disconnect_theme()
        super().accept()

    def reject(self) -> None:
        if not self._applied and self.theme.current_key != self._original_theme:
            self.theme.apply(self._original_theme)
        self._disconnect_theme()
        super().reject()

    def _disconnect_theme(self) -> None:
        try:
            self.theme.theme_changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass


def _create_folder(path: str, *, token: CancelToken) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        log.warning("Could not create the library folder %s", path, exc_info=True)
