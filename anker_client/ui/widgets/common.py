"""Small reusable widgets and helpers shared by every page."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anker_client.ui import icons
from anker_client.ui.theme import palette


def repolish(widget: QWidget) -> None:
    """Re-apply QSS after changing a dynamic property."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setProperty("role", role)
    repolish(widget)
    return widget


def label(text: str = "", role: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def button(
    text: str = "",
    icon_name: str = "",
    *,
    variant: str = "",
    size: str = "",
    tooltip: str = "",
    on_click: Callable[[], None] | None = None,
) -> QPushButton:
    btn = QPushButton(text)
    if icon_name:
        on_accent = variant in {"primary", "success"}
        btn.setIcon(icons.icon(icon_name, palette.current().accent_text if on_accent else None))
        btn.setIconSize(QSize(16, 16) if size != "lg" else QSize(20, 20))
    if variant:
        btn.setProperty("variant", variant)
    if size:
        btn.setProperty("size", size)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        btn.clicked.connect(lambda _checked=False: on_click())
    return btn


def icon_button(icon_name: str, tooltip: str = "", *, on_click: Callable[[], None] | None = None,
                size: int = 18) -> QToolButton:
    btn = QToolButton()
    btn.setProperty("variant", "icon")
    btn.setIcon(icons.icon(icon_name))
    btn.setIconSize(QSize(size, size))
    btn.setToolTip(tooltip)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        btn.clicked.connect(lambda _checked=False: on_click())
    return btn


Margins = tuple[int, int, int, int]


def hbox(*items: QWidget | QLayout | int | None, spacing: int = 8, margins: Margins = (0, 0, 0, 0)) -> QHBoxLayout:
    """Horizontal layout; ints are stretch factors, ``None`` is a stretch of 1."""
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    _fill(layout, items)
    return layout


def vbox(*items: QWidget | QLayout | int | None, spacing: int = 8, margins: Margins = (0, 0, 0, 0)) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    _fill(layout, items)
    return layout


def _fill(layout: QHBoxLayout | QVBoxLayout, items: tuple[QWidget | QLayout | int | None, ...]) -> None:
    for item in items:
        if item is None:
            layout.addStretch(1)
        elif isinstance(item, int):
            layout.addStretch(item)
        elif isinstance(item, QLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)


class Divider(QFrame):
    def __init__(self, vertical: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "divider")
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class Badge(QLabel):
    """Pill label. kind: "" | accent | success | warning | danger."""

    def __init__(self, text: str = "", kind: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.set_kind(kind)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_kind(self, kind: str) -> None:
        self.setProperty("role", f"badge-{kind}" if kind else "badge")
        repolish(self)


class Spinner(QWidget):
    """Indeterminate circular spinner painted with the accent colour."""

    def __init__(self, size: int = 28, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event) -> None:  # noqa: N802
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        from PyQt6.QtGui import QColor

        pen = QPen(QColor(palette.current().accent))
        pen.setWidth(3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        r = self.rect().adjusted(3, 3, -3, -3)
        p.drawArc(r, -self._angle * 16, 270 * 16)
        p.end()


class EmptyState(QWidget):
    """Centered icon + title + message + optional action button."""

    action_clicked = pyqtSignal()

    def __init__(self, icon_name: str, title: str, message: str = "", action_text: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setPixmap(icons.pixmap(icon_name, 48, palette.current().text_faint))
        self._title = label(title, "title")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message = label(message, "muted", wrap=True)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setMaximumWidth(460)
        layout.addWidget(self._icon)
        layout.addWidget(self._title)
        layout.addWidget(self._message, 0, Qt.AlignmentFlag.AlignHCenter)
        self._button = button(action_text, variant="primary", on_click=self.action_clicked.emit)
        self._button.setVisible(bool(action_text))
        layout.addWidget(self._button, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_content(self, icon_name: str, title: str, message: str = "", action_text: str = "") -> None:
        self._icon.setPixmap(icons.pixmap(icon_name, 48, palette.current().text_faint))
        self._title.setText(title)
        self._message.setText(message)
        self._message.setVisible(bool(message))
        self._button.setText(action_text)
        self._button.setVisible(bool(action_text))


class LoadingOverlay(QWidget):
    """Spinner + caption shown while a page loads (place it in a QStackedLayout)."""

    def __init__(self, text: str = "Loading…", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = Spinner(32)
        self.caption = label(text, "muted")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.caption)

    def set_text(self, text: str) -> None:
        self.caption.setText(text)
