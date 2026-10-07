"""Building blocks for the Settings page and the first-run wizard.

* :class:`ToggleSwitch` — an animated on/off switch (checkable ``QAbstractButton``).
* :class:`SettingRow` — title + description on the left, controls on the right.
* :class:`SettingsGroup` — a card that stacks rows with dividers between them.
* :class:`StatusLine` — icon (or spinner) + message, coloured by kind.
* :class:`IconBinder` — re-tints icons when the theme changes (icons are
  rendered in a fixed colour when created).
* :func:`background` adapts a plain callable for :func:`ui.async_.run_async`
  (which always passes a ``token`` keyword); :func:`open_local_path` /
  :func:`open_url` open things with the shell; :func:`confirm` asks before
  destructive actions.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from PyQt6 import sip
from PyQt6.QtCore import (
    QEasingCurve,
    QRectF,
    QSize,
    Qt,
    QUrl,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor, QDesktopServices, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.tasks import CancelToken
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Divider, Spinner, label, repolish

log = logging.getLogger(__name__)
T = TypeVar("T")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def background(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Callable[..., T]:
    """Wrap ``fn(*args, **kwargs)`` so ``run_async`` can call it with a ``token`` keyword."""

    def run(*, token: CancelToken) -> T:
        token.raise_if_cancelled()
        return fn(*args, **kwargs)

    run.__qualname__ = getattr(fn, "__qualname__", "call")
    return run


def open_url(url: str) -> bool:
    """Open ``url`` in the user's default browser."""
    ok = QDesktopServices.openUrl(QUrl(url))
    if not ok:
        log.warning("Could not open %s", url)
    return bool(ok)


def open_local_path(path: str | os.PathLike[str]) -> bool:
    """Open a local folder/file in Explorer (the folder is created first when missing)."""
    target = Path(path)
    try:
        if not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.debug("Could not create %s before opening it", target, exc_info=True)
    ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
    if not ok:
        log.warning("Could not open %s", target)
    return bool(ok)


