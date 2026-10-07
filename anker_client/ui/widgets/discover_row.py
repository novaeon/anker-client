"""Horizontal "Discover" rows: a section header over a single-line, horizontally scrolling cover strip.

* :class:`HorizontalCoverView` is a ``CoverGridView`` configured as a strip:
  no wrapping, left-to-right flow, fixed height (one card + scrollbar),
  horizontal scrollbar as needed, and the mouse wheel scrolls horizontally.
* Wheel latching: a vertical page scroll that sweeps over a row keeps
  scrolling the page (the row ignores wheel events for ``WheelLatch.WINDOW``
  seconds after the page last scrolled). A row also passes the wheel on to the
  page when it is already at its start/end, so the page never gets "stuck".
* :class:`DiscoverRow` adds the header: title, optional "See all" link and
  previous/next buttons that scroll the strip by one viewport.
* :class:`LatchingScrollArea` is the vertical page scroller that feeds the latch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QAbstractItemView, QListView, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.common import button, hbox, icon_button, label
from anker_client.ui.widgets.cover_grid import CoverGridView, CoverItem
from anker_client.ui.widgets.game_common import tag_icon


class WheelLatch:
    """Remembers when the page last consumed a wheel event."""

    WINDOW = 0.35  # seconds

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = float("-inf")

    def touch(self) -> None:
        self._last = self._clock()

    def active(self) -> bool:
        return self._clock() - self._last < self.WINDOW


class LatchingScrollArea(QScrollArea):
    """Vertical page scroller that records page scrolls in a :class:`WheelLatch`."""

    def __init__(self, latch: WheelLatch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._latch = latch
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.verticalScrollBar().setSingleStep(40)

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is not None and event.angleDelta().y() != 0:
            self._latch.touch()
        super().wheelEvent(event)


class HorizontalCoverView(CoverGridView):
    """A one-line, horizontally scrolling cover strip."""

    WHEEL_PIXELS_PER_NOTCH = 120

    def __init__(self, loader: ImageLoader, latch: WheelLatch | None = None, parent: QWidget | None = None) -> None:
        super().__init__(loader, parent)
        self._latch = latch
        self.setWrapping(False)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.horizontalScrollBar().setSingleStep(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._sync_height()

    def set_items(self, items: list[CoverItem]) -> None:
        self.grid_model.set_items(items)
        self.horizontalScrollBar().setValue(0)

    def _sync_height(self) -> None:
        cell = self.gridSize() if self.gridSize().isValid() else self.cell_size()
        bar = self.horizontalScrollBar().sizeHint().height()
        height = cell.height() + bar + 4
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._sync_height()

    def wheelEvent(self, e: QWheelEvent | None) -> None:
        if e is None:
            return
        bar = self.horizontalScrollBar()
        delta = e.angleDelta()
        step = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        if step == 0 or bar.maximum() <= bar.minimum():
            e.ignore()
            return
        horizontal_gesture = abs(delta.x()) > abs(delta.y())
        if not horizontal_gesture and self._latch is not None and self._latch.active():
            e.ignore()  # the user is scrolling the page; don't hijack it
            return
        at_start = bar.value() <= bar.minimum() and step > 0
        at_end = bar.value() >= bar.maximum() and step < 0
        if at_start or at_end:
            e.ignore()  # let the page scroll on
            return
        pixels = e.pixelDelta()
        moved = (pixels.x() or pixels.y()) if not pixels.isNull() else \
            round(step / 120 * self.WHEEL_PIXELS_PER_NOTCH)
        bar.setValue(bar.value() - moved)
        e.accept()


class DiscoverRow(QWidget):
    """Section header ("Trending Games" · See all · ‹ ›) over a :class:`HorizontalCoverView`."""

    see_all_clicked = pyqtSignal()
    item_activated = pyqtSignal(str)
    context_requested = pyqtSignal(str, QPoint)

    def __init__(self, title: str, loader: ImageLoader, *, latch: WheelLatch | None = None,
                 see_all: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self.title_label = label(title, "title")
        self.count_label = label("", "faint")
        self.see_all_button = button("See all", variant="link", on_click=self.see_all_clicked.emit)
        self.see_all_button.setVisible(see_all)
        self.prev_button = icon_button("chevron_left", "Scroll left", on_click=lambda: self.scroll_page(-1))
        self.next_button = icon_button("chevron_right", "Scroll right", on_click=lambda: self.scroll_page(1))
        tag_icon(self.prev_button, "chevron_left")
        tag_icon(self.next_button, "chevron_right")
        self.view = HorizontalCoverView(loader, latch, self)
        self.view.item_activated.connect(self.item_activated)
        self.view.context_requested.connect(self.context_requested)
        bar = self.view.horizontalScrollBar()
        bar.valueChanged.connect(self._update_arrows)
        bar.rangeChanged.connect(self._update_arrows)
        self._animation = QPropertyAnimation(bar, b"value", self)
        self._animation.setDuration(260)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        header = QWidget(self)
        header.setLayout(hbox(self.title_label, self.count_label, None, self.see_all_button,
                              self.prev_button, self.next_button, spacing=8, margins=(4, 0, 4, 0)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(header)
        layout.addWidget(self.view)
        self._update_arrows()

    @property
    def title(self) -> str:
        return self._title

    def set_items(self, items: list[CoverItem]) -> None:
        self.view.set_items(items)
        self.count_label.setText(str(len(items)) if items else "")
        self._update_arrows()

    def items(self) -> list[CoverItem]:
        return self.view.grid_model.items()

    def scroll_page(self, direction: int) -> None:
        bar = self.view.horizontalScrollBar()
        cell = max(1, self.view.gridSize().width())
        page = max(cell, (self.view.viewport().width() // cell) * cell)
        target = max(bar.minimum(), min(bar.maximum(), bar.value() + direction * page))
        self._animation.stop()
        self._animation.setStartValue(bar.value())
        self._animation.setEndValue(target)
        self._animation.start()

    def _update_arrows(self, *_args: object) -> None:
        bar = self.view.horizontalScrollBar()
        scrollable = bar.maximum() > bar.minimum()
        self.prev_button.setVisible(scrollable)
        self.next_button.setVisible(scrollable)
        self.prev_button.setEnabled(bar.value() > bar.minimum())
        self.next_button.setEnabled(bar.value() < bar.maximum())
