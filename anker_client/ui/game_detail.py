# anker_client/ui/game_detail.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea, QHBoxLayout
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from anker_client.core.scraper import parse_game_page
from anker_client.config import BASE_URL


class GamePageWorker(QThread):
    loaded = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, session, slug: str):
        super().__init__()
        self._session = session
        self._slug = slug

    def run(self) -> None:
        try:
            resp = self._session.get(f"{BASE_URL}/game/{self._slug}", timeout=10)
            resp.raise_for_status()
            data = parse_game_page(resp.text)
            data["slug"] = self._slug
            self.loaded.emit(data)
        except Exception as e:
            self.error.emit(str(e))


class ScreenshotLoader(QThread):
    loaded = pyqtSignal(bytes)

    def __init__(self, session, url: str):
        super().__init__()
        self._session = session
        self._url = url
        self.finished.connect(self.deleteLater)

    def run(self) -> None:
        try:
            resp = self._session.get(self._url, timeout=15)
            resp.raise_for_status()
            self.loaded.emit(bytes(resp.content))
        except Exception:
            pass


class ScreenshotGallery(QWidget):
    """Compact single-image gallery with prev/next arrows and a counter."""

    IMG_H = 150

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._urls: list[str] = []
        self._pixmaps: dict[str, QPixmap] = {}
        self._index = 0
        self._workers: list[ScreenshotLoader] = []
        self._build_ui()
        self.hide()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)

        btn_style = "font-size: 15px; border: none; padding: 0 2px; color: #94a3b8;"
        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedWidth(22)
        self._prev_btn.setStyleSheet(btn_style)
        self._prev_btn.clicked.connect(self._prev)
        row.addWidget(self._prev_btn)

        self._img_label = QLabel()
        self._img_label.setFixedHeight(self.IMG_H)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet("background: #1e293b; border-radius: 4px; color: #475569;")
        row.addWidget(self._img_label, stretch=1)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedWidth(22)
        self._next_btn.setStyleSheet(btn_style)
        self._next_btn.clicked.connect(self._next)
        row.addWidget(self._next_btn)

        layout.addLayout(row)

        self._counter = QLabel()
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter.setStyleSheet("color: #64748b; font-size: 10px;")
        layout.addWidget(self._counter)

    def load_screenshots(self, urls: list[str]) -> None:
        self._workers.clear()
        self._urls = urls[:8]
        self._pixmaps = {}
        self._index = 0

        if not self._urls:
            self.hide()
            return

        self.show()
        self._refresh_display()

        for url in self._urls:
            worker = ScreenshotLoader(self.session, url)
            worker.loaded.connect(lambda data, u=url: self._on_img_loaded(u, data))
            worker.start()
            self._workers.append(worker)

    def _on_img_loaded(self, url: str, data: bytes) -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if not pixmap.isNull():
            self._pixmaps[url] = pixmap
            if url == self._urls[self._index]:
                self._show_current()

    def _refresh_display(self) -> None:
        self._show_current()
        n = len(self._urls)
        self._counter.setText(f"{self._index + 1} / {n}")
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < n - 1)

    def _show_current(self) -> None:
        url = self._urls[self._index]
        if url in self._pixmaps:
            w = max(self._img_label.width(), 200)
            self._img_label.setPixmap(self._pixmaps[url].scaled(
                w, self.IMG_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            self._img_label.setText("")
        else:
            self._img_label.clear()
            self._img_label.setText("…")
        self._counter.setText(f"{self._index + 1} / {len(self._urls)}")

    def _prev(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._refresh_display()

    def _next(self) -> None:
        if self._index < len(self._urls) - 1:
            self._index += 1
            self._refresh_display()


class GameDetailPanel(QWidget):
    download_requested = pyqtSignal(dict)  # emits full game_data

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._game_data = None
        self._worker = None
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

        self.screenshot_strip = ScreenshotGallery(self.session)
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
        self.title_label.setText(game.get("title", "Loading..."))
        self.desc_label.setText("Loading details...")
        self.screenshot_strip.load_screenshots([])
        self.download_btn.setEnabled(False)

        self._worker = GamePageWorker(self.session, game["slug"])
        self._worker.loaded.connect(self._on_loaded)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_loaded(self, data: dict) -> None:
        self._game_data = data
        self.title_label.setText(data.get("title", ""))
        lines = [p for p in [", ".join(data.get("genres", [])), data.get("file_size", "")] if p]
        self.meta_label.setText("\n".join(lines))
        self.desc_label.setText(data.get("description", ""))
        self.screenshot_strip.load_screenshots(data.get("screenshots", []))
        self.download_btn.setEnabled(data.get("download_id") is not None)

    def _on_error(self, msg: str) -> None:
        self.desc_label.setText(f"Error loading game details: {msg}")

    def _on_download_clicked(self) -> None:
        if self._game_data:
            self.download_requested.emit(self._game_data)
