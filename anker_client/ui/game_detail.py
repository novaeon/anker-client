from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from anker_client.config import BASE_URL
from anker_client.core.scraper import parse_game_page
from anker_client.core.tasks import BackgroundTask, get_task_runner
from anker_client.ui.image_loader import get_image_loader


def fetch_game_page(
    cancel_event: threading.Event,
    session,
    slug: str,
) -> dict:
    """Fetch and parse one detail page outside the GUI thread."""

    if cancel_event.is_set():
        return {}
    response = session.get(f"{BASE_URL}/game/{slug}", timeout=(5, 10))
    response.raise_for_status()
    if cancel_event.is_set():
        return {}
    data = parse_game_page(response.text)
    data["slug"] = slug
    return data


class ScreenshotGallery(QWidget):
    """Lazy single-image gallery backed by the application image cache."""

    IMG_H = 150

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self._urls: list[str] = []
        self._index = 0
        self._loader = get_image_loader()
        self._loader.image_loaded.connect(self._on_image_loaded)
        self._loader.image_failed.connect(self._on_image_failed)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._show_current)
        self._build_ui()
        self.hide()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)
        button_style = (
            "font-size: 15px; border: none; padding: 0 2px; color: #94a3b8;"
        )

        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedWidth(22)
        self._prev_btn.setStyleSheet(button_style)
        self._prev_btn.clicked.connect(self._prev)
        row.addWidget(self._prev_btn)

        self._img_label = QLabel()
        self._img_label.setFixedHeight(self.IMG_H)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet(
            "background: #1e293b; border-radius: 4px; color: #475569;"
        )
        row.addWidget(self._img_label, stretch=1)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedWidth(22)
        self._next_btn.setStyleSheet(button_style)
        self._next_btn.clicked.connect(self._next)
        row.addWidget(self._next_btn)
        layout.addLayout(row)

        self._counter = QLabel()
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter.setStyleSheet("color: #64748b; font-size: 10px;")
        layout.addWidget(self._counter)

    def load_screenshots(self, urls: list[str]) -> None:
        # De-duplicate while preserving server order.
        self._urls = list(dict.fromkeys(url for url in urls if url))[:8]
        self._index = 0
        if not self._urls:
            self._img_label.clear()
            self.hide()
            return
        self.show()
        self._refresh_display()

    def _refresh_display(self) -> None:
        if not self._urls:
            return
        self._show_current()
        count = len(self._urls)
        self._counter.setText(f"{self._index + 1} / {count}")
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < count - 1)

    def _show_current(self) -> None:
        if not self._urls:
            return
        url = self._urls[self._index]
        pixmap = self._loader.request(url)
        if pixmap is None:
            self._img_label.clear()
            self._img_label.setText("Loading…")
        else:
            self._set_pixmap(pixmap)

        # Warm only the next image; loading all eight at once was a major burst
        # of network, decode and allocation work in the old gallery.
        if self._index + 1 < len(self._urls):
            self._loader.request(self._urls[self._index + 1])

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        width = max(self._img_label.width(), 200)
        self._img_label.setPixmap(
            pixmap.scaled(
                width,
                self.IMG_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._img_label.setText("")

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        if self._urls and url == self._urls[self._index]:
            self._set_pixmap(pixmap)

    def _on_image_failed(self, url: str) -> None:
        if self._urls and url == self._urls[self._index]:
            self._img_label.clear()
            self._img_label.setText("Image unavailable")

    def _prev(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._refresh_display()

    def _next(self) -> None:
        if self._index < len(self._urls) - 1:
            self._index += 1
            self._refresh_display()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._urls:
            self._resize_timer.start()


class GameDetailPanel(QWidget):
    download_requested = pyqtSignal(dict)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._game_data: dict | None = None
        self._task: BackgroundTask | None = None
        self._generation = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color: #94a3b8;")
        layout.addWidget(self.meta_label)

        self.screenshot_strip = ScreenshotGallery()
        layout.addWidget(self.screenshot_strip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.desc_label = QLabel("")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.desc_label.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(self.desc_label)
        layout.addWidget(scroll)

        self.download_btn = QPushButton("Download")
        self.download_btn.setMinimumHeight(40)
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._on_download_clicked)
        layout.addWidget(self.download_btn)

    def load_game(self, game: dict) -> None:
        slug = game.get("slug")
        if not slug:
            return

        self._generation += 1
        generation = self._generation
        if self._task:
            self._task.cancel()

        self._game_data = None
        self.title_label.setText(game.get("title", "Loading…"))
        metadata = [
            ", ".join(game.get("genres", [])),
            f"{game.get('size_gb')} GB" if game.get("size_gb") else "",
        ]
        self.meta_label.setText("\n".join(part for part in metadata if part))
        self.desc_label.setText(game.get("overview") or "Loading details…")
        self.screenshot_strip.load_screenshots([])
        self.download_btn.setEnabled(False)

        task = get_task_runner().submit(fetch_game_page, self.session, slug)
        self._task = task
        task.signals.result.connect(
            lambda data, generation=generation: self._on_loaded(generation, data)
        )
        task.signals.error.connect(
            lambda message, generation=generation: self._on_error(
                generation, message
            )
        )
        task.signals.finished.connect(lambda task=task: self._task_finished(task))

    def _on_loaded(self, generation: int, data: dict) -> None:
        if generation != self._generation or not data:
            return
        self._game_data = data
        self.title_label.setText(data.get("title", ""))
        lines = [
            part
            for part in [
                ", ".join(data.get("genres", [])),
                data.get("file_size", ""),
            ]
            if part
        ]
        self.meta_label.setText("\n".join(lines))
        self.desc_label.setText(data.get("description", ""))
        self.screenshot_strip.load_screenshots(data.get("screenshots", []))
        self.download_btn.setEnabled(data.get("download_id") is not None)

    def _on_error(self, generation: int, message: str) -> None:
        if generation == self._generation:
            self.desc_label.setText(f"Error loading game details: {message}")

    def _task_finished(self, task: BackgroundTask) -> None:
        if self._task is task:
            self._task = None

    def _on_download_clicked(self) -> None:
        if self._game_data:
            self.download_requested.emit(dict(self._game_data))

    def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
