"""Steam-style "Achievement Unlocked" pop-up: a small always-on-top card in a screen corner.

Deliberately *not* themed with the app palette: it imitates Steam's in-game
notification (dark blue-grey card, 64 px icon, white title, light-blue-grey
name) so it reads as an achievement toast over any game. Rare achievements
(``Achievement.rare``, < 10 % of players) get Steam's gold treatment: a gold
title and frame, a light sweep across the card and sparkles around the icon.

The window never takes focus or input (``WindowDoesNotAcceptFocus``,
``WindowTransparentForInput``, ``WA_ShowWithoutActivating``), so it cannot
interrupt a game; it shows over windowed and borderless games, not over
exclusive full-screen ones. :class:`AchievementNotifier` places and times it.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

from anker_client.core.models import Achievement
from anker_client.ui import icons
from anker_client.ui.image_loader import ImageLoader

CARD_WIDTH = 320
CARD_HEIGHT = 84
SHADOW = 14  # transparent margin for the drop shadow
ICON_SIZE = 64
RADIUS = 4
FONT_FAMILY = "Segoe UI"

# Steam's notification palette.
BG_TOP = QColor("#2b3240")
BG_BOTTOM = QColor("#181b22")
BORDER = QColor(255, 255, 255, 26)
TITLE = QColor("#ffffff")
NAME = QColor("#c6d4df")
MUTED = QColor("#8f98a0")
GOLD = QColor("#f2c94c")
GOLD_DEEP = QColor("#b8860b")


def window_size() -> QSize:
    return QSize(CARD_WIDTH + 2 * SHADOW, CARD_HEIGHT + 2 * SHADOW)


def _font(point_size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font


class AchievementToast(QWidget):
    """One pop-up. ``set_progress`` drives the shimmer/sparkles (0 → 1 over the card's lifetime)."""

    icon_changed = pyqtSignal()

    def __init__(self, achievement: Achievement, loader: ImageLoader | None = None, *,
                 unlocked_count: int = 0, total: int = 0) -> None:
        super().__init__(None)
        self.achievement = achievement
        self.unlocked_count = unlocked_count
        self.total = total
        self.rare = achievement.rare
        self._loader = loader
        self._icon: QPixmap | None = None
        self._phase = 0.0  # seconds since shown (animations)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFixedSize(window_size())
        self.setWindowTitle("Achievement unlocked")
        self._ticker = QTimer(self)
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._tick)
        if loader is not None:
            loader.loaded.connect(self._on_loaded)
            self._request_icon()

    # --- texts ------------------------------------------------------------------------
    @property
    def title_text(self) -> str:
        return "Rare Achievement Unlocked!" if self.rare else "Achievement Unlocked!"

    @property
    def detail_text(self) -> str:
        if self.rare and self.achievement.global_percent is not None:
            return f"{_percent(self.achievement.global_percent)} of players have this achievement"
        if self.total:
            return f"{self.unlocked_count} of {self.total} achievements"
        return ""

    # --- icon -------------------------------------------------------------------------
    def _icon_request_size(self) -> QSize:
        scale = max(1.0, self.devicePixelRatioF())
        side = int(ICON_SIZE * min(2.0, scale))
        return QSize(side, side)

    def _request_icon(self) -> None:
        url = self.achievement.icon_url
        if not url or self._loader is None:
            return
        pixmap = self._loader.request(url, self._icon_request_size())
        if pixmap is not None and not pixmap.isNull():
            self._icon = pixmap
            self.update()
            self.icon_changed.emit()

    def _on_loaded(self, url: str, _size_key: str) -> None:
        if url == self.achievement.icon_url and self._icon is None:
            self._request_icon()

    @property
    def has_icon(self) -> bool:
        return self._icon is not None

    # --- animation --------------------------------------------------------------------
    def start_effects(self) -> None:
        if self.rare:
            self._ticker.start()

    def stop_effects(self) -> None:
        self._ticker.stop()

    def set_phase(self, seconds: float) -> None:
        """Jump the rare effects to ``seconds`` after showing (screenshots, tests)."""
        self._phase = seconds
        self.update()

    def _tick(self) -> None:
        self._phase += self._ticker.interval() / 1000.0
        self.update()

    # --- painting ---------------------------------------------------------------------
    def card_rect(self) -> QRectF:
        return QRectF(SHADOW, SHADOW, CARD_WIDTH, CARD_HEIGHT)

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        card = self.card_rect()
        self._paint_shadow(p, card)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)
        gradient = QLinearGradient(card.topLeft(), card.bottomLeft())
        gradient.setColorAt(0, BG_TOP)
        gradient.setColorAt(1, BG_BOTTOM)
        p.fillPath(path, gradient)
        if self.rare:
            self._paint_shimmer(p, path, card)
        p.setPen(QPen(self._border_color(), 1.0))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))  # top highlight
        p.drawLine(QPointF(card.left() + RADIUS, card.top() + 1.5), QPointF(card.right() - RADIUS, card.top() + 1.5))
        icon_rect = QRectF(card.left() + 10, card.top() + (CARD_HEIGHT - ICON_SIZE) / 2, ICON_SIZE, ICON_SIZE)
        self._paint_icon(p, icon_rect)
        if self.rare:
            self._paint_sparkles(p, icon_rect)
        self._paint_text(p, QRectF(icon_rect.right() + 12, card.top() + 10,
                                   card.right() - icon_rect.right() - 22, CARD_HEIGHT - 20))
        p.end()

    def _border_color(self) -> QColor:
        if not self.rare:
            return BORDER
        glow = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._phase * 4.0))
        color = QColor(GOLD)
        color.setAlpha(int(110 + 120 * glow))
        return color

    @staticmethod
    def _paint_shadow(p: QPainter, card: QRectF) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(SHADOW, 0, -2):
            alpha = int(46 * (1 - i / SHADOW) ** 2)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(card.adjusted(-i, -i + 3, i, i + 3), RADIUS + i, RADIUS + i)

    def _paint_shimmer(self, p: QPainter, clip: QPainterPath, card: QRectF) -> None:
        cycle = (self._phase % 2.4) / 1.1  # a sweep every 2.4 s, lasting 1.1 s
        if cycle > 1.0:
            return
        x = card.left() - 120 + (card.width() + 240) * cycle
        band = QLinearGradient(QPointF(x - 60, card.top()), QPointF(x + 60, card.bottom()))
        band.setColorAt(0.0, QColor(255, 236, 170, 0))
        band.setColorAt(0.5, QColor(255, 236, 170, 46))
        band.setColorAt(1.0, QColor(255, 236, 170, 0))
        p.save()
        p.setClipPath(clip)
        p.fillRect(card, band)
        p.restore()

    def _paint_icon(self, p: QPainter, rect: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(rect, 2, 2)
        p.save()
        p.setClipPath(path)
        if self._icon is not None:
            p.drawPixmap(rect.toRect(), self._icon)
        else:
            p.fillRect(rect, QColor("#3a4251"))
            glyph = icons.pixmap("trophy", 34, "#c6d4df")
            p.drawPixmap(int(rect.center().x() - 17), int(rect.center().y() - 17), glyph)
        p.restore()
        if self.rare:
            p.setPen(QPen(GOLD, 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 3, 3)

    def _paint_sparkles(self, p: QPainter, icon: QRectF) -> None:
        points = ((0.05, -0.08), (0.92, 0.02), (1.06, 0.62), (0.68, 1.07), (-0.07, 0.86), (0.36, -0.12))
        for index, (fx, fy) in enumerate(points):
            wave = math.sin(self._phase * 5.0 + index * 1.7)
            if wave <= 0:
                continue
            center = QPointF(icon.left() + fx * icon.width(), icon.top() + fy * icon.height())
            _star(p, center, 2.5 + 3.5 * wave, QColor(255, 240, 190, int(230 * wave)))

    def _paint_text(self, p: QPainter, rect: QRectF) -> None:
        title_font = _font(10.0, QFont.Weight.DemiBold)
        name_font = _font(9.5)
        detail_font = _font(8.25)
        lines: list[tuple[str, QFont, QColor]] = [
            (self.title_text, title_font, GOLD if self.rare else TITLE),
            (self.achievement.display_name, name_font, NAME),
        ]
        if self.detail_text:
            lines.append((self.detail_text, detail_font, GOLD if self.rare else MUTED))
        heights = [QFontMetrics(font).height() for _t, font, _c in lines]
        gap = 1.0
        y = rect.top() + max(0.0, (rect.height() - sum(heights) - gap * (len(lines) - 1)) / 2)
        for (text, font, color), height in zip(lines, heights, strict=True):
            metrics = QFontMetrics(font)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(rect.left(), y, rect.width(), height),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width())))
            y += height + gap


def _star(p: QPainter, center: QPointF, radius: float, color: QColor) -> None:
    """A four-point twinkle."""
    path = QPainterPath()
    inner = radius * 0.28
    for i in range(8):
        angle = math.pi / 4 * i - math.pi / 2
        r = radius if i % 2 == 0 else inner
        point = QPointF(center.x() + r * math.cos(angle), center.y() + r * math.sin(angle))
        if i == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.fillPath(path, color)


def _percent(value: float) -> str:
    if value < 0.1:
        return "<0.1%"
    return f"{value:.1f}%"
