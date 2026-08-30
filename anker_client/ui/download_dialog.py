from __future__ import annotations

import os
import threading
import uuid

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from anker_client.core.downloader import DownloadTask
from anker_client.core.installer import create_shortcut, find_game_exe, install_game
from anker_client.core.paths import sanitize_windows_name
from anker_client.core.scraper import get_download_url
from anker_client.core.tasks import BackgroundTask, get_task_runner
import anker_client.settings as settings


def _resolve_download_url(
    cancel_event: threading.Event,
    session,
    download_id: int,
    csrf_token: str,
) -> str:
    if cancel_event.is_set():
        return ""
    return get_download_url(session, download_id, csrf_token)


def _install_archive(
    cancel_event: threading.Event,
    archive_path: str,
    game_title: str,
    games_dir: str,
) -> tuple[str | None, str]:
    if cancel_event.is_set():
        return None, ""
    game_dir = install_game(archive_path, game_title, games_dir)
    return find_game_exe(game_dir, game_title), game_dir


def _fmt_size(size: int) -> str:
    if size == 0:
        return "? MB"
    if size < 1024**2:
        return f"{size / 1024:.0f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


def _fmt_speed(bytes_per_second: float) -> str:
    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    if bytes_per_second < 1024**2:
        return f"{bytes_per_second / 1024:.0f} KB/s"
    return f"{bytes_per_second / 1024**2:.1f} MB/s"


