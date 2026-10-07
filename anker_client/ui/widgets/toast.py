"""In-window toast notifications (bottom-right stack).

``ToastManager(host)`` overlays toasts on ``host`` (the main window's content
area): newest at the bottom, at most ``max_toasts`` (the oldest is dropped),
auto-dismissed after 4 s (errors 8 s, toasts with an action 7 s; the timer
pauses while the pointer is over a toast), click anywhere on a toast to dismiss
it. Each level has its own colour stripe + icon: info | success | warning | error.
An optional action button runs a callback and dismisses the toast. Long
tokens in the text (paths, URLs) wrap instead of being cut off.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, icon_button, label

log = logging.getLogger(__name__)

LEVELS = ("info", "success", "warning", "error")
DEFAULT_TIMEOUT_MS = 4000
ERROR_TIMEOUT_MS = 8000
ACTION_TIMEOUT_MS = 7000
MAX_TOASTS = 4
TOAST_WIDTH = 380
_SHADOW = 10  # px around the card reserved for the painted shadow
_ICONS = {"info": "info", "success": "check_circle", "warning": "warning", "error": "error"}


def normalize_level(level: str) -> str:
    return level if level in LEVELS else "info"


_ZWSP = "\u200b"  # zero-width space: a line-break opportunity that renders as nothing
_LONG_TOKEN = 24
_BREAK_AFTER = "\\/._-:?&=,"


def breakable(text: str) -> str:
    """``text`` with invisible break opportunities inside long tokens (paths, URLs, hashes).

    Word wrap only breaks at spaces; a long Windows path in an error message would otherwise be cut
    off at the toast's fixed width.
    """

    def split(token: str) -> str:
        if len(token) <= _LONG_TOKEN:
            return token
        out: list[str] = []
        run = 0
        for ch in token:
            out.append(ch)
            run += 1
            if ch in _BREAK_AFTER or run >= _LONG_TOKEN:
                out.append(_ZWSP)
                run = 0
        return "".join(out)

    return "\n".join(" ".join(split(tok) for tok in line.split(" ")) for line in text.split("\n"))


def level_color(level: str) -> str:
    p = palette.current()
    return {"info": p.info, "success": p.success, "warning": p.warning, "error": p.danger}[normalize_level(level)]


def default_timeout(level: str, has_action: bool) -> int:
    if normalize_level(level) == "error":
        return ERROR_TIMEOUT_MS
    return ACTION_TIMEOUT_MS if has_action else DEFAULT_TIMEOUT_MS


class Toast(QWidget):
    """One notification card. Emits ``dismissed(self)`` exactly once."""

    dismissed = pyqtSignal(object)

    def __init__(
        self,
        message: str,
        level: str = "info",
        *,
        title: str = "",
        action_text: str = "",
        on_action: Callable[[], None] | None = None,
        timeout_ms: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.level = normalize_level(level)
        self.title = title
        self.message = message
        self._on_action = on_action
        self._done = False
        self._timeout_ms = timeout_ms if timeout_ms is not None else default_timeout(self.level, bool(action_text))
        self._remaining = self._timeout_ms
        self._clock = QElapsedTimer()
        self._fade: QPropertyAnimation | None = None
        self._slide: QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(TOAST_WIDTH + 2 * _SHADOW)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self._build(action_text)
        self.adjustSize()

    # --- construction -------------------------------------------------------------------
    def _build(self, action_text: str) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(_SHADOW + 16, _SHADOW + 12, _SHADOW + 8, _SHADOW + 12)
        outer.setSpacing(12)

        self._icon = QLabel()
        self._icon.setPixmap(icons.pixmap(_ICONS[self.level], 20, level_color(self.level)))
        self._icon.setFixedSize(20, 20)
        outer.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(3)
        column.setContentsMargins(0, 1, 0, 0)
        if self.title:
            self._title_label = label(breakable(self.title), "heading", wrap=True)
            column.addWidget(self._title_label)
        self._message_label = label(breakable(self.message), "muted" if self.title else "", wrap=True)
        self._message_label.setVisible(bool(self.message))
        column.addWidget(self._message_label)
        self.action_button = None
        if action_text:
            self.action_button = button(action_text, variant="link", on_click=self._trigger_action)
            self.action_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            column.addSpacing(4)
            column.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(column, 1)

        self.close_button = icon_button("close", "Dismiss", on_click=self.dismiss, size=14)
        outer.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

    # --- behaviour ----------------------------------------------------------------------
    @property
    def is_dismissed(self) -> bool:
        return self._done

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    def start_timer(self) -> None:
        if self._timeout_ms > 0 and not self._done:
            self._clock.start()
            self._timer.start(max(1, self._remaining))

    def dismiss(self) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.dismissed.emit(self)

    def _trigger_action(self) -> None:
        callback = self._on_action
        self.dismiss()
        if callback is not None:
            try:
                callback()
            except Exception:
                log.exception("Toast action failed")

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._timer.isActive():
            self._remaining = max(500, self._remaining - self._clock.elapsed())
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if not self._done and not self._timer.isActive():
            self.start_timer()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.dismiss()
        super().mouseReleaseEvent(event)

    # --- animation (owned by the toast so it dies with it) ------------------------------------
    def fade_in(self, duration_ms: int = 160) -> None:
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        self._fade = QPropertyAnimation(effect, b"opacity", self)
        self._fade.setDuration(duration_ms)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.finished.connect(self._drop_effect)
        self._fade.start()

    def _drop_effect(self) -> None:
        # The effect forces offscreen rendering of the whole toast; drop it once fully visible.
        self.setGraphicsEffect(None)

    def slide_to(self, target: QPoint, duration_ms: int = 180) -> None:
        if self._slide is None:
            self._slide = QPropertyAnimation(self, b"pos", self)
            self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.stop()
        self._slide.setDuration(duration_ms)
        self._slide.setEndValue(target)
        self._slide.start()

    def stop_animations(self) -> None:
        for anim in (self._fade, self._slide):
            if anim is not None:
                anim.stop()

    # --- painting -------------------------------------------------------------------------
    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(_SHADOW, _SHADOW, -_SHADOW, -_SHADOW)
        radius = max(4, pal.radius + 2)
        shadow = QColor(0, 0, 0)
        strength = 46 if pal.dark else 22
        for step in range(_SHADOW, 0, -2):  # soft shadow: stacked translucent rounded rects
            shadow.setAlpha(int(strength / step) + (2 if pal.dark else 1))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shadow)
            p.drawRoundedRect(card.adjusted(-step / 2, -step / 2 + 3, step / 2, step / 2 + 3),
                              radius + step / 2, radius + step / 2)
        path = QPainterPath()
        path.addRoundedRect(card, radius, radius)
        p.fillPath(path, QColor(pal.elevated))
        p.setClipPath(path)
        p.fillRect(QRectF(card.left(), card.top(), 4, card.height()), QColor(level_color(self.level)))
        p.setClipping(False)
        border = QColor(pal.border)
        p.setPen(border)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.end()


class ToastManager(QObject):
    """Owns the toast stack laid over ``host``."""

    changed = pyqtSignal()

    def __init__(self, host: QWidget, *, max_toasts: int = MAX_TOASTS, margin: int = 14,
                 animate: bool = True) -> None:
        super().__init__(host)
        self._host = host
        self._max = max(1, max_toasts)
        self._margin = margin
        self._animate = animate
        self._toasts: list[Toast] = []
        host.installEventFilter(self)

    def toasts(self) -> list[Toast]:
        return list(self._toasts)

    def show_toast(
        self,
        message: str,
        level: str = "info",
        *,
        title: str = "",
        action_text: str = "",
        on_action: Callable[[], None] | None = None,
        timeout_ms: int | None = None,
    ) -> Toast:
        toast = Toast(message, level, title=title, action_text=action_text, on_action=on_action,
                      timeout_ms=timeout_ms, parent=self._host)
        toast.dismissed.connect(self._on_dismissed)
        self._toasts.append(toast)
        while len(self._toasts) > self._max:
            self._toasts[0].dismiss()
        toast.adjustSize()
        toast.move(self._slot_position(len(self._toasts) - 1, toast, entering=True))
        toast.show()
        toast.raise_()
        if self._animate:
            toast.fade_in()
        self._relayout()
        toast.start_timer()
        self.changed.emit()
        return toast

    def clear(self) -> None:
        for toast in list(self._toasts):
            toast.dismiss()

    # --- layout ---------------------------------------------------------------------------
    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: N802
        if obj is self._host and event is not None and event.type() == QEvent.Type.Resize:
            self._relayout(animated=False)
        return False

    def _slot_position(self, index: int, toast: Toast, *, entering: bool = False) -> QPoint:
        # Actual heights (set by adjustSize, which honours word-wrap height-for-width), not sizeHint().
        bottom = self._host.height() - self._margin + _SHADOW
        for later in self._toasts[index + 1:]:
            bottom -= later.height() - _SHADOW
        height = toast.height()
        x = self._host.width() - toast.width() - self._margin + _SHADOW
        y = bottom - height + (24 if entering else 0)
        return QPoint(max(0, x), max(0, y))

    def _relayout(self, *, animated: bool = True) -> None:
        for index, toast in enumerate(self._toasts):
            target = self._slot_position(index, toast)
            if toast.pos() == target:
                continue
            if animated and self._animate and toast.isVisible():
                toast.slide_to(target)
            else:
                toast.move(target)
            toast.raise_()

    def _on_dismissed(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        toast.stop_animations()
        toast.hide()
        toast.deleteLater()
        self._relayout()
        self.changed.emit()
