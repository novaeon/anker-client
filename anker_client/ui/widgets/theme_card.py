"""Theme picker cards: a painted miniature of each palette (window, sidebar, covers, button).

``ThemeCard`` is a checkable button showing one :class:`~anker_client.ui.theme.palette.Palette`;
``ThemePicker`` lays the cards out in a grid, keeps exactly one checked and
emits ``theme_selected(key)`` when the user picks a different one. Callers
apply the theme (``ThemeManager.apply``) and persist ``settings.theme``.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QEnterEvent,
    QFocusEvent,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QAbstractButton, QButtonGroup, QGridLayout, QSizePolicy, QWidget

from anker_client.core.paths import resource_path
from anker_client.ui import icons
from anker_client.ui.theme import palette as palettes
from anker_client.ui.theme.palette import Palette

log = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _wallpaper(name: str) -> QPixmap | None:
    """First frame of a theme wallpaper (GIFs decode their first frame), or None."""
    path = resource_path(name)
    if not path.exists():
        return None
    pixmap = QPixmap(str(path))
    return None if pixmap.isNull() else pixmap


def _with_alpha(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


class ThemeCard(QAbstractButton):
    """A checkable card previewing ``theme``."""

    def __init__(self, theme: Palette, *, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self._compact = compact
        self._hover = False
        self._keyboard_focus = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(f"{theme.name} theme")
        self.setAccessibleName(f"{theme.name} theme")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(150 if compact else 180)
        self.setFixedHeight(self._preview_height() + (34 if compact else 48))

    def _preview_height(self) -> int:
        return 66 if self._compact else 108

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(150 if self._compact else 200, self.height())

    def focusInEvent(self, event: QFocusEvent | None) -> None:  # noqa: N802
        # Only keyboard focus gets a ring; a mouse click already shows the selection.
        self._keyboard_focus = event is not None and event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent | None) -> None:  # noqa: N802
        self._keyboard_focus = False
        super().focusOutEvent(event)

    def enterEvent(self, event: QEnterEvent | None) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent | None) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    # --- painting ---------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent | None) -> None:  # noqa: N802
        ui = palettes.current()  # colours of the app chrome around the preview
        t = self.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        radius = max(4.0, float(ui.radius) + 2)

        # card body
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ui.surface))
        painter.drawRoundedRect(outer, radius, radius)

        preview = QRectF(outer.left() + 8, outer.top() + 8, outer.width() - 16, self._preview_height() - 8)
        self._paint_preview(painter, preview, t)

        # caption
        text_top = preview.bottom() + 8
        name_font = QFont(self.font())
        name_font.setPointSizeF(10 if not self._compact else 9.5)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(QColor(ui.text))
        name_rect = QRectF(outer.left() + 12, text_top, outer.width() - 24, 18)
        painter.drawText(name_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), t.name)
        if not self._compact:
            sub_font = QFont(self.font())
            sub_font.setPointSizeF(8.5)
            painter.setFont(sub_font)
            painter.setPen(QColor(ui.text_muted))
            kind = "Dark" if t.dark else "Light"
            if t.wallpaper:
                kind += " · wallpaper"
            painter.drawText(
                QRectF(outer.left() + 12, text_top + 18, outer.width() - 24, 16),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                kind,
            )

        # border / selection
        if self.isChecked():
            pen = QPen(QColor(ui.accent), 2)
        elif self._hover:
            pen = QPen(QColor(ui.text_faint), 1)
        else:
            pen = QPen(QColor(ui.border), 1)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawRoundedRect(outer, radius, radius)
        if self.hasFocus() and self._keyboard_focus and not self.isChecked():
            ring = QColor(ui.accent)
            ring.setAlpha(120)
            painter.setPen(QPen(ring, 2))
            painter.drawRoundedRect(outer.adjusted(1, 1, -1, -1), radius, radius)

        if self.isChecked():
            self._paint_check(painter, preview, ui)
        painter.end()

    def _paint_preview(self, painter: QPainter, rect: QRectF, t: Palette) -> None:
        r = max(2.0, float(min(t.radius, 8)))
        clip = QPainterPath()
        clip.addRoundedRect(rect, r, r)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(rect, QColor(t.bg))
        wallpaper = _wallpaper(t.wallpaper) if t.wallpaper else None
        if wallpaper is not None:
            scaled = wallpaper.scaled(
                rect.size().toSize() * 2,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(2.0)
            sx = max(0.0, (scaled.width() / 2 - rect.width()) / 2)
            sy = max(0.0, (scaled.height() / 2 - rect.height()) / 2)
            painter.drawPixmap(rect.topLeft(), scaled, QRectF(sx * 2, sy * 2, rect.width() * 2, rect.height() * 2))

        alpha = t.surface_alpha
        # sidebar
        side = QRectF(rect.left(), rect.top(), rect.width() * 0.26, rect.height())
        painter.fillRect(side, _with_alpha(t.surface, alpha))
        painter.fillRect(QRectF(side.right() - 1, side.top(), 1, side.height()), QColor(t.border))
        bar_h = max(4.0, rect.height() * 0.06)
        y = side.top() + rect.height() * 0.14
        for i in range(4):
            item = QRectF(side.left() + 5, y, side.width() - 10, bar_h + 4)
            if i == 1:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_with_alpha(t.accent, 70))
                painter.drawRoundedRect(item, 2, 2)
            width = item.width() * (0.55 + 0.1 * (i % 2))
            line = QRectF(item.left() + 4, item.center().y() - bar_h / 4, width, bar_h / 2)
            painter.fillRect(line, QColor(t.text if i == 1 else t.text_faint))
            y += bar_h + 9

        # main area: title line + cover tiles + primary button
        main = QRectF(side.right() + 8, rect.top() + 8, rect.right() - side.right() - 16, rect.height() - 16)
        painter.fillRect(QRectF(main.left(), main.top(), main.width() * 0.45, max(4.0, bar_h)), QColor(t.text))
        tiles_top = main.top() + bar_h + 8
        tile_gap = 5.0
        tile_w = (main.width() - tile_gap * 3) / 4
        tile_h = min(tile_w * 1.35, main.bottom() - tiles_top - bar_h - 10)
        painter.setPen(QPen(QColor(t.border), 1))
        for i in range(4):
            tile = QRectF(main.left() + i * (tile_w + tile_gap), tiles_top, tile_w, tile_h)
            painter.setBrush(_with_alpha(t.accent if i == 0 else t.surface_alt, 255 if i == 0 else max(alpha, 200)))
            painter.drawRoundedRect(tile, min(r, 3.0), min(r, 3.0))
        button = QRectF(main.left(), tiles_top + tile_h + 6, main.width() * 0.32, max(6.0, bar_h + 3))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.accent))
        painter.drawRoundedRect(button, min(r, 3.0), min(r, 3.0))
        chip = QRectF(button.right() + 5, button.top(), main.width() * 0.2, button.height())
        painter.setBrush(QColor(t.success))
        painter.drawRoundedRect(chip, min(r, 3.0), min(r, 3.0))
        painter.restore()

        painter.setPen(QPen(QColor(t.border), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, r, r)

    @staticmethod
    def _paint_check(painter: QPainter, preview: QRectF, ui: Palette) -> None:
        d = 20.0
        badge = QRectF(preview.right() - d - 6, preview.top() + 6, d, d)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ui.accent))
        painter.drawEllipse(badge)
        pix = icons.pixmap("check", 14, ui.accent_text)
        painter.drawPixmap(badge.adjusted(3, 3, -3, -3).toRect(), pix)


class ThemePicker(QWidget):
    """Grid of :class:`ThemeCard`; emits ``theme_selected(key)`` on user selection."""

    theme_selected = pyqtSignal(str)

    def __init__(
        self,
        current: str = "",
        *,
        columns: int = 3,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._cards: dict[str, ThemeCard] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for index, theme in enumerate(palettes.THEMES.values()):
            card = ThemeCard(theme, compact=compact)
            self._group.addButton(card)
            self._cards[theme.key] = card
            grid.addWidget(card, index // columns, index % columns)
            card.clicked.connect(lambda _checked=False, key=theme.key: self._on_clicked(key))
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        self.set_current(current or palettes.current().key)

    def cards(self) -> dict[str, ThemeCard]:
        return dict(self._cards)

    def current(self) -> str:
        checked = self._group.checkedButton()
        return checked.theme.key if isinstance(checked, ThemeCard) else ""

    def set_current(self, key: str) -> None:
        card = self._cards.get(key) or self._cards.get(palettes.DEFAULT_THEME)
        if card is not None:
            card.setChecked(True)
        for each in self._cards.values():
            each.update()

    def _on_clicked(self, key: str) -> None:
        for each in self._cards.values():
            each.update()
        self.theme_selected.emit(key)
