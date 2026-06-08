# anker_client/ui/download_dialog.py
import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QHBoxLayout,
    QApplication
)
from PyQt6.QtCore import QThread, pyqtSignal
from anker_client.core.scraper import get_download_url
from anker_client.core.downloader import DownloadThread
from anker_client.core.installer import (
    install_game, find_game_exe, create_shortcut
)
from anker_client.core.paths import sanitize_windows_name
import anker_client.settings as settings


class InstallWorker(QThread):
    status = pyqtSignal(str)
    exe_found = pyqtSignal(str, str)   # exe_path, game_dir
    exe_needed = pyqtSignal(str)       # game_dir — need user to pick
    error = pyqtSignal(str)

    def __init__(self, archive_path: str, game_title: str):
        super().__init__()
        self._archive = archive_path
        self._title = game_title

    def run(self) -> None:
        try:
            self.status.emit("Extracting...")
            game_dir = install_game(self._archive, self._title, settings.get_games_dir())

            self.status.emit("Finding game executable...")
            exe = find_game_exe(game_dir, self._title)
            if exe:
                self.exe_found.emit(exe, game_dir)
            else:
                self.exe_needed.emit(game_dir)
        except Exception as e:
            self.error.emit(str(e))


def _fmt_size(n: int) -> str:
    if n == 0:
        return "? MB"
    if n < 1024 ** 2:
        return f"{n/1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


def _fmt_speed(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps/1024:.0f} KB/s"
    return f"{bps/1024**2:.1f} MB/s"


class DownloadDialog(QDialog):
    def __init__(self, session, game_data: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.game_data = game_data
        self.setWindowTitle(f"Installing {game_data.get('title', '')}")
        self.setMinimumWidth(480)
        self.setModal(True)
        self._download_thread = None
        self._install_thread = None
        self._archive_path = None
        self._build_ui()
        self._start_download()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.phase_label = QLabel("Preparing download...")
        layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(self.detail_label)

        btns = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btns.addStretch()
        btns.addWidget(self.cancel_btn)
        layout.addLayout(btns)

    def _start_download(self) -> None:
        title = self.game_data.get("title", "game")
        csrf = self.game_data.get("csrf_token")
        dl_id = self.game_data.get("download_id")

        self.phase_label.setText("Fetching download link...")
        try:
            url = get_download_url(self.session, dl_id, csrf)
        except Exception as e:
            self.phase_label.setText(f"Error: {e}")
            self.cancel_btn.setText("Close")
            return

        safe_title = sanitize_windows_name(title)
        self._archive_path = os.path.join(settings.get_games_dir(), "_temp", f"{safe_title}.zip")
        os.makedirs(os.path.dirname(self._archive_path), exist_ok=True)

        self.phase_label.setText("Downloading...")
        self._download_thread = DownloadThread(self.session, url, self._archive_path)
        self._download_thread.progress.connect(self._on_download_progress)
        self._download_thread.finished.connect(self._on_download_done)
        self._download_thread.error.connect(self._on_error)
        self._download_thread.start()

    def _on_download_progress(self, done: int, total: int, speed: float) -> None:
        pct = int(done / total * 100) if total else 0
        self.progress_bar.setValue(pct)
        eta = ""
        if total and speed > 0:
            secs = int((total - done) / speed)
            if secs >= 3600:
                eta = f"  —  {secs // 3600}h {(secs % 3600) // 60}m remaining"
            elif secs >= 60:
                eta = f"  —  {secs // 60}m {secs % 60}s remaining"
            else:
                eta = f"  —  {secs}s remaining"
        self.detail_label.setText(
            f"{_fmt_size(done)} / {_fmt_size(total)}  —  {_fmt_speed(speed)}{eta}"
        )

    def _on_download_done(self, path: str) -> None:
        title = self.game_data.get("title", "game")
        self.phase_label.setText("Extracting...")
        self.progress_bar.setRange(0, 0)  # indeterminate

        self._install_thread = InstallWorker(path, title)
        self._install_thread.status.connect(self.phase_label.setText)
        self._install_thread.exe_found.connect(self._on_exe_found)
        self._install_thread.exe_needed.connect(self._on_exe_needed)
        self._install_thread.error.connect(self._on_error)
        self._install_thread.start()

    def _on_exe_found(self, exe_path: str, game_dir: str) -> None:
        self._create_shortcuts(exe_path, game_dir)
        self.phase_label.setText("Done!")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_btn.setText("Close")
        self._send_notification()

    def _on_exe_needed(self, game_dir: str) -> None:
        from PyQt6.QtWidgets import QFileDialog
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        exe_path, _ = QFileDialog.getOpenFileName(
            self, "Select game executable", game_dir, "Executables (*.exe)"
        )
        if exe_path:
            self._create_shortcuts(exe_path, game_dir)
            self._send_notification()
        self.phase_label.setText("Done!")
        self.cancel_btn.setText("Close")

    def _create_shortcuts(self, exe_path: str, game_dir: str) -> None:
        title = self.game_data.get("title", "Game")
        safe = sanitize_windows_name(title)

        desktop = os.path.join(os.path.expanduser("~"), "Desktop", f"{safe}.lnk")
        start_menu = os.path.join(
            os.environ["APPDATA"],
            "Microsoft", "Windows", "Start Menu", "Programs", f"{safe}.lnk"
        )
        create_shortcut(exe_path, desktop, game_dir)
        create_shortcut(exe_path, start_menu, game_dir)
        self.phase_label.setText("Shortcuts created on Desktop and Start Menu.")

    def _send_notification(self) -> None:
        """Fire a tray notification via MainWindow if one exists."""
        from anker_client.ui.main_window import MainWindow  # local import avoids circular dep
        title = self.game_data.get("title", "Game")
        for w in QApplication.topLevelWidgets():
            if isinstance(w, MainWindow):
                w.notify("Download complete", f"{title} is ready to play")
                return

    def _on_error(self, msg: str) -> None:
        self.progress_bar.setRange(0, 100)
        self.phase_label.setText(f"Error: {msg}")
        self.cancel_btn.setText("Close")

    def _cancel(self) -> None:
        if self._download_thread and self._download_thread.isRunning():
            self._download_thread.cancel()
        self.reject()