def confirm(
    parent: QWidget | None,
    *,
    title: str,
    text: str,
    informative: str = "",
    confirm_text: str = "OK",
    destructive: bool = True,
) -> bool:
    """Modal confirmation box; True only when the confirm button was pressed."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    role = QMessageBox.ButtonRole.DestructiveRole if destructive else QMessageBox.ButtonRole.AcceptRole
    ok_button = box.addButton(confirm_text, role)
    ok_button.setProperty("variant", "danger" if destructive else "primary")
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    box.exec()
    return box.clickedButton() is ok_button


def alive(obj: Any) -> bool:
    """False once the C++ side of a Qt object has been deleted."""
    return obj is not None and not sip.isdeleted(obj)


# ---------------------------------------------------------------------------
# icon re-tinting
# ---------------------------------------------------------------------------

#: Colour roles understood by :class:`IconBinder` (attribute names of ``Palette``).
ColorRole = str


class IconBinder:
    """Remembers which icon each widget shows so it can be re-rendered after a theme switch."""

    def __init__(self) -> None:
        self._entries: list[tuple[QWidget, str, ColorRole, int]] = []

    def bind(self, widget: QWidget, name: str, role: ColorRole = "text", size: int = 16) -> QWidget:
        self._entries = [e for e in self._entries if alive(e[0]) and e[0] is not widget]
        self._entries.append((widget, name, role, size))
        self._apply(widget, name, role, size)
        return widget

    def retint(self) -> None:
        live = []
        for entry in self._entries:
            if alive(entry[0]):
                self._apply(*entry)
                live.append(entry)
        self._entries = live

    @staticmethod
    def _apply(widget: QWidget, name: str, role: ColorRole, size: int) -> None:
        color = getattr(palette.current(), role, palette.current().text)
        if isinstance(widget, QAbstractButton):
            widget.setIcon(icons.icon(name, color))
            widget.setIconSize(QSize(size, size))
        elif isinstance(widget, QLabel):
            widget.setPixmap(icons.pixmap(name, size, color))


# ---------------------------------------------------------------------------
# toggle switch
# ---------------------------------------------------------------------------


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


class ToggleSwitch(QAbstractButton):
    """Animated on/off switch painted from the current palette."""

    TRACK_W = 40
    TRACK_H = 22

    def __init__(self, parent: QWidget | None = None, *, checked: bool = False, accessible_name: str = "") -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if accessible_name:
            self.setAccessibleName(accessible_name)
        self._hover = False
        self._keyboard_focus = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda _v: self.update())
        self.clicked.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.TRACK_W + 4, self.TRACK_H + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(0.0 if checked else 1.0)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _position(self) -> float:
        if self._anim.state() == QVariantAnimation.State.Running:
            value = self._anim.currentValue()
            return float(value) if value is not None else (1.0 if self.isChecked() else 0.0)
        return 1.0 if self.isChecked() else 0.0

    def focusInEvent(self, event: Any) -> None:  # noqa: N802
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802
        self._keyboard_focus = False
        super().focusOutEvent(event)

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent | None) -> None:  # noqa: N802
        p = palette.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        pos = self._position()
        track = QRectF(2, (self.height() - self.TRACK_H) / 2, self.TRACK_W, self.TRACK_H)
        off_color = _mix(QColor(p.border), QColor(p.text_faint), 0.55 if self._hover else 0.35)
        on_color = QColor(p.accent_hover if self._hover else p.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(off_color, on_color, pos))
        radius = self.TRACK_H / 2
        painter.drawRoundedRect(track, radius, radius)
        if self.hasFocus() and self._keyboard_focus:
            ring = QColor(p.accent)
            ring.setAlpha(110)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ring, 2))
            painter.drawRoundedRect(track.adjusted(-1.5, -1.5, 1.5, 1.5), radius + 1.5, radius + 1.5)
            painter.setPen(Qt.PenStyle.NoPen)
        knob_d = self.TRACK_H - 6
        x = track.left() + 3 + pos * (self.TRACK_W - knob_d - 6)
        knob_off = QColor(p.text_muted)
        knob_on = QColor(p.accent_text)
        painter.setBrush(_mix(knob_off, knob_on, pos))
        painter.drawEllipse(QRectF(x, track.top() + 3, knob_d, knob_d))
        painter.end()


# ---------------------------------------------------------------------------
# rows / groups
# ---------------------------------------------------------------------------


class SettingRow(QWidget):
    """One setting: title + optional description on the left, controls on the right.

    Clicking the text of a row that holds a :class:`ToggleSwitch` toggles it.
    """

    def __init__(
        self,
        title: str,
        description: str = "",
        *controls: QWidget,
        parent: QWidget | None = None,
        stacked: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        self._toggle: ToggleSwitch | None = None
        self.title_label = label(title)
        self.title_label.setWordWrap(True)
        self.description_label = label(description, "caption", wrap=True)
        self.description_label.setVisible(bool(description))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.setContentsMargins(0, 0, 0, 0)
        text.addWidget(self.title_label)
        text.addWidget(self.description_label)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(8)
        self.controls.setContentsMargins(0, 0, 0, 0)
        # ``stacked`` puts wide controls (lists, path pickers) under the text.
        outer: QHBoxLayout | QVBoxLayout = QVBoxLayout(self) if stacked else QHBoxLayout(self)
        outer.setContentsMargins(18, 12, 18, 12)
        outer.setSpacing(10 if stacked else 16)
        outer.addLayout(text, 1)
        outer.addLayout(self.controls, 0)
        for control in controls:
            self.add_control(control)

    def add_control(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.controls.addWidget(widget, stretch, Qt.AlignmentFlag.AlignVCenter)
        if isinstance(widget, ToggleSwitch) and self._toggle is None:
            self._toggle = widget
            if not widget.accessibleName():
                widget.setAccessibleName(self.title_label.text())
        return widget

    def set_description(self, text: str) -> None:
        self.description_label.setText(text)
        self.description_label.setVisible(bool(text))

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:  # noqa: N802
        toggle = self._toggle
        if (
            event is not None
            and toggle is not None
            and toggle.isEnabled()
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            toggle.click()
            return
        super().mouseReleaseEvent(event)


class SettingsGroup(QFrame):
    """A card holding :class:`SettingRow` widgets separated by thin dividers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(0)
        self._count = 0

    def add_row(self, row: QWidget, *, divider: bool = True) -> QWidget:
        """Append ``row``; ``divider=False`` attaches it to the previous row (e.g. a details line)."""
        if self._count and divider:
            divider = Divider()
            divider.setContentsMargins(0, 0, 0, 0)
            holder = QWidget()
            holder.setProperty("role", "transparent")
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(18, 0, 18, 0)
            hl.addWidget(divider)
            self._layout.addWidget(holder)
        self._layout.addWidget(row)
        self._count += 1
        return row


