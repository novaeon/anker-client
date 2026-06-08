# anker_client/ui/search_widget.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QScrollArea, QGridLayout, QLabel
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from anker_client.core.scraper import livewire_search
from anker_client.ui.game_card import GameCard

_CARD_W = 160
_CARD_GAP = 12


class SearchWorker(QThread):
    results_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, session, query: str):
        super().__init__()
        self._session = session
        self._query = query

    def run(self) -> None:
        try:
            results = livewire_search(self._session, self._query)
            self.results_ready.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class SearchWidget(QWidget):
    game_selected = pyqtSignal(dict)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._search_worker = None
        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._do_search)
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
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(_CARD_GAP)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.grid_scroll.setWidget(self.grid_container)
        layout.addWidget(self.grid_scroll)

    # ------------------------------------------------------------------
    # Resize: reflow grid columns to fill available width
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._cards:
            QTimer.singleShot(0, self._reflow_grid)

    def _reflow_grid(self) -> None:
        vp_w = self.grid_scroll.viewport().width()
        cols = max(1, (vp_w - 8 + _CARD_GAP) // (_CARD_W + _CARD_GAP))
        if cols == self._current_cols:
            return
        self._current_cols = cols
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for i, card in enumerate(self._cards):
            self.grid_layout.addWidget(card, i // cols, i % cols)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_text_changed(self, text: str) -> None:
        if len(text) >= 2:
            self._debounce_timer.start(500)
        else:
            self._debounce_timer.stop()

    def _do_search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        self.status_label.setText("Searching...")
        self._clear_results()
        self._search_worker = SearchWorker(self.session, query)
        self._search_worker.results_ready.connect(self._show_results)
        self._search_worker.error.connect(self._show_error)
        self._search_worker.start()

    def _clear_results(self) -> None:
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for card in self._cards:
            card.deleteLater()
        self._cards = []
        self._current_cols = 0

    def _show_results(self, results: list) -> None:
        self._clear_results()
        if not results:
            self.status_label.setText("No results found.")
            return
        self.status_label.setText(f"{len(results)} result(s)")
        for game in results:
            card = GameCard(game)
            card.clicked.connect(self.game_selected)
            self._cards.append(card)
        QTimer.singleShot(0, self._reflow_grid)

    def _show_error(self, msg: str) -> None:
        self.status_label.setText(f"Error: {msg}")
