# anker_client/ui/main_window.py
import os
import threading
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QStatusBar, QSplitter,
    QSystemTrayIcon, QMenu, QDialog, QVBoxLayout,
    QLabel, QPushButton, QHBoxLayout, QApplication
)
from PyQt6.QtCore import Qt, QElapsedTimer, QTimer
from PyQt6.QtGui import QPixmap, QPainter, QMovie
from anker_client.assets import get_app_icon
from anker_client.ui.search_widget import SearchWidget
from anker_client.ui.game_detail import GameDetailPanel
from anker_client.ui.library_tab import LibraryTab
from anker_client.ui.settings_tab import SettingsTab
from anker_client.core.tasks import BackgroundTask, get_task_runner
import anker_client.settings as settings


def _auto_login(cancel_event: threading.Event, session) -> dict:
    """Load remembered credentials and authenticate without blocking startup."""

    email, password = session.load_credentials()
    if cancel_event.is_set() or not (email and password):
        return {"attempted": False, "success": False}
    success = session.login(email, password)
    return {"attempted": True, "success": success}


class MainWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setWindowTitle("AnkerClient")
        self.setWindowIcon(get_app_icon())
        self.setMinimumSize(1120, 700)
        self._wallpaper_raw: QPixmap | None = None
        self._wallpaper_scaled: QPixmap | None = None
        self._wallpaper_movie: QMovie | None = None
        self._tray: QSystemTrayIcon | None = None
        self._auth_task: BackgroundTask | None = None
        self._active_download_dialog = None
        self._wallpaper_frame_clock = QElapsedTimer()
        self._wallpaper_frame_clock.start()
        self._wallpaper_resize_timer = QTimer(self)
        self._wallpaper_resize_timer.setSingleShot(True)
        self._wallpaper_resize_timer.setInterval(120)
        self._wallpaper_resize_timer.timeout.connect(self._scale_wallpaper)
        self._shutting_down = False
        self._build_ui()
        self._init_tray()
        QTimer.singleShot(0, self._begin_authentication)

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Search tab: results on left, detail panel on right
        search_container = QSplitter(Qt.Orientation.Horizontal)
        self.search_widget = SearchWidget(self.session)
        self.detail_panel = GameDetailPanel(self.session)
        self.detail_panel.setMinimumWidth(280)
        self.detail_panel.download_requested.connect(self._on_download_requested)
        search_container.addWidget(self.search_widget)
        search_container.addWidget(self.detail_panel)
        search_container.setSizes([760, 360])
        self.search_widget.game_selected.connect(self.detail_panel.load_game)
        self.tabs.addTab(search_container, "Search")

        # Library tab
        self.library_tab = LibraryTab(self.session)
        self.tabs.addTab(self.library_tab, "Library")

        # Settings tab
        self.settings_tab = SettingsTab(on_saved=self.library_tab.refresh)
        self.settings_tab.theme_changed.connect(self._apply_wallpaper)
        self.tabs.addTab(self.settings_tab, "Settings")

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._update_status()

        # Apply wallpaper for the startup theme
        from anker_client.settings import get_theme
        self._apply_wallpaper(get_theme())

    # ------------------------------------------------------------------
    # Wallpaper support
    # ------------------------------------------------------------------

    def _apply_wallpaper(self, theme_key: str) -> None:
        from anker_client.themes import get_wallpaper_path
        path = get_wallpaper_path(theme_key)
        if self._wallpaper_movie:
            self._wallpaper_movie.stop()
            self._wallpaper_movie = None

        if path:
            if os.path.splitext(path)[1].lower() == ".gif":
                movie = QMovie(path)
                if movie.isValid():
                    self._wallpaper_movie = movie
                    movie.setCacheMode(QMovie.CacheMode.CacheNone)
                    movie.frameChanged.connect(self._on_wallpaper_frame_changed)
                    movie.start()
                    self._wallpaper_raw = movie.currentPixmap()
                else:
                    self._wallpaper_raw = None
            else:
                px = QPixmap(path)
                self._wallpaper_raw = px if not px.isNull() else None
        else:
            self._wallpaper_raw = None
        self._scale_wallpaper()
        self.update()

    def _on_wallpaper_frame_changed(self) -> None:
        if not self._wallpaper_movie:
            return
        # Repainting a smoothly scaled full-window pixmap at the GIF's native
        # frame rate monopolized the GUI thread.  Fifteen frames per second is
        # visually fluid for a background and leaves headroom for interaction.
        if self._wallpaper_frame_clock.elapsed() < 66:
            return
        self._wallpaper_frame_clock.restart()
        px = self._wallpaper_movie.currentPixmap()
        self._wallpaper_raw = px if not px.isNull() else None
        self._scale_wallpaper(animated=True)
        self.update()

    def _scale_wallpaper(self, animated: bool = False) -> None:
        if self._wallpaper_raw:
            self._wallpaper_scaled = self._wallpaper_raw.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                (
                    Qt.TransformationMode.FastTransformation
                    if animated or self._wallpaper_movie
                    else Qt.TransformationMode.SmoothTransformation
                ),
            )
        else:
            self._wallpaper_scaled = None

    def paintEvent(self, event) -> None:
        if self._wallpaper_scaled:
            painter = QPainter(self)
            x = (self.width() - self._wallpaper_scaled.width()) // 2
            y = (self.height() - self._wallpaper_scaled.height()) // 2
            painter.drawPixmap(x, y, self._wallpaper_scaled)
        else:
            super().paintEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Coalesce the stream of native resize events into one rescale.
        self._wallpaper_resize_timer.start()

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _init_tray(self) -> None:
        icon = get_app_icon()
        if icon.isNull():
            icon = self.style().standardIcon(
                self.style().StandardPixmap.SP_ComputerIcon
            )

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("AnkerClient")

        tray_menu = QMenu()
        show_act = tray_menu.addAction("Show")
        show_act.triggered.connect(self._restore_window)
        tray_menu.addSeparator()
        quit_act = tray_menu.addAction("Quit")
        quit_act.triggered.connect(self._quit_app)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_window()

    def _restore_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        if (
            self._active_download_dialog
            and not self._active_download_dialog.request_application_close()
        ):
            return
        if self._tray:
            self._tray.hide()
        self._shutdown_background()
        QApplication.instance().quit()

    def notify(self, title: str, msg: str) -> None:
        if self._tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(title, msg,
                                   QSystemTrayIcon.MessageIcon.Information, 4000)

    # ------------------------------------------------------------------
    # Close event
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        behavior = settings.get_close_behavior()

        if behavior is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Close AnkerClient")
            dlg.setMinimumWidth(360)
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(
                "What should happen when you close AnkerClient?\n\n"
                "You can change this later in Settings."
            ))
            btns = QHBoxLayout()
            tray_btn = QPushButton("Minimize to tray")
            quit_btn = QPushButton("Quit")
            tray_btn.clicked.connect(
                lambda: (settings.save_close_behavior("tray"), dlg.accept())
            )
            quit_btn.clicked.connect(
                lambda: (settings.save_close_behavior("quit"), dlg.reject())
            )
            btns.addWidget(tray_btn)
            btns.addWidget(quit_btn)
            layout.addLayout(btns)
            dlg.exec()
            behavior = settings.get_close_behavior()

        if behavior == "tray":
            event.ignore()
            self.hide()
        else:
            if self._tray:
                self._tray.hide()
            self._shutdown_background()
            event.accept()

    # ------------------------------------------------------------------

    def _update_status(self) -> None:
        if self.session.is_logged_in:
            self.status_bar.showMessage(f"Logged in as {self.session.username}")
        else:
            self.status_bar.showMessage("Not logged in")

    def _begin_authentication(self) -> None:
        if self._shutting_down or self.session.is_logged_in:
            return
        self.status_bar.showMessage("Checking remembered sign-in...")
        task = get_task_runner().submit(_auto_login, self.session)
        self._auth_task = task
        task.signals.result.connect(self._on_auto_login_result)
        task.signals.error.connect(self._on_auto_login_error)
        task.signals.finished.connect(lambda task=task: self._auth_finished(task))

    def _on_auto_login_result(self, result: dict) -> None:
        if self._shutting_down:
            return
        if result.get("success"):
            self._on_authenticated()
            return
        self._update_status()
        QTimer.singleShot(0, self._show_login_dialog)

    def _on_auto_login_error(self, _message: str) -> None:
        if not self._shutting_down:
            self._update_status()
            QTimer.singleShot(0, self._show_login_dialog)

    def _auth_finished(self, task: BackgroundTask) -> None:
        if self._auth_task is task:
            self._auth_task = None

    def _on_authenticated(self) -> None:
        self._update_status()
        # The initial library scan occurs before auto-login finishes, so enrich
        # uncached local games now that authenticated requests are available.
        self.library_tab.refresh()

    def _show_login_dialog(self) -> None:
        from anker_client.ui.login_dialog import LoginDialog
        if self._shutting_down or self.session.is_logged_in:
            return
        dialog = LoginDialog(self.session, self)
        if dialog.exec() == LoginDialog.DialogCode.Accepted:
            self._on_authenticated()

    def _on_download_requested(self, game_data: dict) -> None:
        from anker_client.ui.download_dialog import DownloadDialog
        dialog = DownloadDialog(self.session, game_data, self)
        self._active_download_dialog = dialog
        dialog.exec()
        self._active_download_dialog = None
        if dialog.installation_succeeded:
            self.library_tab.refresh()

    def _shutdown_background(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._auth_task:
            self._auth_task.cancel()
        self.search_widget.shutdown()
        self.detail_panel.shutdown()
        self.library_tab.shutdown()
        get_task_runner().cancel_all()