def group_heading(text: str) -> QLabel:
    heading = label(text, "heading")
    heading.setContentsMargins(2, 6, 0, 0)
    return heading


class SectionHeader(QWidget):
    def __init__(self, title: str, description: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addWidget(label(title, "title"))
        if description:
            layout.addWidget(label(description, "muted", wrap=True))


# ---------------------------------------------------------------------------
# status line
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, tuple[str, str, str]] = {
    # kind: (icon, palette colour attribute, label role)
    "success": ("check_circle", "success", "success"),
    "warning": ("warning", "warning", "warning"),
    "error": ("error", "danger", "error"),
    "info": ("info", "text_muted", "muted"),
    "muted": ("info", "text_faint", "muted"),
    "plain": ("", "text_muted", "muted"),  # text only
}


class StatusLine(QWidget):
    """Small icon + text line. ``kind``: success | warning | error | info | muted | busy."""

    def __init__(self, text: str = "", kind: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(16, 16)
        self._spinner = Spinner(16)
        self._spinner.hide()
        self._text = label("", "muted", wrap=True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Top, like the icon: when a layout stretches the line, both stay on the first text line.
        self._text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._text, 1)
        self._kind = kind
        self.set_status(text, kind)

    @property
    def kind(self) -> str:
        return self._kind

    def text(self) -> str:
        return self._text.text()

    def set_status(self, text: str, kind: str = "info") -> None:
        self._kind = kind
        self._text.setText(text)
        busy = kind == "busy"
        icon_name, color_attr, role = _STATUS_STYLE.get(kind, _STATUS_STYLE["info"])
        self._spinner.setVisible(busy)
        self._icon.setVisible(not busy and bool(icon_name))
        if not busy and icon_name:
            self._icon.setPixmap(icons.pixmap(icon_name, 16, getattr(palette.current(), color_attr)))
        self._text.setProperty("role", "muted" if busy else role)
        repolish(self._text)

    def retint(self) -> None:
        self.set_status(self._text.text(), self._kind)


class Avatar(QWidget):
    """Initials in an accent circle (no network image needed)."""

    def __init__(self, size: int = 44, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._initials = ""

    def set_name(self, name: str) -> None:
        parts = [p for p in name.split("@", 1)[0].replace("_", " ").replace(".", " ").split() if p]
        if not parts:
            self._initials = ""  # painted as a generic user icon
        elif len(parts) == 1:
            self._initials = parts[0][:2].upper()
        else:
            self._initials = (parts[0][0] + parts[-1][0]).upper()
        self.update()

    def initials(self) -> str:
        return self._initials

    def paintEvent(self, event: QPaintEvent | None) -> None:  # noqa: N802
        p = palette.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if not self._initials:
            painter.setBrush(QColor(p.surface_alt))
            painter.setPen(QPen(QColor(p.border), 1))
            painter.drawEllipse(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1))
            size = int(self.height() * 0.5)
            offset = (self.width() - size) // 2
            painter.drawPixmap(offset, (self.height() - size) // 2, icons.pixmap("user", size, p.text_muted))
            painter.end()
            return
        painter.setBrush(QColor(p.accent))
        painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        font = painter.font()
        font.setPixelSize(max(10, int(self.height() * 0.38)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(p.accent_text))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self._initials)
        painter.end()
