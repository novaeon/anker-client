"""Shows Steam-style pop-ups for ``AchievementUnlocked`` events (GUI thread).

* Honours ``achievement_popups``, ``achievement_sound`` and
  ``achievement_position`` (bottom/top × right/left corner of the primary
  screen's work area); preview events from Settings show even when pop-ups
  are off.
* Up to ``MAX_VISIBLE`` cards stack away from the corner (Steam stacks several
  unlocks); further ones wait in a FIFO queue and take the first free slot.
* Each card slides in from the screen edge and fades in (260 ms), stays
  ``HOLD_MS`` (rare: ``RARE_HOLD_MS``), then fades out (350 ms). The
  achievement sound (``resources/achievement.wav``) plays when a card appears.
* A card waits up to ``ICON_WAIT_MS`` for its icon (normally prefetched when
  the game started) so it does not pop in empty.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from PyQt6.QtCore import QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QTimer
from PyQt6.QtGui import QGuiApplication

from anker_client.core import events as ev
from anker_client.ui.achievement_sound import AchievementSound
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.achievement_toast import AchievementToast, window_size

log = logging.getLogger(__name__)

MAX_VISIBLE = 3
HOLD_MS = 5500
RARE_HOLD_MS = 7000
IN_MS = 260
OUT_MS = 350
ICON_WAIT_MS = 1200
SLIDE = 26  # px the card travels while appearing
GAP = -10  # cards overlap their transparent shadow margins
EDGE = 4  # distance from the work-area edge (the card's own shadow margin adds more)


@dataclass
class _Shown:
    toast: AchievementToast
    slot: int
    hold: QTimer
    animations: list[QPropertyAnimation]


class AchievementNotifier(QObject):
    def __init__(self, ctx: object, bridge: QtEventBridge, loader: ImageLoader | None,
                 sound: AchievementSound | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._loader = loader
        self._sound = sound
        self._queue: deque[ev.AchievementUnlocked] = deque()
        self._shown: list[_Shown] = []
        self._waiting: dict[int, QTimer] = {}  # id(toast) → icon wait timer
        self._closed = False
        bridge.achievement_unlocked.connect(self.notify)

    # --- public ------------------------------------------------------------------------
    def notify(self, event: ev.AchievementUnlocked) -> None:
        if self._closed:
            return
        settings = self._ctx.settings.get()  # type: ignore[attr-defined]
        if not event.preview and not settings.achievement_popups:
            return
        self._queue.append(event)
        self._pump()

    def visible_toasts(self) -> list[AchievementToast]:
        return [s.toast for s in self._shown]

    def queued(self) -> int:
        return len(self._queue)

    def shutdown(self) -> None:
        self._closed = True
        self._queue.clear()
        for timer in self._waiting.values():
            timer.stop()
        self._waiting.clear()
        for shown in list(self._shown):
            self._dispose(shown)

    # --- layout ------------------------------------------------------------------------
    def _position(self) -> str:
        return str(self._ctx.settings.get().achievement_position)  # type: ignore[attr-defined]

    @staticmethod
    def _work_area() -> QRect:
        screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

    def slot_geometry(self, slot: int, position: str | None = None) -> QPoint:
        """Top-left of the toast window in ``slot`` (0 = closest to the corner)."""
        position = position or self._position()
        area = self._work_area()
        size = window_size()
        step = size.height() + GAP
        x = area.left() + EDGE if position.endswith("left") else area.right() + 1 - EDGE - size.width()
        if position.startswith("top"):
            y = area.top() + EDGE + slot * step
        else:
            y = area.bottom() + 1 - EDGE - size.height() - slot * step
        return QPoint(x, y)

    def _free_slot(self) -> int | None:
        used = {s.slot for s in self._shown}
        return next((slot for slot in range(MAX_VISIBLE) if slot not in used), None)

    # --- showing -----------------------------------------------------------------------
    def _pump(self) -> None:
        while self._queue and len(self._shown) + len(self._waiting) < MAX_VISIBLE:
            event = self._queue.popleft()
            toast = AchievementToast(event.achievement, self._loader,
                                     unlocked_count=event.unlocked_count, total=event.total)
            if toast.has_icon or not event.achievement.icon_url:
                self._show(toast)
                continue
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(ICON_WAIT_MS)
            key = id(toast)
            timer.timeout.connect(lambda t=toast: self._icon_ready(t))
            toast.icon_changed.connect(lambda t=toast: self._icon_ready(t))
            self._waiting[key] = timer
            timer.start()

    def _icon_ready(self, toast: AchievementToast) -> None:
        timer = self._waiting.pop(id(toast), None)
        if timer is None or self._closed:
            return
        timer.stop()
        self._show(toast)

    def _show(self, toast: AchievementToast) -> None:
        slot = self._free_slot()
        if slot is None:  # cannot happen with the counting above; keep the card for later
            slot = 0
        position = self._position()
        final = self.slot_geometry(slot, position)
        direction = -1 if position.startswith("top") else 1
        toast.move(final + QPoint(0, SLIDE * direction))
        toast.setWindowOpacity(0.0)
        toast.show()
        toast.start_effects()
        slide = QPropertyAnimation(toast, b"pos", self)
        slide.setDuration(IN_MS)
        slide.setStartValue(final + QPoint(0, SLIDE * direction))
        slide.setEndValue(final)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade = QPropertyAnimation(toast, b"windowOpacity", self)
        fade.setDuration(IN_MS)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        slide.start()
        fade.start()
        hold = QTimer(self)
        hold.setSingleShot(True)
        hold.setInterval(RARE_HOLD_MS if toast.rare else HOLD_MS)
        shown = _Shown(toast, slot, hold, [slide, fade])
        hold.timeout.connect(lambda s=shown: self._hide(s))
        hold.start()
        self._shown.append(shown)
        settings = self._ctx.settings.get()  # type: ignore[attr-defined]
        if self._sound is not None and settings.achievement_sound:
            self._sound.play()

    def _hide(self, shown: _Shown) -> None:
        if shown not in self._shown:
            return
        fade = QPropertyAnimation(shown.toast, b"windowOpacity", self)
        fade.setDuration(OUT_MS)
        fade.setStartValue(shown.toast.windowOpacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        fade.finished.connect(lambda s=shown: self._finish(s))
        shown.animations.append(fade)
        fade.start()

    def _finish(self, shown: _Shown) -> None:
        self._dispose(shown)
        self._pump()

    def _dispose(self, shown: _Shown) -> None:
        if shown in self._shown:
            self._shown.remove(shown)
        shown.hold.stop()
        for animation in shown.animations:
            animation.stop()
        shown.toast.stop_effects()
        shown.toast.hide()
        shown.toast.deleteLater()
