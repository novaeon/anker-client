"""High-performance cover grid (model/view + painted delegate).

Used by the Store (search/browse results, wishlist) and the Library. Each item
is a :class:`CoverItem`; the view paints 2:3 covers loaded through the shared
:class:`ImageLoader`, a title, a subtitle, up to three badges, an optional
progress bar (downloads in progress) and a hover lift. Thousands of items stay
smooth because nothing is a QWidget per card.

Signals on :class:`CoverGridView`:
* ``item_activated(key)`` — click / Enter
* ``context_requested(key, QPoint global)`` — right-click
* ``near_end()`` — scrolled within ~1.5 rows of the bottom (infinite scroll)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from PyQt6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette


@dataclass(frozen=True, slots=True)
class CoverBadge:
    text: str
    kind: str = ""  # "" | accent | success | warning | danger


@dataclass(frozen=True, slots=True)
class CoverItem:
    key: str  # slug or install id
    title: str
    subtitle: str = ""
    cover_url: str = ""
    badges: tuple[CoverBadge, ...] = ()
    progress: float | None = None  # 0..1 shows a bar over the cover bottom
    dimmed: bool = False  # e.g. hidden/unmanaged games
    favorite: bool = False
    payload: Any = field(default=None, compare=False)  # the GameSummary / InstalledGame


KEY_ROLE = Qt.ItemDataRole.UserRole + 1
ITEM_ROLE = Qt.ItemDataRole.UserRole + 2


class CoverGridModel(QAbstractListModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[CoverItem] = []
        self._index: dict[str, int] = {}

    # --- Qt model API --------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.title}\n{item.subtitle}" if item.subtitle else item.title
        if role == KEY_ROLE:
            return item.key
        if role == ITEM_ROLE:
            return item
        return None

    # --- mutations ------------------------------------------------------------------
    def set_items(self, items: list[CoverItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._reindex()
        self.endResetModel()

    def append_items(self, items: list[CoverItem]) -> None:
        fresh = [i for i in items if i.key not in self._index]
        if not fresh:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
        self._items.extend(fresh)
        self._reindex()
        self.endInsertRows()

    def update_item(self, key: str, **changes: Any) -> None:
        row = self._index.get(key)
        if row is None:
            return
        self._items[row] = replace(self._items[row], **changes)
        idx = self.index(row)
        self.dataChanged.emit(idx, idx)

    def replace_item(self, item: CoverItem) -> None:
        row = self._index.get(item.key)
        if row is None:
            return
        self._items[row] = item
        idx = self.index(row)
        self.dataChanged.emit(idx, idx)

    def remove_key(self, key: str) -> None:
        row = self._index.get(key)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        self._reindex()
        self.endRemoveRows()

    def item(self, key: str) -> CoverItem | None:
        row = self._index.get(key)
        return self._items[row] if row is not None else None

    def items(self) -> list[CoverItem]:
        return list(self._items)

    def row_of(self, key: str) -> int:
        return self._index.get(key, -1)

    def refresh_urls(self, url: str) -> None:
        """Repaint rows whose cover is ``url`` (called when an image finishes loading)."""
        for row, item in enumerate(self._items):
            if item.cover_url == url:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx)

    def _reindex(self) -> None:
        self._index = {item.key: row for row, item in enumerate(self._items)}


class CoverDelegate(QStyledItemDelegate):
    TEXT_HEIGHT = 46
    PADDING = 6

    def __init__(self, loader: ImageLoader, view: CoverGridView) -> None:
        super().__init__(view)
        self._loader = loader
        self._view = view

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return self._view.cell_size()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item: CoverItem | None = index.data(ITEM_ROLE)
        if item is None:
            return
        pal = palette.current()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cell = QRect(option.rect)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        lift = 3 if hovered else 0
        cover_w = cell.width() - 2 * self.PADDING
        cover_h = int(cover_w * 1.5)
        cover = QRectF(cell.x() + self.PADDING, cell.y() + self.PADDING - lift, cover_w, cover_h)
        radius = float(pal.radius)

        path = QPainterPath()
        path.addRoundedRect(cover, radius, radius)

        # shadow
        if hovered:
            shadow = QPainterPath()
            shadow.addRoundedRect(cover.translated(0, 4), radius, radius)
            painter.fillPath(shadow, QColor(0, 0, 0, 90))

        painter.save()
        painter.setClipPath(path)
        painter.fillRect(cover, QColor(pal.surface_alt))
        dpr = self._view.devicePixelRatioF()
        target = QSize(int(cover_w * dpr), int(cover_h * dpr))
        pixmap = self._loader.request(item.cover_url, target) if item.cover_url else None
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(cover, pixmap, QRectF(pixmap.rect()))
        else:
            painter.setPen(QColor(pal.text_faint))
            font = QFont(option.font)
            font.setPointSizeF(22)
            font.setBold(True)
            painter.setFont(font)
            initials = "".join(w[0] for w in item.title.split()[:2] if w).upper() or "?"
            painter.drawText(cover, Qt.AlignmentFlag.AlignCenter, initials)
        if item.dimmed:
            painter.fillRect(cover, QColor(0, 0, 0, 120))
        # progress bar along the bottom of the cover
        if item.progress is not None:
            bar = QRectF(cover.left(), cover.bottom() - 6, cover.width(), 6)
            painter.fillRect(bar, QColor(0, 0, 0, 160))
            done = QRectF(bar.left(), bar.top(), bar.width() * max(0.0, min(1.0, item.progress)), bar.height())
            painter.fillRect(done, QColor(pal.accent))
        painter.restore()

        # border / selection ring
        ring = QColor(pal.accent) if (selected or hovered) else QColor(pal.border)
        pen = QPen(ring, 2 if selected else 1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # badges (top-left, stacked horizontally)
        bx = cover.left() + 6
        by = cover.top() + 6
        small = QFont(option.font)
        small.setPointSizeF(max(7.5, option.font.pointSizeF() - 1.5))
        small.setBold(True)
        painter.setFont(small)
        fm = QFontMetrics(small)
        for badge in item.badges[:3]:
            text_w = fm.horizontalAdvance(badge.text)
            rect = QRectF(bx, by, text_w + 12, fm.height() + 4)
            if rect.right() > cover.right() - 6:
                break
            bg = {
                "accent": pal.accent,
                "success": pal.success,
                "warning": pal.warning,
                "danger": pal.danger,
            }.get(badge.kind, pal.elevated)
            fg = pal.accent_text if badge.kind else pal.text
            badge_path = QPainterPath()
            badge_path.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.fillPath(badge_path, QColor(bg))
            painter.setPen(QColor(fg))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge.text)
            bx = rect.right() + 4

        if item.favorite:
            from anker_client.ui import icons

            heart = icons.pixmap("heart_filled", 16, pal.danger)
            painter.drawPixmap(int(cover.right() - 22), int(cover.top() + 6), heart)

        # title + subtitle
        text_rect = QRectF(cell.x() + self.PADDING, cover.bottom() + 6 + lift, cover_w, self.TEXT_HEIGHT)
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(pal.text))
        tfm = QFontMetrics(title_font)
        painter.drawText(
            QRectF(text_rect.left(), text_rect.top(), text_rect.width(), tfm.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            tfm.elidedText(item.title, Qt.TextElideMode.ElideRight, int(text_rect.width())),
        )
        if item.subtitle:
            sub_font = QFont(option.font)
            sub_font.setPointSizeF(max(7.5, option.font.pointSizeF() - 1))
            painter.setFont(sub_font)
            painter.setPen(QColor(pal.text_muted))
            sfm = QFontMetrics(sub_font)
            painter.drawText(
                QRectF(text_rect.left(), text_rect.top() + tfm.height() + 2, text_rect.width(), sfm.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                sfm.elidedText(item.subtitle, Qt.TextElideMode.ElideRight, int(text_rect.width())),
            )
        painter.restore()


class CoverGridView(QListView):
    item_activated = pyqtSignal(str)
    context_requested = pyqtSignal(str, QPoint)
    near_end = pyqtSignal()

    MIN_CARD_WIDTH = 150
    MAX_CARD_WIDTH = 210
    SPACING = 10

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "grid")
        self._model = CoverGridModel(self)
        self.setModel(self._model)
        self._delegate = CoverDelegate(loader, self)
        self.setItemDelegate(self._delegate)
        self._loader = loader
        self._card_width = 170
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(self.SPACING)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.clicked.connect(self._emit_activated)
        self.activated.connect(self._emit_activated)
        self.verticalScrollBar().valueChanged.connect(self._check_near_end)
        loader.loaded.connect(lambda url, _k: self._model.refresh_urls(url))

    @property
    def grid_model(self) -> CoverGridModel:
        return self._model

    # --- sizing ----------------------------------------------------------------------
    def cell_size(self) -> QSize:
        cover_h = int((self._card_width - 2 * CoverDelegate.PADDING) * 1.5)
        return QSize(self._card_width, cover_h + 2 * CoverDelegate.PADDING + CoverDelegate.TEXT_HEIGHT + 4)

    def _recompute_card_width(self) -> None:
        available = max(1, self.viewport().width() - 2 * self.SPACING)
        columns = max(1, (available + self.SPACING) // (self.MIN_CARD_WIDTH + 2 * self.SPACING))
        width = min(self.MAX_CARD_WIDTH, available // columns - 2 * self.SPACING)
        width = max(self.MIN_CARD_WIDTH - 20, width)
        if width != self._card_width:
            self._card_width = width
            self.setGridSize(QSize(width + self.SPACING, self.cell_size().height() + self.SPACING))
            self.scheduleDelayedItemsLayout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._recompute_card_width()
        self._check_near_end()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._recompute_card_width()

    # --- events ----------------------------------------------------------------------
    def _emit_activated(self, index: QModelIndex) -> None:
        key = index.data(KEY_ROLE)
        if key:
            self.item_activated.emit(key)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.pos())
        if index.isValid():
            self.setCurrentIndex(index)
            self.context_requested.emit(index.data(KEY_ROLE), event.globalPos())

    def _check_near_end(self, *_args: Any) -> None:
        bar = self.verticalScrollBar()
        threshold = int(self.cell_size().height() * 1.5)
        if self._model.rowCount() and (bar.maximum() - bar.value() <= threshold):
            self.near_end.emit()

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.Leave:
            self.viewport().update()
        return super().event(e)

    # --- convenience -------------------------------------------------------------------
    def set_items(self, items: list[CoverItem]) -> None:
        self._model.set_items(items)
        self.scrollToTop()

    def append_items(self, items: list[CoverItem]) -> None:
        self._model.append_items(items)

    def select_key(self, key: str) -> None:
        row = self._model.row_of(key)
        if row >= 0:
            index = self._model.index(row)
            self.setCurrentIndex(index)
            self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    def current_key(self) -> str:
        index = self.currentIndex()
        return index.data(KEY_ROLE) or "" if index.isValid() else ""
