"""Settings page (sectioned).

Layout: page title, a section list on the left (General, Library, Downloads,
Account, Appearance, Advanced, About) and the selected section's scrollable
form on the right. There is no Apply button: every control writes through
``ctx.settings.update(...)`` immediately, and every control is refreshed from
``bridge.settings_changed`` so changes made elsewhere (Downloads speed menu,
library view toggles, the setup wizard) show up live.

Sections
* General — close behaviour, start minimized, launch with Windows
  (``services.system.autostart.set_enabled`` off the GUI thread; failures
  revert the switch and toast), notifications, app update checks.
* Library — library folders (add with validation / remove with confirmation /
  make default / open; free space per folder; a library rescan after
  changes), shortcut toggles, minimize on game launch, show hidden games,
  game update checks + interval.
* Downloads — download folder (checked off the GUI thread: full path, writable,
  not inside Windows/Program Files), simultaneous downloads, connections per
  download, speed limit (presets + custom MB/s), auto install, delete archive,
  test archive, resume on startup, browser-check time limit, 7-Zip (detected
  path + version, Browse…, Detect automatically).
* Account — signed-in user (live from ``bridge.auth_changed``) with Sign out
  and "Open account page", or Sign in (``nav.request_login``); remember sign-in.
* Appearance — theme cards; a click applies the theme live through the
  ``ThemeManager`` and persists ``settings.theme``.
* Advanced — store index stats + "Sync now" (``ctx.catalog.sync`` via
  ``run_async``, progress from ``bridge.catalog_sync_progress``, cancellable),
  image cache size + Clear, log detail (``core.logging_setup.set_level``),
  open logs/data folders, run the setup wizard again, reset settings
  (confirmed; keeps library folders, setup state and window layout).
* About — version, links (GitHub, releases, report an issue, AnkerGames),
  "Check for updates" (``ctx.app_updates.check``; also live from
  ``bridge.app_update_available``), disclaimer and third-party notices.

Disk/network/registry work always goes through ``run_async``; dynamic
information is loaded when its section is shown.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QSignalBlocker, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anker_client import __version__
from anker_client.constants import BASE_URL, GITHUB_REPO, ISSUES_URL, RELEASES_URL
from anker_client.core import logging_setup
from anker_client.core.formatting import format_bytes, format_relative_time, pluralize
from anker_client.core.models import AppRelease, UserInfo
from anker_client.core.paths import resource_path
from anker_client.core.settings import CLOSE_ASK, CLOSE_QUIT, CLOSE_TRAY, Settings
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.services.system import autostart
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.navigator import Navigator
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.common import Badge, button, label, repolish
from anker_client.ui.widgets.settings_controls import (
    Avatar,
    IconBinder,
    SectionHeader,
    SettingRow,
    SettingsGroup,
    StatusLine,
    ToggleSwitch,
    background,
    confirm,
    group_heading,
    open_local_path,
    open_url,
)
from anker_client.ui.widgets.settings_folders import (
    LibraryFoldersEditor,
    check_download_folder,
    check_library_folder,
    folder_free_space,
)
from anker_client.ui.widgets.settings_sevenzip import SevenZipPanel
from anker_client.ui.widgets.theme_card import ThemePicker

log = logging.getLogger(__name__)

SECTIONS = ("general", "library", "downloads", "account", "appearance", "advanced", "about")

SECTION_INFO: dict[str, tuple[str, str, str]] = {
    # key: (label, icon, description)
    "general": ("General", "settings", "Startup, window and notification behaviour."),
    "library": ("Library", "library", "Where games are installed and how your library behaves."),
    "downloads": ("Downloads", "download", "Speed, connections and what happens after a download finishes."),
    "account": ("Account", "user", "Your AnkerGames sign-in."),
    "appearance": ("Appearance", "palette", "Pick a theme. Changes apply instantly."),
    "advanced": ("Advanced", "wrench", "Store index, image cache, logs and resets."),
    "about": ("About", "info", "Version, links and legal notices."),
}

CLOSE_CHOICES = (
    (CLOSE_ASK, "Ask me every time"),
    (CLOSE_TRAY, "Keep running in the tray"),
    (CLOSE_QUIT, "Quit AnkerClient"),
)
UPDATE_INTERVALS = (
    (1, "Every hour"), (3, "Every 3 hours"), (6, "Every 6 hours"), (12, "Every 12 hours"),
    (24, "Once a day"), (48, "Every 2 days"), (168, "Once a week"),
)
VERIFICATION_TIMEOUTS = (
    (60, "1 minute"), (120, "2 minutes"), (180, "3 minutes"), (300, "5 minutes"), (600, "10 minutes"),
    (900, "15 minutes"),
)
LOG_LEVELS = (
    ("INFO", "Normal"),
    ("DEBUG", "Detailed (for bug reports)"),
    ("WARNING", "Warnings and errors"),
    ("ERROR", "Errors only"),
)
SPEED_PRESETS_MB = (1, 2, 5, 10, 25, 50)
CUSTOM_SPEED = -1
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
# Kept by "Reset settings": the library itself, setup state and window layout are not preferences.
RESET_KEEPS = ("library_dirs", "default_library", "first_run_completed", "window_geometry", "window_state",
               "schema_version")
THIRD_PARTY_NOTICE = (
    "AnkerClient is built with Python, Qt 6 and PyQt6 (GPL v3), Qt WebEngine (Chromium, BSD and other "
    "licences), requests (Apache 2.0), Beautiful Soup (MIT), lxml (BSD), keyring (MIT), psutil (BSD) and "
    "pywin32 (PSF). Games are unpacked with 7-Zip (GNU LGPL), which is installed separately. Icons are "
    "based on Lucide (ISC)."
)
DISCLAIMER = (
    "AnkerClient is unofficial and is not affiliated with, endorsed by, or sponsored by AnkerGames. "
    "Use it with your own account and follow the AnkerGames terms and applicable law."
)


def _combo(choices: tuple[tuple[Any, str], ...], width: int = 220) -> QComboBox:
    combo = QComboBox()
    for value, text in choices:
        combo.addItem(text, value)
    combo.setMinimumWidth(width)
    return combo


def _select(combo: QComboBox, value: Any, custom_text: Callable[[Any], str]) -> None:
    """Select ``value`` without emitting signals; adds a "custom" entry for unknown values."""
    with QSignalBlocker(combo):
        index = combo.findData(value)
        if index < 0:
            combo.addItem(custom_text(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)


def _folder_state(path: str, *, token: CancelToken) -> tuple[bool, int | None]:
    return os.path.isdir(path), folder_free_space(path)


class _Section(QScrollArea):
    """Scrollable section body: header + groups in a column of bounded width.

    The content is built on first use (``ensure_built``) so the page is cheap to create at
    startup and theme switches only re-polish sections the user has opened.
    """

    def __init__(self, key: str, builder: Callable[[_Section], None]) -> None:
        super().__init__()
        self.key = key
        self._builder = builder
        self.built = False
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def ensure_built(self) -> bool:
        """Build the content once; returns True when it was built just now."""
        if self.built:
            return False
        self.built = True
        holder = QWidget()
        holder.setProperty("role", "transparent")
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(0, 0, 12, 24)
        outer.setSpacing(0)
        self.column = QWidget()
        self.column.setProperty("role", "transparent")
        self.column.setMaximumWidth(820)
        self.body = QVBoxLayout(self.column)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        name, _icon, description = SECTION_INFO[self.key]
        self.body.addWidget(SectionHeader(name, description))
        self.body.addSpacing(6)
        outer.addWidget(self.column, 1)
        outer.addStretch(0)
        self._builder(self)
        self.body.addStretch(1)
        self.setWidget(holder)
        return True

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_group(self, heading: str = "") -> SettingsGroup:
        if heading:
            if self.body.count() > 2:
                self.body.addSpacing(10)
            self.body.addWidget(group_heading(heading))
        group = SettingsGroup()
        self.body.addWidget(group)
        return group


class SettingsPage(QWidget):
    #: Emitted after a section is shown (section key) — handy for the shell and tests.
    section_changed = pyqtSignal(str)

    def __init__(self, ctx: AppContext, bridge: QtEventBridge, nav: Navigator, theme: ThemeManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self._ctx = ctx
        self._bridge = bridge
        self._nav = nav
        self._theme = theme
        self._icons = IconBinder()
        self._bindings: dict[str, list[Callable[[Settings], None]]] = defaultdict(list)
        self._handles: dict[str, TaskHandle[Any]] = {}
        self._loaded: set[str] = set()
        self._active = False
        self._current = ""
        self._sync_handle: TaskHandle[Any] | None = None
        self._release: AppRelease | None = None
        self._wizard: QWidget | None = None
        self._shown_folders: tuple[tuple[str, ...], str] | None = None
        self._speed_custom = False
        # Hides the progress of a sync started elsewhere if it stops reporting (e.g. it failed).
        self._sync_idle_timer = QTimer(self)
        self._sync_idle_timer.setSingleShot(True)
        self._sync_idle_timer.setInterval(30_000)
        self._sync_idle_timer.timeout.connect(self._on_sync_idle)

        self.nav_buttons: dict[str, QPushButton] = {}
        self.sections: dict[str, _Section] = {}
        self._build()
        self._connect()
        self.show_section("general")

    # =====================================================================================
    # public API
    # =====================================================================================
    def show_section(self, section: str) -> None:
        key = section if section in SECTIONS else ("general" if not self._current else self._current)
        if section and section not in SECTIONS:
            log.warning("Unknown settings section %r", section)
        self._current = key
        self.ensure_section(key)
        button_ = self.nav_buttons[key]
        with QSignalBlocker(button_):
            button_.setChecked(True)
        self.stack.setCurrentWidget(self.sections[key])
        self._load_section(key)
        self.section_changed.emit(key)

    def current_section(self) -> str:
        return self._current

    def ensure_section(self, key: str) -> None:
        """Build a section's widgets (normally done when it is first shown)."""
        if self.sections[key].ensure_built():
            self._refresh_all()
            self._after_build(key)

    def is_built(self, key: str) -> bool:
        return self.sections[key].built

    def on_activated(self) -> None:
        self._active = True
        self._refresh_all()
        self._load_section(self._current, force=True)

    def on_deactivated(self) -> None:
        self._active = False

    def shutdown(self) -> None:
        for handle in list(self._handles.values()):
            handle.cancel()
        self._handles.clear()
        if self._sync_handle is not None:
            self._sync_handle.cancel()
            self._sync_handle = None
        self._sync_idle_timer.stop()
        try:
            self._theme.theme_changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass

    # =====================================================================================
    # construction
    # =====================================================================================
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 0)
        root.setSpacing(18)
        header = QVBoxLayout()
        header.setSpacing(4)
        header.addWidget(label("Settings", "display"))
        header.addWidget(label("Changes are saved automatically.", "muted"))
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(28)
        nav = QWidget()
        nav.setProperty("role", "transparent")
        nav.setFixedWidth(196)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key in SECTIONS:
            name, icon_name, _desc = SECTION_INFO[key]
            btn = button(name, icon_name, variant="nav")
            btn.setCheckable(True)
            btn.setIconSize(QSize(18, 18))
            self._icons.bind(btn, icon_name, "text_muted", 18)
            btn.clicked.connect(lambda _checked=False, k=key: self.show_section(k))
            group.addButton(btn)
            nav_layout.addWidget(btn)
            self.nav_buttons[key] = btn
        nav_layout.addStretch(1)
        body.addWidget(nav, 0)

        self.stack = QStackedWidget()
        self.stack.setProperty("role", "transparent")
        builders = {
            "general": self._build_general,
            "library": self._build_library,
            "downloads": self._build_downloads,
            "account": self._build_account,
            "appearance": self._build_appearance,
            "advanced": self._build_advanced,
            "about": self._build_about,
        }
        for key in SECTIONS:
            section = _Section(key, builders[key])
            self.sections[key] = section
            self.stack.addWidget(section)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

    def _connect(self) -> None:
        self._bridge.settings_changed.connect(self._on_settings_changed)
        self._bridge.auth_changed.connect(self._on_auth_changed)
        self._bridge.catalog_sync_progress.connect(self._on_sync_progress)
        self._bridge.catalog_updated.connect(self._on_catalog_updated)
        self._bridge.app_update_available.connect(self._show_release)
        self._theme.theme_changed.connect(self._on_theme_changed)

    # --- binding helpers -----------------------------------------------------------------------
    def _bind(self, key: str, refresh: Callable[[Settings], None]) -> None:
        self._bindings[key].append(refresh)

    def _toggle(self, key: str) -> ToggleSwitch:
        toggle = ToggleSwitch()
        toggle.setObjectName(f"setting_{key}")

        def refresh(s: Settings) -> None:
            with QSignalBlocker(toggle):
                toggle.setChecked(bool(getattr(s, key)))
            toggle.update()

        self._bind(key, refresh)
        toggle.toggled.connect(lambda checked: self._save(**{key: checked}))
        return toggle

    def _choice(self, key: str, choices: tuple[tuple[Any, str], ...], custom: Callable[[Any], str],
                *, width: int = 220, after: Callable[[Any], None] | None = None) -> QComboBox:
        combo = _combo(choices, width)
        combo.setObjectName(f"setting_{key}")
        self._bind(key, lambda s: _select(combo, getattr(s, key), custom))

        def chosen(_index: int) -> None:
            value = combo.currentData()
            if self._save(**{key: value}) and after is not None:
                after(value)

        combo.activated.connect(chosen)
        return combo

    def _spin(self, key: str, low: int, high: int, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setObjectName(f"setting_{key}")
        spin.setRange(low, high)
        spin.setKeyboardTracking(False)  # save on Enter/focus-out/arrows, not per keystroke
        spin.setMinimumWidth(96)
        if suffix:
            spin.setSuffix(suffix)

        def refresh(s: Settings) -> None:
            with QSignalBlocker(spin):
                spin.setValue(int(getattr(s, key)))

        self._bind(key, refresh)
        spin.valueChanged.connect(lambda value: self._save(**{key: int(value)}))
        return spin

    def _save(self, **changes: Any) -> bool:
        try:
            self._ctx.settings.update(**changes)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Could not save settings %s", sorted(changes), exc_info=True)
            self._nav.toast(f"Could not save the setting: {error_text(exc)}", "error")
            self._refresh_all()
            return False
        # Refresh right away (the bridge event follows and is harmless): clamped values show at once.
        self._refresh_keys(frozenset(changes))
        return True

    def _settings(self) -> Settings:
        return self._ctx.settings.get()

    def _refresh_all(self) -> None:
        s = self._settings()
        for refreshers in self._bindings.values():
            for refresh in refreshers:
                refresh(s)

    def _refresh_keys(self, keys: frozenset[str]) -> None:
        s = self._settings()
        for key in keys:
            for refresh in self._bindings.get(key, ()):
                refresh(s)

    def _run(self, purpose: str, fn: Callable[..., Any], /, *args: Any,
             on_result: Callable[[Any], None] | None = None,
             on_error: Callable[[BaseException], None] | None = None, **kwargs: Any) -> TaskHandle[Any]:
        """``run_async`` with one in-flight call per purpose (older results are dropped)."""
        previous = self._handles.pop(purpose, None)
        if previous is not None:
            previous.cancel()
        handle: TaskHandle[Any] | None = None

        def finished() -> None:
            if self._handles.get(purpose) is handle:
                self._handles.pop(purpose, None)

        handle = run_async(self, self._ctx.runner, fn, *args, on_result=on_result, on_error=on_error,
                           on_finished=finished, **kwargs)
        self._handles[purpose] = handle
        return handle

    def _cancel(self, purpose: str) -> None:
        handle = self._handles.pop(purpose, None)
        if handle is not None:
            handle.cancel()

    def _icon_label(self, name: str, role: str = "text_muted", size: int = 18) -> QLabel:
        lbl = QLabel()
        lbl.setFixedSize(size, size)
        self._icons.bind(lbl, name, role, size)
        return lbl

    # =====================================================================================
    # General
    # =====================================================================================
    def _build_general(self, section: _Section) -> None:
        startup = section.add_group("Startup")
        self.autostart_toggle = ToggleSwitch()
        self.autostart_toggle.setObjectName("setting_launch_on_startup")
        self.autostart_toggle.toggled.connect(self._on_autostart_toggled)

        def refresh_autostart(s: Settings) -> None:
            with QSignalBlocker(self.autostart_toggle):
                self.autostart_toggle.setChecked(s.launch_on_startup)
            self.autostart_toggle.update()

        self._bind("launch_on_startup", refresh_autostart)
        startup.add_row(SettingRow("Launch with Windows", "Start AnkerClient in the tray when you sign in to "
                                                         "Windows, so downloads resume right away.",
                                   self.autostart_toggle))
        startup.add_row(SettingRow("Start minimized", "Open in the system tray instead of showing the window.",
                                   self._toggle("start_minimized")))

        window = section.add_group("Window")
        self.close_combo = self._choice("close_behavior", CLOSE_CHOICES, str)
        window.add_row(SettingRow("When I close the window", "AnkerClient keeps downloading while it runs in "
                                                             "the tray.", self.close_combo))

        notify = section.add_group("Notifications")
        notify.add_row(SettingRow("Desktop notifications", "Tell me when downloads finish, fail or need my "
                                                           "attention, even when AnkerClient is in the tray.",
                                  self._toggle("notifications_enabled")))

        updates = section.add_group("Updates")
        updates.add_row(SettingRow("Check for AnkerClient updates", "Look for a new version at startup and once "
                                                                    "a day.", self._toggle("check_app_updates")))

    def _on_autostart_toggled(self, checked: bool) -> None:
        # A state check still in flight predates this change; its answer would undo it.
        self._cancel("autostart_state")
        self.autostart_toggle.setEnabled(False)

        def done(ok: object) -> None:
            self.autostart_toggle.setEnabled(True)
            if ok:
                self._save(launch_on_startup=checked)
                self._nav.toast("AnkerClient will start with Windows." if checked
                                else "AnkerClient will no longer start with Windows.", "success")
            else:
                self._revert_autostart()
                self._nav.toast("Windows did not accept the startup change.", "error")

        def failed(exc: BaseException) -> None:
            self.autostart_toggle.setEnabled(True)
            self._revert_autostart()
            if isinstance(exc, NotImplementedError):
                self._nav.toast("Starting with Windows is not available in this version.", "error")
            else:
                self._nav.toast(f"Could not change the startup setting: {error_text(exc)}", "error")

        self._run("autostart_set", background(autostart.set_enabled, checked), on_result=done, on_error=failed)

    def _revert_autostart(self) -> None:
        with QSignalBlocker(self.autostart_toggle):
            self.autostart_toggle.setChecked(self._settings().launch_on_startup)
        self.autostart_toggle.update()

    def _load_general(self) -> None:
        def reconcile(enabled: object) -> None:
            # The registry is the truth (the user may have removed the entry in Task Manager).
            if isinstance(enabled, bool) and enabled != self._settings().launch_on_startup:
                self._save(launch_on_startup=enabled)

        self._run("autostart_state", background(autostart.is_enabled), on_result=reconcile,
                  on_error=lambda exc: log.debug("Autostart state unavailable: %s", exc))

    # =====================================================================================
    # Library
    # =====================================================================================
    def _build_library(self, section: _Section) -> None:
        section.add(group_heading("Library folders"))
        folders = SettingsGroup()
        intro = SettingRow("Game folders", "Each game is installed into its own folder inside one of these. "
                                           "New games go to the default folder unless you pick another.")
        folders.add_row(intro)
        self.folders_editor = LibraryFoldersEditor()
        self.folders_editor.add_requested.connect(self._add_library_folder)
        self.folders_editor.remove_requested.connect(self._remove_library_folder)
        self.folders_editor.default_requested.connect(lambda path: self._save(default_library=path))
        self.folders_editor.open_requested.connect(open_local_path)
        folders.add_row(self.folders_editor)
        section.add(folders)

        def refresh_folders(s: Settings) -> None:
            shown = (tuple(s.library_dirs), s.default_library)
            if shown == self._shown_folders:
                return
            self._shown_folders = shown
            self.folders_editor.set_folders(list(s.library_dirs), s.default_library)
            if "library" in self._loaded:
                self._load_folder_space()

        self._bind("library_dirs", refresh_folders)
        self._bind("default_library", refresh_folders)

        shortcuts = section.add_group("Shortcuts")
        shortcuts.add_row(SettingRow("Desktop shortcuts", "Create a desktop shortcut when a game is installed.",
                                     self._toggle("create_desktop_shortcut")))
        shortcuts.add_row(SettingRow("Start menu shortcuts", "Add installed games to the Start menu.",
                                     self._toggle("create_start_menu_shortcut")))

        behaviour = section.add_group("Playing")
        behaviour.add_row(SettingRow("Minimize while playing", "Hide AnkerClient in the tray when a game starts.",
                                     self._toggle("minimize_on_game_launch")))
        behaviour.add_row(SettingRow("Show hidden games", "Include games you have hidden in the library.",
                                     self._toggle("show_hidden_games")))

        updates = section.add_group("Game updates")
        updates.add_row(SettingRow("Check for game updates", "Compare installed versions with AnkerGames in the "
                                                             "background.", self._toggle("check_game_updates")))
        self.update_interval_combo = self._choice("game_update_interval_hours", UPDATE_INTERVALS,
                                                  lambda v: f"Every {v} hours", width=180)
        updates.add_row(SettingRow("How often", "", self.update_interval_combo))
        self._bind("check_game_updates", lambda s: self.update_interval_combo.setEnabled(s.check_game_updates))

    def _load_library(self) -> None:
        self._load_folder_space()

    def _load_folder_space(self) -> None:
        for path in self.folders_editor.folders():
            self._run(f"space:{path}", _folder_state, path,
                      on_result=lambda state, p=path: self._show_folder_state(p, state),
                      on_error=lambda exc, p=path: self.folders_editor.set_folder_info(
                          p, f"Free space unknown ({error_text(exc)})"))

    def _show_folder_state(self, path: str, state: tuple[bool, int | None]) -> None:
        exists, free = state
        if not exists:
            self.folders_editor.set_folder_info(path, "Folder not found. It is created when a game is installed.",
                                                "warning")
        elif free is None:
            self.folders_editor.set_folder_info(path, "Free space unknown")
        else:
            self.folders_editor.set_folder_info(path, f"{format_bytes(free)} free")

    def _add_library_folder(self, path: str | None = None) -> None:
        if not path:
            path = QFileDialog.getExistingDirectory(self, "Add a library folder", os.path.expanduser("~"))
        if not path:
            return
        self.folders_editor.show_message("Checking the folder…", "busy")
        existing = list(self._settings().library_dirs)

        def checked(result: Any) -> None:
            if not result.ok:
                self.folders_editor.show_message(result.message, "error")
                return
            self.folders_editor.show_message("", "info")
            dirs = list(self._settings().library_dirs)
            if self._save(library_dirs=[*dirs, result.path]):
                self._nav.toast(f"Added {result.path} to your library folders.", "success")
                self._rescan_library()

        self._run("add_folder", background(check_library_folder, path, existing=existing), on_result=checked,
                  on_error=lambda exc: self.folders_editor.show_message(error_text(exc), "error"))

    def _remove_library_folder(self, path: str) -> None:
        s = self._settings()
        remaining = [d for d in s.library_dirs if os.path.normcase(d) != os.path.normcase(path)]
        if not remaining:
            self.folders_editor.show_message("Your library needs at least one folder.", "error")
            return
        if not confirm(
            self,
            title="Remove library folder",
            text=f"Remove {path} from your library folders?",
            informative="Games in this folder stay on disk but no longer appear in your library. "
                        "You can add the folder again at any time.",
            confirm_text="Remove folder",
        ):
            return
        default = s.default_library
        if os.path.normcase(default) == os.path.normcase(path):
            default = remaining[0]
        if self._save(library_dirs=remaining, default_library=default):
            self._rescan_library()

    def _rescan_library(self) -> None:
        self._run("library_scan", self._ctx.library.scan,
                  on_error=lambda exc: log.warning("Library rescan failed: %s", error_text(exc)))

    # =====================================================================================
    # Downloads
    # =====================================================================================
    def _build_downloads(self, section: _Section) -> None:
        location = section.add_group("Location")
        self.download_dir_label = label("", "muted", wrap=True)
        self.download_dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.download_dir_button = button("Change…", "folder", size="sm", on_click=self._choose_download_dir)
        self._icons.bind(self.download_dir_button, "folder", "text", 16)
        self.download_dir_reset = button("Use default", size="sm", on_click=lambda: self._save(download_dir=""))
        row = SettingRow("Download folder", "Archives are stored here while they download. By default they go "
                                            "inside the library folder, on the same drive as the game.")
        row.add_control(self.download_dir_reset)
        row.add_control(self.download_dir_button)
        location.add_row(row)
        path_row = QWidget()
        path_row.setProperty("role", "transparent")
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(18, 0, 18, 12)
        path_layout.addWidget(self._icon_label("hdd", "text_muted", 16))
        path_layout.addWidget(self.download_dir_label, 1)
        location.add_row(path_row, divider=False)

        def refresh_dir(s: Settings) -> None:
            if s.download_dir:
                self.download_dir_label.setText(s.download_dir)
            else:
                self.download_dir_label.setText("Default: a .ankerclient\\downloads folder inside each library folder")
            self.download_dir_reset.setVisible(bool(s.download_dir))

        self._bind("download_dir", refresh_dir)

        speed = section.add_group("Speed")
        speed.add_row(SettingRow("Simultaneous downloads", "How many games download at the same time.",
                                 self._spin("max_concurrent_downloads", 1, 5)))
        speed.add_row(SettingRow("Connections per download", "More connections can be faster on long-distance "
                                                             "links; 4 is a good default.",
                                 self._spin("connections_per_download", 1, 16)))
        self.speed_combo = QComboBox()
        self.speed_combo.setObjectName("setting_speed_limit_kbps")
        self.speed_combo.addItem("Unlimited", 0)
        for mb in SPEED_PRESETS_MB:
            self.speed_combo.addItem(f"{mb} MB/s", mb * 1024)
        self.speed_combo.addItem("Custom…", CUSTOM_SPEED)
        self.speed_combo.setMinimumWidth(150)
        self.speed_combo.activated.connect(self._on_speed_choice)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setObjectName("custom_speed")
        self.speed_spin.setRange(0.1, 9000.0)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSingleStep(0.5)
        self.speed_spin.setSuffix(" MB/s")
        self.speed_spin.setKeyboardTracking(False)
        self.speed_spin.setMinimumWidth(110)
        self.speed_spin.setValue(10.0)
        self.speed_spin.valueChanged.connect(lambda v: self._save(speed_limit_kbps=max(1, round(v * 1024))))
        speed.add_row(SettingRow("Speed limit", "Applies to all downloads together.", self.speed_spin,
                                 self.speed_combo))
        self._bind("speed_limit_kbps", self._refresh_speed)

        after = section.add_group("After downloading")
        after.add_row(SettingRow("Install automatically", "Unpack and install each game as soon as it finishes "
                                                          "downloading.", self._toggle("auto_install")))
        after.add_row(SettingRow("Delete the archive after installing", "Frees the space used by the downloaded "
                                                                        "file.",
                                 self._toggle("delete_archive_after_install")))
        after.add_row(SettingRow("Test archives before installing", "Checks the download with 7-Zip first. Slower, "
                                                                    "but catches damaged files early.",
                                 self._toggle("verify_archive_before_install")))
        after.add_row(SettingRow("Resume downloads on startup", "Continue unfinished downloads when AnkerClient "
                                                                "starts.", self._toggle("auto_resume_downloads")))

        check = section.add_group("Browser check")
        check.add_row(SettingRow(
            "Time limit", "Some downloads ask for a quick check in a browser window. AnkerClient waits this long "
                          "before giving up.",
            self._choice("verification_timeout_seconds", VERIFICATION_TIMEOUTS, lambda v: f"{v} seconds",
                         width=160),
        ))

        tools = section.add_group("7-Zip")
        holder = QWidget()
        holder.setProperty("role", "transparent")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(18, 14, 18, 14)
        holder_layout.setSpacing(6)
        holder_layout.addWidget(label("AnkerClient uses 7-Zip to unpack downloaded games.", "caption", wrap=True))
        holder_layout.addSpacing(2)
        self.seven_zip_panel = SevenZipPanel(self._ctx.runner)
        self.seven_zip_panel.path_chosen.connect(self._on_seven_zip_chosen)
        self.seven_zip_panel.auto_detect_requested.connect(self._on_seven_zip_auto)
        holder_layout.addWidget(self.seven_zip_panel)
        tools.add_row(holder)
        def refresh_seven_zip(s: Settings) -> None:
            if s.seven_zip_path != self._seven_zip_seen:
                self._seven_zip_seen = s.seven_zip_path
                if "downloads" in self._loaded:
                    self._load_seven_zip()

        self._seven_zip_seen = self._settings().seven_zip_path
        self._bind("seven_zip_path", refresh_seven_zip)

    def _refresh_speed(self, s: Settings) -> None:
        kbps = s.speed_limit_kbps
        index = self.speed_combo.findData(kbps)
        # Stay in "Custom" while the user is editing it, even if the value equals a preset.
        custom = index < 0 or (self._speed_custom and kbps != 0)
        self._speed_custom = custom
        with QSignalBlocker(self.speed_combo):
            self.speed_combo.setCurrentIndex(self.speed_combo.findData(CUSTOM_SPEED) if custom else index)
        if kbps:
            with QSignalBlocker(self.speed_spin):
                self.speed_spin.setValue(max(0.1, kbps / 1024))
        self.speed_spin.setVisible(custom)

    def _on_speed_choice(self, _index: int) -> None:
        value = self.speed_combo.currentData()
        if value == CUSTOM_SPEED:
            self._speed_custom = True
            self.speed_spin.setVisible(True)
            self.speed_spin.setFocus()
            self._save(speed_limit_kbps=max(1, round(self.speed_spin.value() * 1024)))
        else:
            self._speed_custom = False
            self._save(speed_limit_kbps=int(value))

    def _choose_download_dir(self) -> None:
        start = self._settings().download_dir or self._settings().default_library
        path = QFileDialog.getExistingDirectory(self, "Choose a download folder", start)
        if not path:
            return
        self.download_dir_button.setEnabled(False)

        def checked(result: Any) -> None:
            self.download_dir_button.setEnabled(True)
            if result.ok:
                self._save(download_dir=result.path)
            else:
                self._nav.toast(f"That download folder cannot be used. {result.message}", "error")

        def failed(exc: BaseException) -> None:
            self.download_dir_button.setEnabled(True)
            self._nav.toast(f"Could not check the folder: {error_text(exc)}", "error")

        self._run("download_dir", background(check_download_folder, path), on_result=checked, on_error=failed)

    def _load_downloads(self) -> None:
        self._load_seven_zip()

    def _load_seven_zip(self) -> None:
        self.seven_zip_panel.detect(self._settings().seven_zip_path)

    def _on_seven_zip_chosen(self, path: str) -> None:
        if os.path.normcase(path) != os.path.normcase(self._settings().seven_zip_path):
            self._save(seven_zip_path=path)

    def _on_seven_zip_auto(self) -> None:
        if self._settings().seven_zip_path:
            self._save(seven_zip_path="")  # the binding re-runs detection
        else:
            self._load_seven_zip()

    # =====================================================================================
    # Account
    # =====================================================================================
    def _build_account(self, section: _Section) -> None:
        card = SettingsGroup()
        top = QWidget()
        top.setProperty("role", "transparent")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(20, 18, 20, 18)
        top_layout.setSpacing(16)
        self.avatar = Avatar(52)
        top_layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.account_name = label("", "title")
        self.account_badge = Badge("Subscriber", "accent")
        name_row.addWidget(self.account_name)
        name_row.addWidget(self.account_badge)
        name_row.addStretch(1)
        text.addLayout(name_row)
        self.account_detail = label("", "muted", wrap=True)
        text.addWidget(self.account_detail)
        top_layout.addLayout(text, 1)
        self.sign_in_button = button("Sign in", "login", variant="primary", on_click=self._nav.request_login)
        self._icons.bind(self.sign_in_button, "login", "accent_text", 16)
        self.profile_button = button("Account page", "external", size="sm", on_click=self._open_profile)
        self._icons.bind(self.profile_button, "external", "text", 16)
        self.sign_out_button = button("Sign out", "logout", variant="danger", size="sm", on_click=self._sign_out)
        self._icons.bind(self.sign_out_button, "logout", "danger", 16)
        top_layout.addWidget(self.sign_in_button, 0, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(self.profile_button, 0, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(self.sign_out_button, 0, Qt.AlignmentFlag.AlignVCenter)
        card.add_row(top)
        section.add(card)

        signin = section.add_group("Sign-in")
        signin.add_row(SettingRow("Remember my sign-in", "Keep your password in Windows Credential Manager so "
                                                         "AnkerClient signs in automatically.",
                                  self._toggle("remember_login")))
        signin.add_row(SettingRow("AnkerGames website", "Manage your account, password and subscription on "
                                                        "ankergames.net.",
                                  self._link_button("Open website", BASE_URL)))
        self._show_user(self._current_user())

    def _link_button(self, text: str, url: str) -> QPushButton:
        btn = button(text, "external", size="sm", on_click=lambda: open_url(url))
        self._icons.bind(btn, "external", "text", 16)
        return btn

    def _current_user(self) -> UserInfo | None:
        try:
            return self._ctx.auth.user
        except Exception:
            log.debug("Could not read the signed-in user", exc_info=True)
            return None

    def _show_user(self, user: UserInfo | None) -> None:
        signed_in = user is not None
        if user is not None:
            self.avatar.set_name(user.display_name or user.email)
            self.account_name.setText(user.display_name or user.email or "Signed in")
            self.account_detail.setText(user.email or "Signed in to AnkerGames")
            self.account_badge.setVisible(user.is_subscriber)
        else:
            self.avatar.set_name("")
            self.account_name.setText("Not signed in")
            self.account_detail.setText("Sign in to download with your AnkerGames account.")
            self.account_badge.hide()
        self.sign_in_button.setVisible(not signed_in)
        self.profile_button.setVisible(signed_in)
        self.sign_out_button.setVisible(signed_in)
        self.sign_out_button.setEnabled(True)
        self.sign_out_button.setText("Sign out")

    def _on_auth_changed(self, user: object) -> None:
        if self.is_built("account"):
            self._show_user(user if isinstance(user, UserInfo) else None)

    def _open_profile(self) -> None:
        user = self._current_user()
        open_url(user.profile_url if user is not None and user.profile_url else BASE_URL)

    def _sign_out(self) -> None:
        self.sign_out_button.setEnabled(False)
        self.sign_out_button.setText("Signing out…")

        def done(_result: object) -> None:
            self._show_user(self._current_user())
            self._nav.toast("Signed out of AnkerGames.", "success")

        def failed(exc: BaseException) -> None:
            self._show_user(self._current_user())
            self._nav.toast(f"Could not sign out: {error_text(exc)}", "error")

        self._run("sign_out", self._ctx.auth.logout, on_result=done, on_error=failed)

    # =====================================================================================
    # Appearance
    # =====================================================================================
    def _build_appearance(self, section: _Section) -> None:
        section.add(group_heading("Theme"))
        self.theme_picker = ThemePicker(self._settings().theme, columns=3)
        self.theme_picker.theme_selected.connect(self._apply_theme)
        section.add(self.theme_picker)
        note = label("Wallpaper themes show an image behind translucent panels. Animated wallpapers pause while "
                     "the window is minimized.", "caption", wrap=True)
        section.add(note)
        self._bind("theme", lambda s: self.theme_picker.set_current(s.theme))

    def _apply_theme(self, key: str) -> None:
        if key != self._theme.current_key:
            self._theme.apply(key)
        self._save(theme=key)

    def _on_theme_changed(self, key: str) -> None:
        self._icons.retint()
        for line in self.findChildren(StatusLine):
            line.retint()
        if self.is_built("library"):
            self.folders_editor.retint()
        if self.is_built("downloads"):
            self.seven_zip_panel.retint()
        if self.is_built("appearance"):
            self.theme_picker.set_current(self._settings().theme or key)
        if self.is_built("account"):
            self.avatar.update()
        for widget in self.findChildren(ToggleSwitch):
            widget.update()

    # =====================================================================================
    # Advanced
    # =====================================================================================
    def _build_advanced(self, section: _Section) -> None:
        catalog = section.add_group("Store index")
        self.sync_button = button("Sync now", "refresh", size="sm", on_click=self.sync_catalog)
        self._icons.bind(self.sync_button, "refresh", "text", 16)
        self.sync_cancel_button = button("Cancel", variant="ghost", size="sm", on_click=self._cancel_sync)
        self.sync_cancel_button.hide()
        self.catalog_row = SettingRow("Game index", "Loading…", self.sync_cancel_button, self.sync_button)
        catalog.add_row(self.catalog_row)
        progress_holder = QWidget()
        progress_holder.setProperty("role", "transparent")
        progress_layout = QVBoxLayout(progress_holder)
        progress_layout.setContentsMargins(18, 0, 18, 14)
        progress_layout.setSpacing(6)
        self.sync_progress = QProgressBar()
        self.sync_progress.setTextVisible(False)
        self.sync_status = label("", "caption")
        progress_layout.addWidget(self.sync_progress)
        progress_layout.addWidget(self.sync_status)
        self.sync_progress_holder = progress_holder
        progress_holder.hide()
        catalog.add_row(progress_holder, divider=False)

        cache = section.add_group("Image cache")
        self.clear_cache_button = button("Clear cache", "trash", size="sm", on_click=self.clear_image_cache)
        self._icons.bind(self.clear_cache_button, "trash", "text", 16)
        self.cache_row = SettingRow("Covers and screenshots", "Calculating…", self.clear_cache_button)
        cache.add_row(self.cache_row)

        trouble = section.add_group("Troubleshooting")
        trouble.add_row(SettingRow("Log detail", "Use Detailed when reporting a problem.",
                                   self._choice("log_level", LOG_LEVELS, str, width=220,
                                                after=lambda v: logging_setup.set_level(str(v)))))
        logs_button = button("Open logs folder", "folder", size="sm",
                             on_click=lambda: open_local_path(self._ctx.paths.logs_dir))
        self._icons.bind(logs_button, "folder", "text", 16)
        trouble.add_row(SettingRow("Logs", "Attach the latest log file when you report an issue.", logs_button))
        data_button = button("Open data folder", "folder", size="sm",
                             on_click=lambda: open_local_path(self._ctx.paths.config_dir))
        self._icons.bind(data_button, "folder", "text", 16)
        trouble.add_row(SettingRow("App data", "Settings, the game database and your saved session.", data_button))

        reset = section.add_group("Reset")
        wizard_button = button("Run setup…", "sparkles", size="sm", on_click=self.run_setup_wizard)
        self._icons.bind(wizard_button, "sparkles", "text", 16)
        reset.add_row(SettingRow("Setup wizard", "Choose the library folder, 7-Zip and preferences again.",
                                 wizard_button))
        self.reset_button = button("Reset settings…", variant="danger", size="sm", on_click=self.reset_settings)
        reset.add_row(SettingRow("Reset all settings", "Restore the defaults. Your library folders, installed "
                                                       "games and sign-in are kept.", self.reset_button))

    def _load_advanced(self) -> None:
        self._load_catalog_stats()
        self._load_cache_size()

    def _load_catalog_stats(self) -> None:
        def stats(*, token: CancelToken) -> tuple[int, str]:
            return self._ctx.catalog.count(), self._ctx.catalog.last_synced()

        def show(result: tuple[int, str]) -> None:
            count, synced = result
            when = f"updated {format_relative_time(synced)}" if synced else "never synced"
            self.catalog_row.set_description(f"{pluralize(count, 'game')} · {when}. The index powers instant "
                                             "search and update checks.")

        self._run("catalog_stats", stats, on_result=show,
                  on_error=lambda exc: self.catalog_row.set_description(f"Unavailable: {error_text(exc)}"))

    def sync_catalog(self) -> None:
        if self._sync_handle is not None:
            return
        self.ensure_section("advanced")
        self.sync_button.setEnabled(False)
        self.sync_button.setText("Syncing…")
        self.sync_cancel_button.show()
        self._show_sync_progress(0, None)

        def done(new_games: object) -> None:
            count = new_games if isinstance(new_games, int) else 0
            self._nav.toast(f"Store index updated · {pluralize(count, 'new game')}." if count
                            else "Store index is up to date.", "success")

        def failed(exc: BaseException) -> None:
            self._nav.toast(f"Store sync failed: {error_text(exc)}", "error")

        self._sync_handle = run_async(self, self._ctx.runner, self._ctx.catalog.sync, full=True,
                                      on_result=done, on_error=failed, on_finished=self._sync_finished)

    def _cancel_sync(self) -> None:
        if self._sync_handle is not None:
            self._sync_handle.cancel("user")
            self.sync_status.setText("Cancelling…")

    def _sync_finished(self) -> None:
        self._sync_handle = None
        self.sync_button.setEnabled(True)
        self.sync_button.setText("Sync now")
        self.sync_cancel_button.hide()
        self.sync_progress_holder.hide()
        self._load_catalog_stats()

    def _show_sync_progress(self, done: int, total: object) -> None:
        self.sync_progress_holder.show()
        if isinstance(total, int) and total > 0:
            self.sync_progress.setRange(0, total)
            self.sync_progress.setValue(min(done, total))
            self.sync_status.setText(f"Syncing the store index… page {done} of {total}")
        else:
            self.sync_progress.setRange(0, 0)  # indeterminate
            self.sync_status.setText(f"Syncing the store index… page {done}" if done else
                                     "Syncing the store index…")

    def _on_sync_progress(self, done: int, total: object) -> None:
        if not self.is_built("advanced"):
            return
        self._show_sync_progress(done, total)
        self._sync_idle_timer.start()

    def _on_sync_idle(self) -> None:
        if self._sync_handle is None and self.is_built("advanced"):
            self.sync_progress_holder.hide()

    def _on_catalog_updated(self, _event: object) -> None:
        self._sync_idle_timer.stop()
        if not self.is_built("advanced"):
            return
        if self._sync_handle is None:
            self.sync_progress_holder.hide()
        self._load_catalog_stats()

    def _load_cache_size(self) -> None:
        self._run("cache_size", background(self._ctx.images.size_bytes),
                  on_result=lambda size: self.cache_row.set_description(f"Using {format_bytes(size)} on disk."),
                  on_error=lambda exc: self.cache_row.set_description(f"Size unknown ({error_text(exc)})."))

    def clear_image_cache(self) -> None:
        self.ensure_section("advanced")
        self.clear_cache_button.setEnabled(False)
        self.cache_row.set_description("Clearing…")

        def done(_r: object) -> None:
            self.clear_cache_button.setEnabled(True)
            self._nav.toast("Image cache cleared.", "success")
            self._load_cache_size()

        def failed(exc: BaseException) -> None:
            self.clear_cache_button.setEnabled(True)
            self._nav.toast(f"Could not clear the image cache: {error_text(exc)}", "error")
            self._load_cache_size()

        self._run("cache_clear", background(self._ctx.images.clear), on_result=done, on_error=failed)

    def run_setup_wizard(self) -> None:
        from anker_client.ui.dialogs.first_run import FirstRunWizard

        if self._wizard is not None:
            self._wizard.raise_()
            self._wizard.activateWindow()
            return
        wizard = FirstRunWizard(self._ctx, self._theme, self.window())
        self._wizard = wizard
        wizard.finished.connect(self._on_wizard_finished)
        wizard.open()

    def setup_wizard(self) -> QWidget | None:
        return self._wizard

    def _on_wizard_finished(self, _code: int) -> None:
        wizard, self._wizard = self._wizard, None
        if wizard is not None:
            wizard.deleteLater()
        self._refresh_all()
        self._loaded.discard("downloads")
        self._loaded.discard("library")
        self._load_section(self._current, force=True)

    def reset_settings(self) -> None:
        if not confirm(
            self,
            title="Reset settings",
            text="Reset all settings to their defaults?",
            informative="Your library folders, installed games and sign-in are kept.",
            confirm_text="Reset settings",
        ):
            return
        current = self._settings()
        values = Settings().to_dict()
        for key in RESET_KEEPS:
            values[key] = getattr(current, key)
        if not self._save(**values):
            return
        defaults = Settings()
        if self._theme.current_key != defaults.theme:
            self._theme.apply(defaults.theme)
        logging_setup.set_level(defaults.log_level)
        if current.launch_on_startup and not defaults.launch_on_startup:
            self._run("autostart_reset", background(autostart.set_enabled, False),
                      on_error=lambda exc: log.warning("Could not remove the startup entry: %s", exc))
        self._refresh_all()
        self._nav.toast("Settings were reset to their defaults.", "success")

    # =====================================================================================
    # About
    # =====================================================================================
    def _build_about(self, section: _Section) -> None:
        card = SettingsGroup()
        top = QWidget()
        top.setProperty("role", "transparent")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(22, 20, 22, 20)
        top_layout.setSpacing(18)
        logo = QLabel()
        logo.setFixedSize(64, 64)
        pixmap = QPixmap(str(resource_path("icon.png")))
        if not pixmap.isNull():
            scaled = pixmap.scaled(QSize(128, 128), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo.setPixmap(scaled)
        top_layout.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(label("AnkerClient", "title"))
        self.version_label = label(f"Version {__version__}", "muted")
        text.addWidget(self.version_label)
        text.addWidget(label("Unofficial desktop client and download manager for AnkerGames.", "caption", wrap=True))
        text.addSpacing(10)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.update_button = button("Check for updates", "update", variant="primary", size="sm",
                                    on_click=self.check_for_updates)
        self._icons.bind(self.update_button, "update", "accent_text", 16)
        self.download_update_button = button("Download update", "download", variant="success", size="sm",
                                             on_click=self._open_release)
        self._icons.bind(self.download_update_button, "download", "accent_text", 16)
        self.download_update_button.hide()
        actions.addWidget(self.update_button)
        actions.addWidget(self.download_update_button)
        actions.addStretch(1)
        text.addLayout(actions)
        self.update_status = StatusLine("", "info")
        self.update_status.hide()
        text.addWidget(self.update_status)
        top_layout.addLayout(text, 1)
        card.add_row(top)
        section.add(card)

        links = section.add_group("Links")
        links.add_row(SettingRow("Source code", "AnkerClient is open source on GitHub.",
                                 self._link_button("GitHub", GITHUB_URL)))
        links.add_row(SettingRow("Release notes", "What changed in each version.",
                                 self._link_button("Releases", RELEASES_URL)))
        links.add_row(SettingRow("Report a problem", "Open an issue and attach your log file.",
                                 self._link_button("Report an issue", ISSUES_URL)))
        links.add_row(SettingRow("AnkerGames", "The game site this client works with.",
                                 self._link_button("ankergames.net", BASE_URL)))

        legal = section.add_group("Legal")
        disclaimer = SettingRow("Disclaimer", DISCLAIMER)
        legal.add_row(disclaimer)
        legal.add_row(SettingRow("Third-party software", THIRD_PARTY_NOTICE))
        legal.add_row(SettingRow("Licence", "AnkerClient is released under the MIT License."))

    def check_for_updates(self) -> None:
        self.ensure_section("about")
        self.update_button.setEnabled(False)
        self.update_status.set_status("Checking for updates…", "busy")
        self.update_status.show()

        def done(release: object) -> None:
            self.update_button.setEnabled(True)
            if isinstance(release, AppRelease):
                self._show_release(release)
            else:
                self.update_status.set_status(f"You're up to date. Version {__version__} is the latest.", "success")

        def failed(exc: BaseException) -> None:
            self.update_button.setEnabled(True)
            self.update_status.set_status(f"Could not check for updates: {error_text(exc)}", "error")

        self._run("app_update", self._ctx.app_updates.check, on_result=done, on_error=failed)

    def _show_release(self, release: object) -> None:
        if not isinstance(release, AppRelease):
            return
        self._release = release
        if not self.is_built("about"):
            return  # shown when the section is first built
        self.update_status.set_status(f"Version {release.version} is available (you have {__version__}).", "info")
        self.update_status.show()
        self.download_update_button.show()
        # One primary action at a time: downloading the update is now the thing to do.
        if self.update_button.property("variant") != "secondary":
            self.update_button.setProperty("variant", "secondary")
            self._icons.bind(self.update_button, "update", "text", 16)
            repolish(self.update_button)

    def _open_release(self) -> None:
        if self._release is not None:
            open_url(self._release.download_url or self._release.url or RELEASES_URL)

    # =====================================================================================
    # live updates / dynamic loading
    # =====================================================================================
    def _on_settings_changed(self, keys: object) -> None:
        if isinstance(keys, frozenset | set) and keys:
            self._refresh_keys(frozenset(keys))
        else:
            self._refresh_all()

    def _after_build(self, key: str) -> None:
        if key == "about" and self._release is not None:
            self._show_release(self._release)

    def _load_section(self, key: str, *, force: bool = False) -> None:
        if key in self._loaded and not force:
            return
        self._loaded.add(key)
        loader = {
            "general": self._load_general,
            "library": self._load_library,
            "downloads": self._load_downloads,
            "account": lambda: self._show_user(self._current_user()),
            "advanced": self._load_advanced,
        }.get(key)
        if loader is not None:
            loader()
