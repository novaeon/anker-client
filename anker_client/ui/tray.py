"""System tray icon.

Tooltip: download progress (``shell_summary.tray_tooltip``). Menu: Open
AnkerClient, Recently played (up to 5 installed games by last played → launch
through ``ctx.launcher``), Pause all / Resume all downloads (enabled to match
the queue), Quit AnkerClient. Left click / double click opens the window;
clicking a balloon does too.

When the platform has no system tray (some Linux sessions, the offscreen QPA
used by tests) ``available`` is False: the menu is still built (so it can be
inspected) but nothing is shown, and the main window falls back to minimising.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from anker_client.constants import APP_NAME
from anker_client.core.models import InstalledGame
from anker_client.core.tasks import CancelToken
from anker_client.ui import icons
from anker_client.ui.async_ import run_async
from anker_client.ui.shell_summary import DownloadSummary, tray_tooltip

log = logging.getLogger(__name__)

RECENT_LIMIT = 5
_MESSAGE_ICONS = {
    "info": QSystemTrayIcon.MessageIcon.Information,
    "success": QSystemTrayIcon.MessageIcon.Information,
    "warning": QSystemTrayIcon.MessageIcon.Warning,
    "error": QSystemTrayIcon.MessageIcon.Critical,
}


def recently_played(games: list[InstalledGame], limit: int = RECENT_LIMIT) -> list[InstalledGame]:
    """Played, launchable, visible games, most recent first."""

    oldest = datetime.min.replace(tzinfo=UTC)

    def key(game: InstalledGame) -> datetime:
        try:
            when = datetime.fromisoformat(game.last_played.replace("Z", "+00:00"))
        except ValueError:
            return oldest
        return when if when.tzinfo is not None else when.replace(tzinfo=UTC)

    played = [g for g in games if g.last_played and g.executable and not g.hidden]
    played.sort(key=key, reverse=True)
    return played[:limit]


class TrayController(QObject):
    open_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    launch_failed = pyqtSignal(str, object)  # install id, exception

    def __init__(self, ctx: object, icon: QIcon, parent: QObject | None = None, *,
                 available: bool | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._available = QSystemTrayIcon.isSystemTrayAvailable() if available is None else available
        self._summary = DownloadSummary()
        self.tray: QSystemTrayIcon | None = None
        self.last_message: tuple[str, str, str] | None = None
        self.menu = QMenu()
        self._build_menu()
        if self._available:
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setContextMenu(self.menu)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(self._on_activated)
            self.tray.messageClicked.connect(self.open_requested.emit)
            self.tray.setToolTip(tray_tooltip(self._summary, APP_NAME))

    @property
    def available(self) -> bool:
        return self._available

    # --- menu -----------------------------------------------------------------------------------
    def _build_menu(self) -> None:
        self.open_action = QAction(f"Open {APP_NAME}", self.menu)
        font = QFont(self.open_action.font())
        font.setBold(True)
        self.open_action.setFont(font)
        self.open_action.triggered.connect(lambda _checked=False: self.open_requested.emit())
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()
        self.recent_menu = QMenu("Recently played", self.menu)
        self.menu.addMenu(self.recent_menu)
        self.menu.addSeparator()
        self.pause_action = QAction("Pause all downloads", self.menu)
        self.pause_action.triggered.connect(lambda _checked=False: self._run(self._ctx.downloads.pause_all))
        self.resume_action = QAction("Resume all downloads", self.menu)
        self.resume_action.triggered.connect(lambda _checked=False: self._run(self._ctx.downloads.resume_all))
        self.menu.addAction(self.pause_action)
        self.menu.addAction(self.resume_action)
        self.menu.addSeparator()
        self.quit_action = QAction(f"Quit {APP_NAME}", self.menu)
        self.quit_action.triggered.connect(lambda _checked=False: self.quit_requested.emit())
        self.menu.addAction(self.quit_action)
        self.menu.aboutToShow.connect(self.refresh_recent)
        self.refresh_icons()
        self.set_summary(self._summary)
        self.refresh_recent()

    def refresh_icons(self) -> None:
        self.open_action.setIcon(icons.icon("home"))
        self.recent_menu.setIcon(icons.icon("clock"))
        self.pause_action.setIcon(icons.icon("pause"))
        self.resume_action.setIcon(icons.icon("resume"))
        self.quit_action.setIcon(icons.icon("close"))

    def refresh_recent(self) -> None:
        self.recent_menu.clear()
        try:
            games = recently_played(self._ctx.library.games(include_hidden=False))
        except Exception:
            log.debug("Library unavailable for the tray menu", exc_info=True)
            games = []
        if not games:
            empty = self.recent_menu.addAction("No games played yet")
            empty.setEnabled(False)
            return
        for game in games:
            action = self.recent_menu.addAction(icons.icon("play"), game.title)
            action.triggered.connect(partial(self._launch_from_menu, game.install_id))

    def recent_titles(self) -> list[str]:
        return [a.text() for a in self.recent_menu.actions() if a.isEnabled()]

    # --- state ------------------------------------------------------------------------------------
    def set_summary(self, summary: DownloadSummary) -> None:
        self._summary = summary
        self.pause_action.setEnabled(summary.in_progress > 0)
        self.resume_action.setEnabled(summary.paused > 0)
        if self.tray is not None:
            self.tray.setToolTip(tray_tooltip(summary, APP_NAME))

    def tooltip(self) -> str:
        return tray_tooltip(self._summary, APP_NAME)

    def show(self) -> None:
        if self.tray is not None:
            self.tray.show()

    def hide(self) -> None:
        if self.tray is not None:
            self.tray.hide()

    def show_message(self, title: str, message: str, level: str = "info", *, timeout_ms: int = 6000) -> bool:
        """Balloon / Windows notification. Returns False when no tray is available."""
        self.last_message = (title, message, level)
        if self.tray is None or not self.tray.isVisible():
            return False
        self.tray.showMessage(title, message, _MESSAGE_ICONS.get(level, _MESSAGE_ICONS["info"]), timeout_ms)
        return True

    # --- actions -----------------------------------------------------------------------------------
    def _launch_from_menu(self, install_id: str, _checked: bool = False) -> None:
        self.launch(install_id)

    def launch(self, install_id: str) -> None:
        def work(*, token: CancelToken) -> None:
            self._ctx.launcher.launch(install_id)

        run_async(self, self._ctx.runner, work,
                  on_error=lambda exc, gid=install_id: self.launch_failed.emit(gid, exc))

    def _run(self, fn: object) -> None:
        def work(*, token: CancelToken) -> None:
            fn()  # type: ignore[operator]

        run_async(self, self._ctx.runner, work,
                  on_error=lambda exc: log.warning("Tray action failed: %s", exc))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    def shutdown(self) -> None:
        if self.tray is not None:
            self.tray.hide()
        self.menu.deleteLater()
