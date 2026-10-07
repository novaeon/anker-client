"""Search-as-you-type picker over the local catalog (``ctx.catalog.search``).

Used by the import dialogs to say which store game an archive or folder
belongs to. Searches run on a worker after a short debounce; results of a
superseded query are dropped. Optionally offers a last row "Use “<text>” as
the title" for games that are not in the catalog (``allow_custom``).

``selection()`` returns ``(summary_or_None, title)``; ``selection_changed``
fires whenever it changes.

Enter in the search box searches now and is consumed: ``QLineEdit`` passes it
on, and the hosting dialog's default button ("Import") would otherwise fire
with the previous selection. Down moves into the results.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from anker_client.core.models import GameSummary
from anker_client.core.tasks import TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import label
from anker_client.ui.widgets.library_common import IconTinter, SearchIcon, tokenless

log = logging.getLogger(__name__)

_SUMMARY_ROLE = Qt.ItemDataRole.UserRole + 1
_CUSTOM_ROLE = Qt.ItemDataRole.UserRole + 2


def summary_caption(game: GameSummary) -> str:
    parts = [p for p in (game.primary_genre, str(game.year) if game.year else "", game.size_text) if p]
    return " · ".join(parts)


class _ResultRow(QWidget):
    def __init__(self, game: GameSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(1)
        self.title = label(game.title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        layout.addWidget(self.title)
        caption = summary_caption(game)
        if caption:
            layout.addWidget(label(caption, "caption"))


class CatalogPicker(QWidget):
    selection_changed = pyqtSignal()

    DEBOUNCE_MS = 220
    LIMIT = 40

    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *, allow_custom: bool = True,
                 placeholder: str = "Search the AnkerGames catalog") -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._allow_custom = allow_custom
        self._seq = 0
        self._handle: TaskHandle[Any] | None = None
        self._preferred_slug = ""
        self._tint = IconTinter(self)

        self.search = QLineEdit()
        self.search.setProperty("role", "search")
        self.search.setPlaceholderText(placeholder)
        self.search.setClearButtonEnabled(True)
        self._search_icon = SearchIcon(self.search)
        self.results = QListWidget()
        self.results.setMinimumHeight(170)
        self.results.setIconSize(QSize(16, 16))
        self.status = label("Type a few letters of the game's name.", "caption")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.search)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._run_search)
        self.search.textChanged.connect(lambda _t: self._timer.start())
        self.search.installEventFilter(self)
        self.results.currentItemChanged.connect(lambda *_a: self.selection_changed.emit())
        self._tint.on_theme_changed(self._apply_icons)
        self._apply_icons()

    # --- public API -----------------------------------------------------------------------
    def set_query(self, text: str, *, prefer_slug: str = "") -> None:
        """Search for ``text`` now and select ``prefer_slug`` when it is among the results."""
        self._preferred_slug = prefer_slug
        self.search.blockSignals(True)
        self.search.setText(text)
        self.search.blockSignals(False)
        self._run_search()

    def selection(self) -> tuple[GameSummary | None, str]:
        item = self.results.currentItem()
        if item is None:
            return None, ""
        if item.data(_CUSTOM_ROLE):
            return None, self.search.text().strip()
        summary: GameSummary | None = item.data(_SUMMARY_ROLE)
        return summary, summary.title if summary else ""

    def has_selection(self) -> bool:
        return bool(self.selection()[1])

    def shutdown(self) -> None:
        self._timer.stop()
        if self._handle is not None:
            self._handle.cancel()

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: N802
        if obj is self.search and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_search()
                return True  # never reaches the dialog's default button
            if event.key() == Qt.Key.Key_Down and self.results.count():
                self.results.setFocus()
                if self.results.currentRow() < 0:
                    self.results.setCurrentRow(0)
                return True
        return super().eventFilter(obj, event)

    # --- search -----------------------------------------------------------------------------
    def _run_search(self) -> None:
        self._timer.stop()
        query = self.search.text().strip()
        self._seq += 1
        seq = self._seq
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if not query:
            self.results.clear()
            self.status.setText("Type a few letters of the game's name.")
            self.selection_changed.emit()
            return
        self.status.setText("Searching…")
        self._handle = run_async(
            self, self._ctx.runner, tokenless(self._ctx.catalog.search, query, limit=self.LIMIT),
            on_result=lambda games, s=seq, q=query: self._show_results(s, q, games),
            on_error=lambda exc, s=seq: self._show_error(s, exc),
        )

    def _show_results(self, seq: int, query: str, games: list[GameSummary]) -> None:
        if seq != self._seq:
            return
        previous_slug = self._current_slug()
        wanted = self._preferred_slug or previous_slug
        self.results.blockSignals(True)
        self.results.clear()
        select_row = 0 if games else -1
        for row, game in enumerate(games or []):
            item = QListWidgetItem()
            item.setData(_SUMMARY_ROLE, game)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, game.title)
            item.setToolTip(game.page_url)
            item.setSizeHint(QSize(0, 52))
            self.results.addItem(item)
            self.results.setItemWidget(item, _ResultRow(game))
            if wanted and game.slug == wanted:
                select_row = row
        if self._allow_custom:
            custom = QListWidgetItem(f"Not listed? Use “{query}” as the title")
            custom.setData(_CUSTOM_ROLE, True)
            custom.setIcon(icons.icon("plus", palette.current().text_muted))
            custom.setSizeHint(QSize(0, 40))
            self.results.addItem(custom)
        if select_row >= 0:
            self.results.setCurrentRow(select_row)
        self.results.blockSignals(False)
        count = len(games or [])
        if count:
            self.status.setText(f"{count}{'+' if count >= self.LIMIT else ''} match{'es' if count != 1 else ''}"
                                " in the catalog.")
        else:
            self.status.setText("No games in the catalog match that name.")
        self._preferred_slug = ""
        self.selection_changed.emit()

    def _show_error(self, seq: int, exc: BaseException) -> None:
        if seq != self._seq:
            return
        log.warning("Catalog search failed: %s", exc)
        self.status.setText(f"Search failed: {error_text(exc)}")

    def _current_slug(self) -> str:
        summary, _title = self.selection()
        return summary.slug if summary else ""

    def _apply_icons(self) -> None:
        self._search_icon.refresh()
