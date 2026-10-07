"""Screenshot carousel: a large 16:9 image with previous/next buttons and a thumbnail strip.

* ``set_images(urls)``; ``current_index``; ``set_current(i)``; ``next()`` /
  ``previous()`` wrap around.
* Keyboard (when focused): Left/Right, Home/End; Enter/Space opens the image.
* Clicking the large image emits ``image_activated(index)`` (the game page
  opens the lightbox); clicking a thumbnail selects it.
* The large image keeps 16:9 (capped at ``MAX_MAIN_HEIGHT``); the strip and
  arrows are hidden when there is only one image.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette
from anker_client.ui.widgets.game_common import OverlayButton, overlay_scrim, overlay_text
from anker_client.ui.widgets.image_label import AsyncImage


class _Thumb(AsyncImage):
    clicked = pyqtSignal()

    def __init__(self, loader: ImageLoader, index: int, size: QSize, parent: QWidget | None = None) -> None:
        super().__init__(loader, parent=parent)
        self.index = index
        self._selected = False
        self.setFixedSize(size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Screenshot {index + 1}")

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    @property
    def selected(self) -> bool:
        return self._selected

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, pal.radius, pal.radius)
        if self._selected:
            p.setPen(QPen(QColor(pal.accent), 2))
        else:
            dim = QColor(pal.bg)
            dim.setAlpha(110)
            p.fillPath(path, dim)
            p.setPen(QPen(QColor(pal.border), 1))
        p.drawPath(path)
        p.end()


class _MainShot(AsyncImage):
    """The large image: click to enlarge; draws a "2 / 5" counter."""

    clicked = pyqtSignal()

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(loader, parent=parent)
        self.counter = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to enlarge")
        self.prev_button = OverlayButton("chevron_left", "Previous screenshot", parent=self)
        self.next_button = OverlayButton("chevron_right", "Next screenshot", parent=self)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        y = (self.height() - self.prev_button.height()) // 2
        self.prev_button.move(12, y)
        self.next_button.move(self.width() - self.next_button.width() - 12, y)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if not self.counter:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(self.counter) + 18
        h = fm.height() + 8
        rect = QRectF(self.width() - w - 12, self.height() - h - 12, w, h)
        pill = QPainterPath()
        pill.addRoundedRect(rect, h / 2, h / 2)
        p.fillPath(pill, overlay_scrim(min_alpha=160))
        p.setPen(overlay_text())
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.counter)
        p.end()


class ScreenshotCarousel(QWidget):
    current_changed = pyqtSignal(int)
    image_activated = pyqtSignal(int)

    THUMB_SIZE = QSize(136, 76)
    MAX_MAIN_HEIGHT = 520

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loader = loader
        self._urls: list[str] = []
        self._index = -1
        self._thumbs: list[_Thumb] = []
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.main = _MainShot(loader, self)
        self.main.clicked.connect(lambda: self.image_activated.emit(self._index) if self._index >= 0 else None)
        self.main.prev_button.clicked.connect(self.previous)
        self.main.next_button.clicked.connect(self.next)

        self._strip_content = QWidget()
        self._strip_content.setProperty("role", "transparent")
        self._strip_layout = QHBoxLayout(self._strip_content)
        self._strip_layout.setContentsMargins(0, 0, 0, 0)
        self._strip_layout.setSpacing(8)
        self._strip_layout.addStretch(1)
        self.strip = QScrollArea()
        self.strip.setWidget(self._strip_content)
        self.strip.setWidgetResizable(True)
        self.strip.setFrameShape(QScrollArea.Shape.NoFrame)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.strip.setFixedHeight(self.THUMB_SIZE.height() + self.strip.horizontalScrollBar().sizeHint().height() + 4)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.main)
        layout.addWidget(self.strip)
        self._sync_heights()

    # --- public ------------------------------------------------------------------------
    @property
    def current_index(self) -> int:
        return self._index

    def urls(self) -> list[str]:
        return list(self._urls)

    def count(self) -> int:
        return len(self._urls)

    def thumbs(self) -> list[_Thumb]:
        return list(self._thumbs)

    def set_images(self, urls: list[str]) -> None:
        urls = [u for u in urls if u]
        if urls == self._urls:
            return
        self._urls = urls
        for thumb in self._thumbs:
            self._strip_layout.removeWidget(thumb)
            thumb.hide()
            thumb.deleteLater()
        self._thumbs = []
        for i, url in enumerate(urls):
            thumb = _Thumb(self._loader, i, self.THUMB_SIZE, self._strip_content)
            thumb.set_image(url)
            thumb.clicked.connect(lambda i=i: self.set_current(i))
            self._strip_layout.insertWidget(i, thumb)
            self._thumbs.append(thumb)
        many = len(urls) > 1
        self.strip.setVisible(many)
        self.main.prev_button.setVisible(many)
        self.main.next_button.setVisible(many)
        self._index = -1
        self._sync_heights()
        self.set_current(0 if urls else -1)

    def set_current(self, index: int) -> None:
        if not self._urls:
            self._index = -1
            self.main.set_image("")
            self.main.counter = ""
            return
        index = max(0, min(len(self._urls) - 1, index))
        if index == self._index:
            return
        self._index = index
        self.main.set_image(self._urls[index])
        self.main.counter = f"{index + 1} / {len(self._urls)}" if len(self._urls) > 1 else ""
        for thumb in self._thumbs:
            thumb.set_selected(thumb.index == index)
        if 0 <= index < len(self._thumbs):
            self.strip.ensureWidgetVisible(self._thumbs[index], 24, 0)
        self.main.update()
        self.current_changed.emit(index)

    def next(self) -> None:
        if self._urls:
            self.set_current((self._index + 1) % len(self._urls))

    def previous(self) -> None:
        if self._urls:
            self.set_current((self._index - 1) % len(self._urls))

    # --- events --------------------------------------------------------------------------
    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key == Qt.Key.Key_Right:
            self.next()
        elif key == Qt.Key.Key_Left:
            self.previous()
        elif key == Qt.Key.Key_Home:
            self.set_current(0)
        elif key == Qt.Key.Key_End:
            self.set_current(len(self._urls) - 1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self._index >= 0:
            self.image_activated.emit(self._index)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._sync_heights()

    def _sync_heights(self) -> None:
        main_h = min(self.MAX_MAIN_HEIGHT, max(120, round(self.width() * 9 / 16)))
        if self.main.height() != main_h:
            self.main.setFixedHeight(main_h)
        total = main_h + (self.layout().spacing() + self.strip.height() if self.strip.isVisibleTo(self) else 0)
        if self.height() != total:
            self.setFixedHeight(total)
