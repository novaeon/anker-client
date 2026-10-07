"""Full-window screenshot viewer.

A frameless, translucent dialog that covers its parent window with the
palette's scrim and shows one image letterboxed ("contain") in the middle.
Previous/next/close overlay buttons; keys: Left/Right, Home/End, Esc closes;
clicking the backdrop (outside the image) closes. The dialog itself keeps the
keyboard focus (the overlay buttons are mouse-only): a focused button would
swallow the arrow keys to move focus instead of changing the image. The
original-size image is requested from the shared ``ImageLoader`` (a
spinner-free "Loading…" caption is drawn until it arrives; "Image unavailable"
when it fails).
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication, QPainter, QPixmap
from PyQt6.QtWidgets import QDialog, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.game_common import OverlayButton, overlay_scrim, overlay_text


class LightboxDialog(QDialog):
    index_changed = pyqtSignal(int)

    SIDE_MARGIN = 84
    TOP_MARGIN = 64
    BOTTOM_MARGIN = 64

    def __init__(self, loader: ImageLoader, urls: list[str], index: int = 0, parent: QWidget | None = None,
                 *, title: str = "") -> None:
        super().__init__(parent)
        self._loader = loader
        self._urls = [u for u in urls if u]
        self._title = title
        self._index = max(0, min(len(self._urls) - 1, index)) if self._urls else -1
        self.setObjectName("lightbox")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("#lightbox { background: transparent; }")
        self.setModal(True)
        self.setWindowTitle(title or "Screenshot")

        self.prev_button = OverlayButton("chevron_left", "Previous (Left arrow)", diameter=48, parent=self)
        self.next_button = OverlayButton("chevron_right", "Next (Right arrow)", diameter=48, parent=self)
        self.close_button = OverlayButton("close", "Close (Esc)", diameter=40, parent=self)
        self.prev_button.clicked.connect(self.previous)
        self.next_button.clicked.connect(self.next)
        self.close_button.clicked.connect(self.reject)
        for overlay in (self.prev_button, self.next_button, self.close_button):
            overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        many = len(self._urls) > 1
        self.prev_button.setVisible(many)
        self.next_button.setVisible(many)
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._fit_to_parent()

    # --- public ------------------------------------------------------------------------
    @property
    def current_index(self) -> int:
        return self._index

    def current_url(self) -> str:
        return self._urls[self._index] if 0 <= self._index < len(self._urls) else ""

    def set_index(self, index: int) -> None:
        if not self._urls:
            return
        index = index % len(self._urls)
        if index != self._index:
            self._index = index
            self.update()
            self.index_changed.emit(index)

    def next(self) -> None:
        self.set_index(self._index + 1)

    def previous(self) -> None:
        self.set_index(self._index - 1)

    def image_rect(self) -> QRect:
        return self.rect().adjusted(self.SIDE_MARGIN, self.TOP_MARGIN, -self.SIDE_MARGIN, -self.BOTTOM_MARGIN)

    # --- internals ---------------------------------------------------------------------
    def _fit_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            self.setGeometry(window.geometry())
            return
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        else:
            self.resize(1280, 800)

    def _pixmap(self) -> QPixmap | None:
        url = self.current_url()
        return self._loader.request(url, QSize()) if url else None

    def _on_loaded(self, url: str, _size_key: str) -> None:
        if url == self.current_url():
            self.update()

    def _on_failed(self, url: str) -> None:
        if url == self.current_url():
            self.update()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        y = (self.height() - self.prev_button.height()) // 2
        self.prev_button.move(20, y)
        self.next_button.move(self.width() - self.next_button.width() - 20, y)
        self.close_button.move(self.width() - self.close_button.width() - 16, 16)

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key == Qt.Key.Key_Right:
            self.next()
        elif key == Qt.Key.Key_Left:
            self.previous()
        elif key == Qt.Key.Key_Home:
            self.set_index(0)
        elif key == Qt.Key.Key_End:
            self.set_index(len(self._urls) - 1)
        else:
            super().keyPressEvent(event)  # Esc → reject
            return
        event.accept()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._drawn_rect().contains(event.position()):
            self.reject()
            return
        super().mousePressEvent(event)

    def _drawn_rect(self) -> QRectF:
        area = QRectF(self.image_rect())
        pixmap = self._pixmap()
        if pixmap is None or pixmap.isNull():
            return area
        size = pixmap.size().toSizeF()
        size.scale(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(area.center().x() - size.width() / 2, area.center().y() - size.height() / 2,
                      size.width(), size.height())

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), overlay_scrim(min_alpha=225))
        pixmap = self._pixmap()
        fg = overlay_text()
        caption_font = QFont(self.font())
        caption_font.setPointSizeF(max(9.0, caption_font.pointSizeF()))
        p.setFont(caption_font)
        if pixmap is not None and not pixmap.isNull():
            p.drawPixmap(self._drawn_rect(), pixmap, QRectF(pixmap.rect()))
        else:
            p.setPen(overlay_text(muted=True))
            failed = self._loader.is_failed(self.current_url())
            p.drawText(self.image_rect(), Qt.AlignmentFlag.AlignCenter,
                       "Image unavailable" if failed else "Loading…")
        caption = f"{self._index + 1} / {len(self._urls)}" if self._urls else ""
        if self._title:
            caption = f"{self._title}  ·  {caption}" if caption else self._title
        p.setPen(fg)
        bottom = QRect(0, self.height() - self.BOTTOM_MARGIN, self.width(), self.BOTTOM_MARGIN)
        p.drawText(bottom, Qt.AlignmentFlag.AlignCenter, caption)
        p.end()
