"""Main window: sidebar navigation, page stack, header, status, tray, wallpaper. Implements Navigator.

Layout (docs/ARCHITECTURE.md §UI → Shell)::

    ┌──────────┬──────────────────────────────────────────────┐
    │ Sidebar  │ Header: ← back · search · sync · "3 updates" │
    │          ├──────────────────────────────────────────────┤
    │          │ QStackedWidget (Store, Game, Library,        │
    │          │   Downloads, Settings — built lazily)  toasts│
    ├──────────┴──────────────────────────────────────────────┤
    │ Status strip: download summary · Playing X · version    │
    └─────────────────────────────────────────────────────────┘

* Pages are created on first use through ``shell_pages.create_page``; a page
  that fails to import/construct is replaced by an error placeholder with
  "Try again", so the shell never dies because of one page.
* Navigation history (``back``; Alt+Left / mouse back button): every
  ``show_*`` call records a :class:`Location`; consecutive store locations
  (search-as-you-type, genre switches) replace each other. ``show_store(query=…)``
  / ``show_store(genre=…)`` always reach the store page (it may have left a
  search on its own); ``show_store()`` shows the store as the user left it.
* Live chrome: library count + updates chip from ``LibraryService.games()``
  (re-read on ``library_changed``, hidden games excluded); downloads
  badge/progress/status/tray from job events (throttled to 4 Hz); sync
  indicator; account chip; running game.
* ``UpdatesFound`` toasts only updates not announced yet: updates already
  flagged in the library (``mark_updates_known``, called again by ``app.main``
  after the startup scan) and updates of hidden games are not news.
* ``Notification`` events become toasts (+ tray balloon when the window is
  hidden, ``tray=True`` and notifications are enabled); ``GameInstalled`` with
  ``needs_executable`` offers "Choose executable"; ``AppUpdateAvailable``
  offers "Download" (release page) and shows a header chip.
* Close: ``settings.close_behavior`` — ask (dialog with "Remember my choice"),
  tray (hide; minimise when there is no tray) or quit. Quitting while
  downloads run asks first ("Downloads will pause and resume next time").
* Shortcuts: Ctrl+F search, Ctrl+1..4 pages, Alt+Left back, F5 refresh page
  (``page.refresh()`` when present, else ``on_activated()``), Ctrl+Q quit.
* Geometry/state saved to ``settings.window_geometry``/``window_state`` (base64)
  when hiding/quitting; ``reset_window=True`` ignores and clears them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from PyQt6.QtCore import QByteArray, QEvent, QPoint, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon, QKeySequence, QMouseEvent, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anker_client.constants import APP_NAME, BASE_URL, RELEASES_URL
from anker_client.core import events as ev
from anker_client.core.errors import ExecutableNotSetError
from anker_client.core.logging_setup import set_level
from anker_client.core.models import (
    AppRelease,
    DownloadJob,
    DownloadKind,
    DownloadOption,
    GameDetails,
    GameSummary,
    GameUpdate,
    UserInfo,
)
from anker_client.core.paths import resource_path
from anker_client.core.settings import CLOSE_ASK, CLOSE_QUIT, CLOSE_TRAY
from anker_client.core.tasks import CancelToken
from anker_client.services.container import AppContext
from anker_client.ui import icons, shell_dialogs
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.shell_pages import PAGE_SPECS, PagePlaceholder, call_page, create_page, is_placeholder
from anker_client.ui.shell_summary import DownloadSummary, summarize_jobs
from anker_client.ui.shell_wallpaper import WallpaperSurface
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.tray import TrayController
from anker_client.ui.widgets.header import Header
from anker_client.ui.widgets.sidebar import Sidebar
from anker_client.ui.widgets.status_strip import StatusStrip
from anker_client.ui.widgets.toast import Toast, ToastManager

log = logging.getLogger(__name__)

HISTORY_LIMIT = 50
SUMMARY_THROTTLE_MS = 250
LIBRARY_REFRESH_MS = 150
MIN_SIZE = QSize(1100, 700)
DEFAULT_SIZE = QSize(1280, 800)
NAV_ORDER = ("store", "library", "downloads", "settings")


def open_url(url: str) -> None:
    """Open ``url`` in the default browser (module-level so tests can intercept it)."""
    QDesktopServices.openUrl(QUrl(url))


def quit_application() -> None:
    QApplication.quit()


def app_icon() -> QIcon:
    for name in ("icon.ico", "icon.png"):
        path = resource_path(name)
        if path.exists():
            return QIcon(str(path))
    return QIcon()


@dataclass(frozen=True, slots=True)
class Location:
    page: str
    query: str = ""
    genre: str = ""
    slug: str = ""
    install_id: str = ""
    section: str = ""
    summary: GameSummary | None = field(default=None, compare=False)


class MainWindow(QMainWindow):
    def __init__(
        self,
        ctx: AppContext,
        bridge: QtEventBridge,
        theme: ThemeManager,
        *,
        loader: ImageLoader | None = None,
        reset_window: bool = False,
        tray_available: bool | None = None,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._bridge = bridge
        self._theme = theme
        self._loader = loader if loader is not None else ImageLoader(ctx.images, ctx.runner, self)
        self._pages: dict[str, QWidget] = {}
        self._active_page: QWidget | None = None
        self._history: list[Location] = []
        self._store_query = ""  # last search the shell sent to the store page ("" = none / cleared)
        self._game_slug = ""
        self._jobs: dict[str, DownloadJob] = {}
        self._summary = DownloadSummary()
        self._running: dict[str, str] = {}
        self._announced_updates: set[str] = set()
        self._app_release: AppRelease | None = None
        self._quitting = False
        self._shut_down = False
        self._was_shown = False
        self._tray_hint_shown = False
        self._shortcuts: list[QShortcut] = []

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(MIN_SIZE)
        self._build_ui()
        self._toasts = ToastManager(self._content)
        self._tray = TrayController(ctx, app_icon(), self, available=tray_available)
        self._tray.open_requested.connect(self.show_and_raise)
        self._tray.quit_requested.connect(self.request_quit)
        self._tray.launch_failed.connect(self._on_tray_launch_failed)
        self._tray.show()

        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(SUMMARY_THROTTLE_MS)
        self._summary_timer.timeout.connect(self._apply_summary)
        self._library_timer = QTimer(self)
        self._library_timer.setSingleShot(True)
        self._library_timer.setInterval(LIBRARY_REFRESH_MS)
        self._library_timer.timeout.connect(self._refresh_library)

        self._connect_signals()
        self._install_shortcuts()
        self.restore_window_state(reset=reset_window)
        self._on_theme_changed(theme.current_key)

        self._sidebar.set_user(self._safe(lambda: ctx.auth.user, None))
        self.mark_updates_known()
        self._refresh_library()
        self.reload_jobs()
        self._reload_running()
        self.show_store()

    # =====================================================================================
    # construction
    # =====================================================================================
    def _build_ui(self) -> None:
        self._surface = WallpaperSurface()
        root = QVBoxLayout(self._surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._sidebar = Sidebar()
        body.addWidget(self._sidebar)
        self._content = QWidget()
        self._content.setProperty("role", "transparent")
        column = QVBoxLayout(self._content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._header = Header()
        column.addWidget(self._header)
        self._stack = QStackedWidget()
        self._stack.setProperty("role", "transparent")
        column.addWidget(self._stack, 1)
        body.addWidget(self._content, 1)
        root.addLayout(body, 1)
        self._status = StatusStrip()
        root.addWidget(self._status)
        self.setCentralWidget(self._surface)

    def _connect_signals(self) -> None:
        self._sidebar.navigate.connect(self._on_nav)
        self._sidebar.account_clicked.connect(self._on_account_clicked)
        self._header.back_requested.connect(self.back)
        self._header.search_requested.connect(self._on_search)
        self._header.updates_clicked.connect(self.show_updates)
        self._header.app_update_clicked.connect(self._open_app_release)
        self._status.downloads_clicked.connect(self.show_downloads)
        self._theme.theme_changed.connect(self._on_theme_changed)

        b = self._bridge
        b.job_added.connect(self._on_job)
        b.job_updated.connect(self._on_job)
        b.job_removed.connect(self._on_job_removed)
        b.queue_changed.connect(self.reload_jobs)
        b.library_changed.connect(self._on_library_changed)
        b.game_installed.connect(self._on_game_installed)
        b.game_uninstalled.connect(self._on_library_changed)
        b.game_launched.connect(self._on_game_launched)
        b.game_exited.connect(self._on_game_exited)
        b.catalog_sync_progress.connect(self._header.set_sync_progress)
        b.catalog_updated.connect(self._on_catalog_updated)
        b.updates_found.connect(self._on_updates_found)
        b.app_update_available.connect(self._on_app_update)
        b.auth_changed.connect(self._on_auth_changed)
        b.settings_changed.connect(self._on_settings_changed)
        b.notification.connect(self._on_notification)

        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.commitDataRequest.connect(self._on_session_ending)

    def _install_shortcuts(self) -> None:
        def add(sequence: QKeySequence | str, handler: Callable[[], None]) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

        add("Ctrl+F", self._header.focus_search)
        for number, key in enumerate(NAV_ORDER, start=1):
            add(f"Ctrl+{number}", partial(self._on_nav, key))
        add("Alt+Left", self.back)
        add("F5", self.refresh_current_page)
        add("Ctrl+Q", self.request_quit)

    # =====================================================================================
    # Navigator
    # =====================================================================================
    def show_store(self, *, query: str = "", genre: str = "") -> None:
        query = " ".join(query.split())
        if query:
            loc = Location("store", query=query)
        elif genre:
            loc = Location("store", genre=genre)
        else:
            loc = Location("store")  # the store as the user left it (its tabs/search are its own state)
        self._navigate(loc)

    def show_game(self, slug: str, summary: GameSummary | None = None) -> None:
        if slug:
            self._navigate(Location("game", slug=slug, summary=summary))

    def show_library(self, install_id: str = "") -> None:
        self._navigate(Location("library", install_id=install_id))

    def show_downloads(self) -> None:
        self._navigate(Location("downloads"))

    def show_settings(self, section: str = "") -> None:
        self._navigate(Location("settings", section=section))

    def back(self) -> None:
        if len(self._history) < 2:
            return
        self._history.pop()
        self._navigate(self._history[-1], record=False)

    def show_updates(self) -> None:
        """Library filtered to games with updates (when the library page supports filters)."""
        self.show_library()
        call_page(self._pages.get("library"), "set_filter", "updates")

    def request_install(self, details: GameDetails, option: DownloadOption | None = None) -> None:
        if not details.download_options:
            self.toast(f"No downloads are available for {details.title} right now.", "warning")
            return
        from anker_client.ui.dialogs import install_dialog

        try:
            dialog = install_dialog.InstallDialog(self._ctx, details, option, self, loader=self._loader)
        except Exception as exc:
            log.exception("Could not open the install dialog")
            self.toast(f"Could not open the install options: {error_text(exc)}", "error")
            return
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            chosen = dialog.selected_option()
            library_root = dialog.selected_library()
        finally:
            dialog.deleteLater()
        self._enqueue(details, chosen, library_root)

    def request_login(self) -> None:
        try:
            from anker_client.ui.dialogs import login

            dialog = login.LoginDialog(self._ctx, self)
        except Exception as exc:
            log.exception("Could not open the sign-in dialog")
            self.toast(f"Sign-in is unavailable right now: {error_text(exc)}", "error")
            return
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def choose_executable(self, install_id: str) -> None:
        try:
            from anker_client.ui.dialogs import game_dialogs

            dialog = game_dialogs.ExecutablePickerDialog(self._ctx, install_id, self)
        except Exception as exc:
            log.exception("Could not open the executable picker")
            self.toast(f"Could not open the executable picker: {error_text(exc)}", "error")
            return
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def toast(
        self,
        message: str,
        level: str = "info",
        *,
        title: str = "",
        action_text: str = "",
        on_action: Callable[[], None] | None = None,
        timeout_ms: int | None = None,
    ) -> Toast:
        return self._toasts.show_toast(message, level, title=title, action_text=action_text,
                                       on_action=on_action, timeout_ms=timeout_ms)

    # =====================================================================================
    # navigation internals
    # =====================================================================================
    def _on_nav(self, key: str) -> None:
        {"store": self.show_store, "library": self.show_library, "downloads": self.show_downloads,
         "settings": self.show_settings}.get(key, self.show_store)()

    def _navigate(self, loc: Location, *, record: bool = True) -> None:
        page = self._ensure_page(loc.page)
        if record:
            self._record(loc)
        self._apply_location(page, loc)
        self._switch_to(page)
        self._update_chrome(loc)

    def _record(self, loc: Location) -> None:
        current = self._history[-1] if self._history else None
        if current == loc:
            return
        if current is not None and current.page == "store" and loc.page == "store":
            self._history[-1] = loc  # search refinements / genre switches replace each other
            return
        self._history.append(loc)
        if len(self._history) > HISTORY_LIMIT:
            del self._history[: len(self._history) - HISTORY_LIMIT]

    def _apply_location(self, page: QWidget, loc: Location) -> None:
        if loc.page == "store":
            # Explicit requests always reach the page: it can leave a search or switch genres on its own
            # (search tab close button, genre chips), so a cached "already showing that" would be wrong.
            # The page ignores a repeat of what it already shows.
            if loc.query:
                call_page(page, "set_query", loc.query)
                self._store_query = loc.query
            elif loc.genre:
                if self._store_query:
                    call_page(page, "set_query", "")
                call_page(page, "set_genre", loc.genre)
                self._store_query = ""
        elif loc.page == "game":
            if loc.slug != self._game_slug or is_placeholder(page):
                self._game_slug = loc.slug
                call_page(page, "load", loc.slug, loc.summary)
        elif loc.page == "library":
            if loc.install_id:
                call_page(page, "select", loc.install_id)
        elif loc.page == "settings" and loc.section:
            call_page(page, "show_section", loc.section)

    def _switch_to(self, page: QWidget) -> None:
        # Tracked explicitly: QStackedWidget makes the first added page current on its own.
        old = self._active_page
        if old is page:
            return
        if old is not None:
            call_page(old, "on_deactivated")
        self._active_page = page
        self._stack.setCurrentWidget(page)
        call_page(page, "on_activated")

    def _update_chrome(self, loc: Location) -> None:
        self._sidebar.set_current(self._section_for(loc))
        self._header.set_back_enabled(len(self._history) > 1)
        if loc.page == "store":
            self._header.set_search_text(loc.query or self._store_query)
        elif loc.page != "game":
            self._header.set_search_text("")

    def _section_for(self, loc: Location) -> str:
        if loc.page != "game":
            return loc.page
        for previous in reversed(self._history):
            if previous.page != "game":
                return previous.page
        return "store"

    def _ensure_page(self, key: str) -> QWidget:
        page = self._pages.get(key)
        if page is not None:
            return page
        spec = PAGE_SPECS[key]
        page = create_page(spec, self._ctx, self._bridge, self, self._loader, self._theme)
        if isinstance(page, PagePlaceholder):
            page.retry_requested.connect(self._rebuild_page)
        self._pages[key] = page
        self._stack.addWidget(page)
        return page

    def _rebuild_page(self, key: str) -> None:
        old = self._pages.pop(key, None)
        if old is not None:
            if old is self._active_page:
                self._active_page = None
            self._stack.removeWidget(old)
            old.deleteLater()
        if key == "store":
            self._store_query = ""
        elif key == "game":
            self._game_slug = ""
        current = self._history[-1] if self._history else Location("store")
        if current.page == key:
            self._navigate(current, record=False)
        else:
            self._ensure_page(key)

    def _on_search(self, query: str) -> None:
        if query:
            self.show_store(query=query)
        elif self.current_page_key() == "store" and self._store_query:
            # The box was cleared: leave the search (the store returns to its previous tab).
            self._store_query = ""
            call_page(self._pages.get("store"), "set_query", "")
            loc = Location("store")
            self._record(loc)
            self._update_chrome(loc)

    def refresh_current_page(self) -> None:
        page = self._active_page
        if page is None:
            return
        if is_placeholder(page):
            key = getattr(page, "key", "")
            if key:
                self._rebuild_page(key)
            return
        if callable(getattr(page, "refresh", None)):
            call_page(page, "refresh")
        else:
            call_page(page, "on_activated")

    # --- accessors (tests, app) --------------------------------------------------------------
    def current_page_key(self) -> str:
        return next((key for key, page in self._pages.items() if page is self._active_page), "")

    def page(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    def history(self) -> list[Location]:
        return list(self._history)

    @property
    def header(self) -> Header:
        return self._header

    @property
    def sidebar(self) -> Sidebar:
        return self._sidebar

    @property
    def status_strip(self) -> StatusStrip:
        return self._status

    @property
    def toasts(self) -> ToastManager:
        return self._toasts

    @property
    def tray(self) -> TrayController:
        return self._tray

    @property
    def surface(self) -> WallpaperSurface:
        return self._surface

    @property
    def download_summary(self) -> DownloadSummary:
        return self._summary

    # =====================================================================================
    # install flow
    # =====================================================================================
    def _enqueue(self, details: GameDetails, option: DownloadOption, library_root: str) -> None:
        downloads = self._ctx.downloads
        installed = self._safe(lambda: self._ctx.library.find_by_slug(details.slug), None)
        if option.kind is DownloadKind.PATCH and option.to_version:
            version = option.to_version
        elif option.kind is DownloadKind.ADDON and installed is not None:
            version = installed.version  # add-ons do not change the installed version
        else:
            version = details.version

        def work(*, token: CancelToken) -> tuple[DownloadJob, bool]:
            known = {job.id for job in downloads.jobs()}
            job = downloads.enqueue(
                slug=details.slug,
                title=details.title,
                option=option,
                library_root=library_root,
                cover_url=details.cover_url,
                version=version,
                source_updated_date=details.updated_date,
                genres=list(details.genres),
            )
            return job, job.id in known

        run_async(self, self._ctx.runner, work,
                  on_result=self._on_enqueued,
                  on_error=lambda exc: self.toast(error_text(exc), "error",
                                                  title=f"Could not add {details.title} to downloads"))

    def _on_enqueued(self, result: tuple[DownloadJob, bool]) -> None:
        job, existed = result
        if existed:
            self.toast(f"{job.title} is already in your downloads.", "info",
                       action_text="View", on_action=self.show_downloads)
        else:
            self.toast(f"{job.title} · {job.option.label}", "success", title="Added to downloads",
                       action_text="View", on_action=self.show_downloads)

    # =====================================================================================
    # live updates
    # =====================================================================================
    def reload_jobs(self) -> None:
        jobs = self._safe(self._ctx.downloads.jobs, [])
        self._jobs = {job.id: job for job in jobs}
        self._apply_summary()

    def _on_job(self, job: DownloadJob) -> None:
        self._jobs[job.id] = job
        self._schedule_summary()

    def _on_job_removed(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._schedule_summary()

    def _schedule_summary(self) -> None:
        if not self._summary_timer.isActive():  # throttle, never starve: do not restart a running timer
            self._summary_timer.start()

    def _apply_summary(self) -> None:
        self._summary = summarize_jobs(self._jobs.values())
        self._sidebar.set_downloads(self._summary)
        self._status.set_summary(self._summary)
        self._tray.set_summary(self._summary)

    def _on_library_changed(self, _payload: object = None) -> None:
        self._schedule_library_refresh()

    def _on_catalog_updated(self, event: ev.CatalogUpdated) -> None:
        self._header.set_sync_finished(event.new_games)

    def _schedule_library_refresh(self) -> None:
        if not self._library_timer.isActive():
            self._library_timer.start()

    def _refresh_library(self) -> None:
        games = self._safe(lambda: self._ctx.library.games(include_hidden=True), None)
        if games is None:
            return
        visible = [g for g in games if not g.hidden]
        self._sidebar.set_library_count(len(visible))
        self._header.set_updates(sum(1 for g in visible if g.update_available))
        # An update that was installed (flag cleared) is news again the next time one is found.
        # Never add here: a running check flags games before its UpdatesFound event arrives.
        self._announced_updates &= {g.install_id for g in games if g.update_available}
        titles = {g.install_id: g.title for g in games}
        if any(titles.get(gid, title) != title for gid, title in self._running.items()):
            self._running = {gid: titles.get(gid, title) for gid, title in self._running.items()}
            self._status.set_running(list(self._running.values()))

    def _current_update_ids(self) -> set[str]:
        games = self._safe(lambda: self._ctx.library.games(include_hidden=True), [])
        return {g.install_id for g in games if g.update_available}

    def _hidden_ids(self) -> set[str]:
        games = self._safe(lambda: self._ctx.library.games(include_hidden=True), [])
        return {g.install_id for g in games if g.hidden}

    def mark_updates_known(self) -> None:
        """Treat the updates flagged in the library right now as already announced.

        Called at construction and by ``app.main`` once the startup chain (which includes the
        library scan that loads the flags saved by earlier checks) has finished, so the next
        ``UpdatesFound`` only toasts updates that are actually new.
        """
        self._announced_updates |= self._current_update_ids()

    def _on_updates_found(self, updates: tuple[GameUpdate, ...]) -> None:
        hidden = self._hidden_ids()  # the user hid those games: their updates are not news either
        new = [u for u in updates if u.install_id not in self._announced_updates and u.install_id not in hidden]
        self._announced_updates.update(u.install_id for u in updates)
        self._schedule_library_refresh()
        if not new:
            return
        message = f"{new[0].title} can be updated to {new[0].latest_version or 'a newer version'}." \
            if len(new) == 1 else f"{len(new)} games in your library have updates."
        self.toast(message, "info", title="Updates available", action_text="View", on_action=self.show_updates)
        if self._is_hidden() and self._notifications_enabled():
            self._tray.show_message("Updates available", message, "info")

    def _on_app_update(self, release: AppRelease) -> None:
        self._app_release = release
        self._header.set_app_update(release.version)
        self.toast(f"Version {release.version} is ready to download.", "info", title="AnkerClient update available",
                   action_text="Download", on_action=self._open_app_release)

    def _open_app_release(self) -> None:
        release = self._app_release
        open_url(release.url if release is not None and release.url else RELEASES_URL)

    def _on_game_installed(self, event: ev.GameInstalled) -> None:
        self._schedule_library_refresh()
        if not event.needs_executable:
            return
        self.toast(f"Choose which program starts {event.title}.", "warning", title=f"{event.title} is installed",
                   action_text="Choose executable", on_action=lambda: self.choose_executable(event.install_id),
                   timeout_ms=12_000)
        if self._is_hidden() and self._notifications_enabled():
            self._tray.show_message(f"{event.title} is installed",
                                    "Open AnkerClient to choose which program starts the game.", "warning")

    def _on_game_launched(self, event: ev.GameLaunched) -> None:
        self._running[event.install_id] = event.title
        self._status.set_running(list(self._running.values()))
        settings = self._ctx.settings.get()
        if settings.minimize_on_game_launch and self.isVisible() and not self.isMinimized():
            self.showMinimized()

    def _on_game_exited(self, event: ev.GameExited) -> None:
        self._running.pop(event.install_id, None)
        self._status.set_running(list(self._running.values()))

    def _reload_running(self) -> None:
        ids = self._safe(self._ctx.launcher.running, set())
        running: dict[str, str] = {}
        for install_id in sorted(ids):
            game = self._safe(lambda gid=install_id: self._ctx.library.get(gid), None)
            running[install_id] = game.title if game is not None else install_id
        self._running = running
        self._status.set_running(list(running.values()))

    def _on_auth_changed(self, user: UserInfo | None) -> None:
        self._sidebar.set_user(user)

    def _on_settings_changed(self, keys: frozenset[str]) -> None:
        settings = self._ctx.settings.get()
        if "theme" in keys and settings.theme != self._theme.current_key:
            self._theme.apply(settings.theme)
        if "log_level" in keys:
            set_level(settings.log_level)
        if "show_hidden_games" in keys:
            self._schedule_library_refresh()

    def _on_notification(self, event: ev.Notification) -> None:
        self.toast(event.message, event.level, title=event.title)
        if event.tray and self._is_hidden() and self._notifications_enabled():
            self._tray.show_message(event.title, event.message, event.level)

    def _on_tray_launch_failed(self, install_id: str, exc: BaseException) -> None:
        if isinstance(exc, ExecutableNotSetError):
            self.show_and_raise()
            self.choose_executable(install_id)
            return
        game = self._safe(lambda: self._ctx.library.get(install_id), None)
        title = game.title if game is not None else "The game"
        if self._is_hidden():
            self._tray.show_message(f"{title} could not start", error_text(exc), "error")
        self.toast(error_text(exc), "error", title=f"{title} could not start")

    def _on_theme_changed(self, _key: str) -> None:
        self._surface.set_wallpaper(palette.current().wallpaper)
        self._sidebar.refresh_icons()
        self._header.refresh_icons()
        self._status.refresh_icons()
        self._tray.refresh_icons()
        self._surface.update()

    # --- account -------------------------------------------------------------------------------
    def _on_account_clicked(self) -> None:
        user = self._safe(lambda: self._ctx.auth.user, None)
        if user is None:
            self.request_login()
            return
        menu = self.account_menu(user)
        chip = self._sidebar.account
        menu.popup(chip.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))

    def account_menu(self, user: UserInfo) -> QMenu:
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        header = menu.addAction(user.display_name or user.email or "Signed in")
        header.setEnabled(False)
        menu.addSeparator()
        menu.addAction(icons.icon("external"), "Open profile on website",
                       lambda: open_url(user.profile_url or BASE_URL))
        menu.addAction(icons.icon("settings"), "Account settings", lambda: self.show_settings("account"))
        menu.addSeparator()
        menu.addAction(icons.icon("logout"), "Sign out", self.sign_out)
        return menu

    def sign_out(self) -> None:
        auth = self._ctx.auth
        run_async(self, self._ctx.runner, lambda *, token: auth.logout(token=token),
                  on_result=lambda _r: self.toast("You are signed out.", "info"),
                  on_error=lambda exc: self.toast(error_text(exc), "error", title="Sign-out failed"))

    # =====================================================================================
    # window lifecycle
    # =====================================================================================
    def show_and_raise(self) -> None:
        if self.isMinimized():
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    def handle_instance_message(self, message: str) -> None:
        if message.strip().lower() == "show":
            self.show_and_raise()
        else:
            log.debug("Ignoring unknown instance message %r", message)

    def on_services_started(self) -> None:
        """Called by ``app.main`` after ``ctx.start()``: re-read state the services just loaded."""
        self.reload_jobs()
        self._reload_running()
        self._schedule_library_refresh()

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802
        if event is None:
            return
        if self._quitting:
            event.accept()
            return
        event.ignore()
        behavior = self._ctx.settings.get().close_behavior
        if behavior == CLOSE_ASK:
            decision = shell_dialogs.ask_close_action(self, tray_available=self._tray.available)
            if decision is None:
                return
            if decision.remember:
                self._ctx.settings.update(close_behavior=decision.choice.value)
            behavior = decision.choice.value
        if behavior == CLOSE_TRAY:
            self.hide_to_tray()
        elif behavior == CLOSE_QUIT:
            self.request_quit()

    def hide_to_tray(self) -> None:
        self.save_window_state()
        if not self._tray.available:
            self.showMinimized()
            return
        self.hide()
        if not self._tray_hint_shown and self._notifications_enabled():
            self._tray_hint_shown = True
            self._tray.show_message(f"{APP_NAME} is still running",
                                    "Downloads continue in the background. Right-click the tray icon to quit.")

    def request_quit(self) -> bool:
        """Quit the application (asks first when downloads are running). Returns True when quitting."""
        if self._quitting:
            return True
        active = summarize_jobs(self._jobs.values()).in_progress
        if active and not shell_dialogs.confirm_quit_with_downloads(self, active):
            return False
        self._quitting = True
        self.save_window_state()
        quit_application()
        return True

    @property
    def quitting(self) -> bool:
        return self._quitting

    def _on_session_ending(self, *_args: Any) -> None:
        # Windows is logging off/shutting down: never block it with a dialog.
        self._quitting = True
        self.save_window_state()

    def restore_window_state(self, *, reset: bool = False) -> None:
        settings = self._ctx.settings.get()
        restored = False
        if reset:
            self._ctx.settings.update(window_geometry="", window_state="")
        elif settings.window_geometry:
            try:
                restored = self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode("ascii")))
            except (UnicodeEncodeError, TypeError):
                restored = False
            if settings.window_state:
                try:
                    self.restoreState(QByteArray.fromBase64(settings.window_state.encode("ascii")))
                except (UnicodeEncodeError, TypeError):
                    pass
        if not restored:
            self.resize(DEFAULT_SIZE)
            screen = QApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.move(area.center() - self.rect().center())

    def save_window_state(self) -> None:
        if not self._was_shown:
            return  # never shown (started in the tray): keep the saved geometry untouched
        try:
            self._ctx.settings.update(
                window_geometry=bytes(self.saveGeometry().toBase64().data()).decode("ascii"),
                window_state=bytes(self.saveState().toBase64().data()).decode("ascii"),
            )
        except Exception:
            log.warning("Could not save the window geometry", exc_info=True)

    def shutdown(self) -> None:
        """Release everything the window owns (called once by ``app.main`` after the event loop ends)."""
        if self._shut_down:
            return
        self._shut_down = True
        self.save_window_state()
        self._summary_timer.stop()
        self._library_timer.stop()
        self._toasts.clear()
        for page in list(self._pages.values()):
            call_page(page, "shutdown")
        self._surface.set_paused(True)
        self._tray.shutdown()

    # --- Qt events -------------------------------------------------------------------------------
    def showEvent(self, event: QEvent | None) -> None:  # noqa: N802
        self._was_shown = True
        self._surface.set_paused(self.isMinimized())
        super().showEvent(event)

    def hideEvent(self, event: QEvent | None) -> None:  # noqa: N802
        self._surface.set_paused(True)
        super().hideEvent(event)

    def changeEvent(self, event: QEvent | None) -> None:  # noqa: N802
        if event is not None and event.type() == QEvent.Type.WindowStateChange:
            self._surface.set_paused(self.isMinimized() or not self.isVisible())
        super().changeEvent(event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:  # noqa: N802
        if event is not None and event.button() == Qt.MouseButton.BackButton:
            self.back()
            event.accept()
            return
        super().mousePressEvent(event)

    # --- helpers ---------------------------------------------------------------------------------
    def _is_hidden(self) -> bool:
        return not self.isVisible() or self.isMinimized()

    def _notifications_enabled(self) -> bool:
        return bool(self._ctx.settings.get().notifications_enabled)

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any) -> Any:
        """Read-only service query that must never break the shell (services may still be starting)."""
        try:
            return fn()
        except Exception:
            log.debug("Service query failed", exc_info=True)
            return default