class DownloadDialog(QDialog):
    installed = pyqtSignal(str)

    def __init__(self, session, game_data: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.game_data = game_data
        self.installation_succeeded = False
        self.setWindowTitle(f"Installing {game_data.get('title', '')}")
        self.setMinimumWidth(480)
        self.setModal(True)
        self._prepare_task: BackgroundTask | None = None
        self._download_task: DownloadTask | None = None
        self._install_task: BackgroundTask | None = None
        self._archive_path: str | None = None
        self._cancel_requested = False
        self._finished = False
        self._build_ui()
        self._start_download()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.phase_label = QLabel("Preparing download...")
        self.phase_label.setWordWrap(True)
        layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        buttons.addStretch()
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

    def _start_download(self) -> None:
        csrf_token = self.game_data.get("csrf_token")
        download_id = self.game_data.get("download_id")
        if download_id is None or not csrf_token:
            self._on_error("This game page did not provide a valid download link.")
            return

        self.phase_label.setText("Fetching download link...")
        task = get_task_runner().submit(
            _resolve_download_url,
            self.session,
            int(download_id),
            str(csrf_token),
        )
        self._prepare_task = task
        task.signals.result.connect(self._on_download_url)
        task.signals.error.connect(self._on_error)
        task.signals.finished.connect(lambda task=task: self._prepare_finished(task))

    def _prepare_finished(self, task: BackgroundTask) -> None:
        if self._prepare_task is task:
            self._prepare_task = None

    def _on_download_url(self, url: str) -> None:
        if self._cancel_requested or not url:
            return
        title = self.game_data.get("title", "game")
        safe_title = sanitize_windows_name(title)
        filename = f"{safe_title}-{uuid.uuid4().hex}.zip"
        self._archive_path = os.path.join(
            settings.get_games_dir(),
            "_temp",
            "downloads",
            filename,
        )

        self.phase_label.setText("Downloading...")
        self.progress_bar.setRange(0, 100)
        download = DownloadTask(self.session, url, self._archive_path)
        self._download_task = download
        download.signals.progress.connect(self._on_download_progress)
        download.signals.completed.connect(self._on_download_done)
        download.signals.cancelled.connect(self._on_download_cancelled)
        download.signals.error.connect(self._on_error)
        download.signals.finished.connect(
            lambda task=download: self._download_finished(task)
        )
        get_task_runner().start(download)

    def _download_finished(self, task: DownloadTask) -> None:
        if self._download_task is task:
            self._download_task = None
        if self._cancel_requested and not self._install_task and not self._finished:
            if self._archive_path:
                try:
                    os.remove(self._archive_path)
                except OSError:
                    pass
            self._finished = True
            self.reject()

    def _on_download_progress(self, done: int, total: int, speed: float) -> None:
        if self._cancel_requested:
            return
        percent = int(done / total * 100) if total else 0
        if total:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
        else:
            self.progress_bar.setRange(0, 0)

        eta = ""
        if total and speed > 0:
            seconds = max(0, int((total - done) / speed))
            if seconds >= 3600:
                eta = (
                    f"  —  {seconds // 3600}h "
                    f"{(seconds % 3600) // 60}m remaining"
                )
            elif seconds >= 60:
                eta = f"  —  {seconds // 60}m {seconds % 60}s remaining"
            else:
                eta = f"  —  {seconds}s remaining"
        self.detail_label.setText(
            f"{_fmt_size(done)} / {_fmt_size(total)}  —  "
            f"{_fmt_speed(speed)}{eta}"
        )

    def _on_download_done(self, path: str) -> None:
        if self._cancel_requested:
            return
        title = self.game_data.get("title", "game")
        self.phase_label.setText("Extracting and installing...")
        self.detail_label.setText("This step cannot be cancelled safely.")
        self.progress_bar.setRange(0, 0)
        self.cancel_btn.setEnabled(False)

        task = get_task_runner().submit(
            _install_archive,
            path,
            title,
            settings.get_games_dir(),
        )
        self._install_task = task
        task.signals.result.connect(self._on_install_done)
        task.signals.error.connect(self._on_error)
        task.signals.finished.connect(lambda task=task: self._install_finished(task))

    def _install_finished(self, task: BackgroundTask) -> None:
        if self._install_task is task:
            self._install_task = None
        if self._finished:
            self.cancel_btn.setEnabled(True)

    def _on_install_done(self, result: tuple[str | None, str]) -> None:
        executable, game_dir = result
        if not game_dir:
            self._on_error("Installation was cancelled.")
            return

        self.installation_succeeded = True
        if executable:
            self._create_shortcuts(executable, game_dir)
        else:
            executable, _ = QFileDialog.getOpenFileName(
                self,
                "Select game executable",
                game_dir,
                "Executables (*.exe)",
            )
            if executable:
                self._create_shortcuts(executable, game_dir)

        self._finished = True
        self.phase_label.setText("Done!")
        self.detail_label.setText("The game is ready to play.")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Close")
        self.installed.emit(game_dir)
        self._send_notification()

    def _create_shortcuts(self, executable: str, game_dir: str) -> None:
        title = self.game_data.get("title", "Game")
        safe_title = sanitize_windows_name(title)
        desktop = os.path.join(
            os.path.expanduser("~"),
            "Desktop",
            f"{safe_title}.lnk",
        )
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        start_menu = os.path.join(
            appdata,
            "Microsoft",
            "Windows",
            "Start Menu",
            "Programs",
            f"{safe_title}.lnk",
        )
        try:
            create_shortcut(executable, desktop, game_dir)
            create_shortcut(executable, start_menu, game_dir)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Shortcut Failed",
                f"The game installed successfully, but shortcuts could not be "
                f"created:\n\n{exc}",
            )

    def _send_notification(self) -> None:
        from anker_client.ui.main_window import MainWindow

        title = self.game_data.get("title", "Game")
        for window in QApplication.topLevelWidgets():
            if isinstance(window, MainWindow):
                window.notify("Download complete", f"{title} is ready to play")
                return

    def _on_download_cancelled(self) -> None:
        self._finished = True
        self.reject()

    def _on_error(self, message: str) -> None:
        if self._cancel_requested:
            self.reject()
            return
        self._finished = True
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.phase_label.setText("Installation failed")
        self.detail_label.setText(message)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Close")

    def _cancel(self) -> None:
        if self._finished:
            self.accept() if self.installation_succeeded else self.reject()
            return
        if self._install_task:
            return

        self._cancel_requested = True
        if self._prepare_task:
            self._prepare_task.cancel()
            self.reject()
            return
        if self._download_task:
            self.phase_label.setText("Cancelling download...")
            self.detail_label.setText("")
            self.cancel_btn.setEnabled(False)
            self._download_task.cancel()
            return
        self.reject()

    def request_application_close(self) -> bool:
        """Prepare for app shutdown, refusing to interrupt file replacement."""

        if self._install_task:
            QMessageBox.information(
                self,
                "Installation in progress",
                "AnkerClient is finishing the installation. Quit after this "
                "step completes to avoid leaving the game half-installed.",
            )
            return False
        if self._download_task:
            self._cancel()
            return False
        if self._prepare_task:
            self._cancel()
        return True

    def closeEvent(self, event) -> None:
        if self._finished:
            event.accept()
            return
        if self._install_task:
            event.ignore()
            return
        self._cancel()
        if self._download_task:
            event.ignore()
        else:
            event.accept()
