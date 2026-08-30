from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.scraper import livewire_search
from anker_client.core.tasks import BackgroundTask, get_task_runner
from anker_client.ui.game_card import GameCard

_CARD_W = 160
_CARD_GAP = 12


def _search_games(
    cancel_event: threading.Event,
    session,
    query: str,
) -> list[dict]:
    return livewire_search(
        session,
        query,
        should_cancel=cancel_event.is_set,
    )


class SearchWidget(QWidget):
    game_selected = pyqtSignal(dict)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._active_task: BackgroundTask | None = None
        self._pending_search: tuple[int, str] | None = None
        self._generation = 0
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(400)
        self._debounce_timer.timeout.connect(self._do_search)
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(50)
        self._reflow_timer.timeout.connect(self._reflow_grid)
        self._cards: list[GameCard] = []
        self._current_cols = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search games...")
        self.search_input.setMinimumHeight(36)
        self.search_input.textChanged.connect(self._on_text_changed)
        self.search_input.returnPressed.connect(self._do_search)
        layout.addWidget(self.search_input)

        self.status_label = QLabel("Type to search...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(_CARD_GAP)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.grid_scroll.setWidget(self.grid_container)
        layout.addWidget(self.grid_scroll)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._cards:
            self._reflow_timer.start()

    def _reflow_grid(self) -> None:
        viewport_width = self.grid_scroll.viewport().width()
        columns = max(
            1,
            (viewport_width - 8 + _CARD_GAP) // (_CARD_W + _CARD_GAP),
        )
        if columns == self._current_cols:
            return
        self._current_cols = columns
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for index, card in enumerate(self._cards):
            self.grid_layout.addWidget(
                card,
                index // columns,
                index % columns,
            )

    def _on_text_changed(self, text: str) -> None:
        if len(text.strip()) >= 2:
            self._debounce_timer.start()
            return

        self._debounce_timer.stop()
        self._generation += 1
        self._pending_search = None
        if self._active_task:
            self._active_task.cancel()
        self._clear_results()
        self.status_label.setText("Type at least 2 characters to search...")

    def _do_search(self) -> None:
        self._debounce_timer.stop()
        query = self.search_input.text().strip()
        if not query:
            return

        self._generation += 1
        self._pending_search = (self._generation, query)
        if self._active_task:
            # Keep at most one expensive paginated search in flight.  The old
            # task stops between pages and the latest query starts immediately
            # afterwards.
            self._active_task.cancel()
            self.status_label.setText("Updating search...")
            return
        self._start_pending_search()

    def _start_pending_search(self) -> None:
        if not self._pending_search:
            return
        generation, query = self._pending_search
        self._pending_search = None
        self.status_label.setText("Searching...")

        task = get_task_runner().submit(_search_games, self.session, query)
        self._active_task = task
        task.signals.result.connect(
            lambda results, generation=generation: self._show_results(
                generation, results
            )
        )
        task.signals.error.connect(
            lambda message, generation=generation: self._show_error(
                generation, message
            )
        )
        task.signals.finished.connect(lambda task=task: self._task_finished(task))

    def _task_finished(self, task: BackgroundTask) -> None:
        if self._active_task is not task:
            return
        self._active_task = None
        if self._pending_search:
            self._start_pending_search()

    def _clear_results(self) -> None:
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for card in self._cards:
            card.deleteLater()
        self._cards = []
        self._current_cols = 0

    def _show_results(self, generation: int, results: list[dict]) -> None:
        if generation != self._generation:
            return
        self._clear_results()
        if not results:
            self.status_label.setText("No results found.")
            return
        self.status_label.setText(
            f"{len(results)} result{'s' if len(results) != 1 else ''}"
        )
        for game in results:
            card = GameCard(game)
            card.clicked.connect(self.game_selected)
            self._cards.append(card)
        self._reflow_timer.start(0)

    def _show_error(self, generation: int, message: str) -> None:
        if generation == self._generation:
            self.status_label.setText(f"Search failed: {message}")

    def shutdown(self) -> None:
        self._debounce_timer.stop()
        self._pending_search = None
        if self._active_task:
            self._active_task.cancel()
