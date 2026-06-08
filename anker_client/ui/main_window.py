# anker_client/ui/main_window.py
import os
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QStatusBar, QSplitter,
    QSystemTrayIcon, QMenu, QDialog, QVBoxLayout,
    QLabel, QPushButton, QHBoxLayout, QApplication
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QPainter, QIcon, QMovie
from anker_client.ui.search_widget import SearchWidget
from anker_client.ui.game_detail import GameDetailPanel
from anker_client.ui.library_tab import LibraryTab
from anker_client.ui.settings_tab import SettingsTab
import anker_client.settings as settings


class MainWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setWindowTitle("AnkerClient")
        self.setMinimumSize(1120, 700)
        self._wallpaper_raw: QPixmap | None = None
        self._wallpaper_scaled: QPixmap | None = None
        self._wallpaper_movie: QMovie | None = None
        self._tray: QSystemTrayIcon | None = None
        self._build_ui()
        self._init_tray()
        if not self.session.is_logged_in:
            self._show_login_dialog()

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
        px = self._wallpaper_movie.currentPixmap()
        self._wallpaper_raw = px if not px.isNull() else None
        self._scale_wallpaper()
        self.update()

    def _scale_wallpaper(self) -> None:
        if self._wallpaper_raw:
            self._wallpaper_scaled = self._wallpaper_raw.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
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
        self._scale_wallpaper()

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _init_tray(self) -> None:
        icon_path = os.path.join(os.path.dirname(__file__), "..", "resources", "icon.png")
        if os.path.exists(icon_path):
            icon = QIcon(icon_path)
        else:
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
        if self._tray:
            self._tray.hide()
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
            event.accept()

    # ------------------------------------------------------------------

    def _update_status(self) -> None:
        if self.session.is_logged_in:
            self.status_bar.showMessage(f"Logged in as {self.session.username}")
        else:
            self.status_bar.showMessage("Not logged in")

    def _show_login_dialog(self) -> None:
        from anker_client.ui.login_dialog import LoginDialog
        dlg = LoginDialog(self)
        if dlg.exec() == LoginDialog.DialogCode.Accepted:
            email, password, remember = dlg.get_credentials()
            success = self.session.login(email, password)
            if success:
                if remember:
                    self.session.save_credentials(email, password)
                self._update_status()
            else:
                dlg.show_error("Login failed. Check your email and password.")
                self._show_login_dialog()

    def _on_download_requested(self, game_data: dict) -> None:
        from anker_client.ui.download_dialog import DownloadDialog
        dlg = DownloadDialog(self.session, game_data, self)
        dlg.exec()
        # Refresh library after install completes
        self.library_tab.refresh()
