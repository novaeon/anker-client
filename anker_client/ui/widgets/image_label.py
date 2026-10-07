"""A QLabel-like widget that shows a remote image via the shared ImageLoader."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QSizePolicy, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette


class AsyncImage(QWidget):
    """Displays ``url`` cropped to fill its rect (rounded corners, placeholder initials).

    ``set_fit("cover")`` crops to fill; ``"contain"`` letterboxes. Optionally draws a
    bottom gradient scrim (``set_scrim(True)``) so text can be overlaid.
    """

    def __init__(self, loader: ImageLoader, *, radius: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loader = loader
        self._url = ""
        self._placeholder = ""
        self._pixmap: QPixmap | None = None
        self._radius = radius
        self._fit = "cover"
        self._scrim = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        loader.loaded.connect(self._on_loaded)

    def set_image(self, url: str, placeholder: str = "") -> None:
        self._url = url or ""
        self._placeholder = placeholder
        self._pixmap = None
        self._request()
        self.update()

    def set_fit(self, fit: str) -> None:
        self._fit = fit
        self._request()

    def set_scrim(self, enabled: bool) -> None:
        self._scrim = enabled
        self.update()

    def url(self) -> str:
        return self._url

    def _target_size(self) -> QSize | None:
        if self._fit != "cover" or self.width() < 2 or self.height() < 2:
            return None
        dpr = self.devicePixelRatioF()
        return QSize(int(self.width() * dpr), int(self.height() * dpr))

    def _request(self) -> None:
        if not self._url:
            return
        pixmap = self._loader.request(self._url, self._target_size())
        if pixmap is not None:
            self._pixmap = pixmap
            self.update()

    def _on_loaded(self, url: str, _size_key: str) -> None:
        if url == self._url:
            self._request()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._request()

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        radius = pal.radius if self._radius is None else self._radius
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setClipPath(path)
        p.fillRect(rect, QColor(pal.surface_alt))
        if self._pixmap is not None and not self._pixmap.isNull():
            pm = self._pixmap
            if self._fit == "cover":
                scaled = pm.scaled(self.size() * self.devicePixelRatioF(),
                                   Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            else:
                scaled = pm.scaled(self.size() * self.devicePixelRatioF(), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(self.devicePixelRatioF())
            w = scaled.width() / scaled.devicePixelRatio()
            h = scaled.height() / scaled.devicePixelRatio()
            p.drawPixmap(QRectF((rect.width() - w) / 2, (rect.height() - h) / 2, w, h), scaled,
                         QRectF(scaled.rect()))
        elif self._placeholder:
            p.setPen(QColor(pal.text_faint))
            font = QFont(self.font())
            font.setPointSizeF(max(10.0, min(rect.width(), rect.height()) / 6))
            font.setBold(True)
            p.setFont(font)
            initials = "".join(w[0] for w in self._placeholder.split()[:2] if w).upper() or "?"
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials)
        if self._scrim:
            from PyQt6.QtGui import QLinearGradient

            grad = QLinearGradient(0, rect.height() * 0.35, 0, rect.height())
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(1.0, QColor(0, 0, 0, 200))
            p.fillRect(rect, grad)
        p.end()
