"""Bottom status strip (role=statusbar).

Left: download summary ("2 downloading · 12.3 MB/s · 4m left", "Paused · 1
download", "No downloads in progress"); clicking it opens the Downloads page.
Right: the game currently running ("Playing Hollow Knight") and the app version.

Texts are elided to the strip's width (full text in the tooltip): game titles
can be very long and must never force the window wider.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QResizeEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QWidget

from anker_client import __version__
from anker_client.ui import icons
from anker_client.ui.shell_summary import DownloadSummary
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, label

STATUS_HEIGHT = 30
_BUTTON_CHROME = 44  # icon + spacing + QSS padding inside the summary button
_RUNNING_SHARE = 0.35  # at most this share of the strip for "Playing …"


class _Dot(QWidget):
    """Small filled circle in the palette's success colour ("game running")."""

    def __init__(self, size: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(palette.current().success))
        p.drawEllipse(self.rect())
        p.end()


class StatusStrip(QFrame):
    downloads_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "statusbar")
        self.setFixedHeight(STATUS_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 14, 0)
        layout.setSpacing(12)

        self.downloads_button = button("No downloads in progress", "download", variant="status",
                                       on_click=self.downloads_clicked.emit)
        self.downloads_button.setIconSize(QSize(14, 14))
        self.downloads_button.setToolTip("Open Downloads")
        # Explicit minimums: the layout must not take the (unbounded) text width as the minimum.
        self.downloads_button.setMinimumWidth(80)
        layout.addWidget(self.downloads_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)

        self._running_dot = _Dot(8)
        self.running_label = label("", "caption")
        self.running_label.setMinimumWidth(40)
        layout.addWidget(self._running_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.running_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.version_label = label(f"v{__version__}", "caption")
        self.version_label.setToolTip(f"AnkerClient {__version__}")
        layout.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._summary = DownloadSummary()
        self._headline = self._summary.headline
        self._running: list[str] = []
        self._running_full = ""
        self.set_running([])
        self.refresh_icons()

    def set_summary(self, summary: DownloadSummary) -> None:
        self._summary = summary
        self._headline = summary.headline
        self._apply_texts()
        self._apply_download_icon()

    def summary_text(self) -> str:
        """The full download summary (the button may show it elided)."""
        return self._headline

    def set_running(self, titles: list[str]) -> None:
        self._running = list(titles)
        if not titles:
            text = ""
        elif len(titles) == 1:
            text = f"Playing {titles[0]}"
        else:
            text = f"Playing {titles[0]} + {len(titles) - 1} more"
        self._running_full = text
        self.running_label.setToolTip("\n".join(titles))
        self.running_label.setVisible(bool(text))
        self._running_dot.setVisible(bool(text))
        self._apply_texts()

    def running_text(self) -> str:
        """The full "Playing …" text (the label may show it elided)."""
        return self._running_full

    # --- eliding ----------------------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_texts()

    def _apply_texts(self) -> None:
        margins = self.layout().contentsMargins() if self.layout() is not None else None
        inner = self.width() - (margins.left() + margins.right() if margins is not None else 0)
        right = self.version_label.sizeHint().width() + 12
        running = ""
        if self._running_full:
            running = _elide(self.running_label, self._running_full, max(40, int(inner * _RUNNING_SHARE)))
            right += QFontMetrics(self.running_label.font()).horizontalAdvance(running) + 8 + 12 + 12
        self.running_label.setText(running)
        headline = _elide(self.downloads_button, self._headline, max(60, inner - right - _BUTTON_CHROME))
        self.downloads_button.setText(headline)
        self.downloads_button.setToolTip("Open Downloads" if headline == self._headline
                                         else f"{self._headline}\nOpen Downloads")


    def refresh_icons(self) -> None:
        self._apply_download_icon()
        self._running_dot.update()
        self._apply_texts()  # a theme change can change the font metrics

    def _apply_download_icon(self) -> None:
        pal = palette.current()
        busy = self._summary.active > 0 or self._summary.waiting > 0
        self.downloads_button.setIcon(icons.icon("download", pal.accent if busy else pal.text_muted))


def _elide(widget: QWidget, text: str, width: int) -> str:
    widget.ensurePolished()  # the QSS font (e.g. 9pt for the status button) decides the metrics
    return QFontMetrics(widget.font()).elidedText(text, Qt.TextElideMode.ElideRight, max(0, width))
