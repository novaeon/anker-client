"""Building blocks shared by the Store views.

* :class:`ListFooter` — the strip under a paged grid: "Loading more…" with a
  spinner, end-of-list text, or an error with a Retry link.
* :class:`ResultsPanel` — one ``CoverGridView`` wrapped in the four view
  states every listing needs: loading, grid (+ footer), empty, error (+ Retry).
  The end-of-list line is only shown while the grid actually scrolls (a short
  result set already shows its count in the view header, and the line would
  otherwise float far below the last card). ``refresh_theme`` re-renders the
  empty/error icons, which bake palette colours into pixmaps.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.common import EmptyState, LoadingOverlay, Spinner, button, hbox, label
from anker_client.ui.widgets.cover_grid import CoverGridView


class ListFooter(QWidget):
    """Paging status under a grid."""

    retry_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spinner = Spinner(16)
        self.text = label("", "muted")
        self.retry_button = button("Retry", variant="link", on_click=self.retry_clicked.emit)
        self.setLayout(hbox(None, self.spinner, self.text, self.retry_button, None, spacing=8, margins=(0, 6, 0, 6)))
        self.setFixedHeight(36)
        self._mode = ""
        self._end_visible = True
        self.clear()

    @property
    def mode(self) -> str:
        return self._mode

    def show_loading(self, text: str = "Loading more…") -> None:
        self._set("loading", text, spinner=True)

    def show_end(self, text: str) -> None:
        self._set("end", text)

    def show_error(self, text: str) -> None:
        self._set("error", text, retry=True)

    def clear(self) -> None:
        self._set("", "")

    def set_end_visible(self, visible: bool) -> None:
        """Show/hide the end-of-list line (other modes are always visible)."""
        self._end_visible = visible
        self.text.setVisible(bool(self.text.text()) and (self._mode != "end" or visible))

    def _set(self, mode: str, text: str, *, spinner: bool = False, retry: bool = False) -> None:
        self._mode = mode
        self.text.setText(text)
        self.text.setProperty("role", "error" if mode == "error" else "muted")
        style = self.text.style()
        if style is not None:
            style.unpolish(self.text)
            style.polish(self.text)
        self.spinner.setVisible(spinner)
        self.retry_button.setVisible(retry)
        # Keep the strip's height while hidden content changes so the grid doesn't jump.
        self.set_end_visible(self._end_visible)


class ResultsPanel(QWidget):
    """Loading / grid / empty / error states around one cover grid."""

    retry_clicked = pyqtSignal()
    empty_action_clicked = pyqtSignal()

    LOADING, GRID, EMPTY, ERROR = "loading", "grid", "empty", "error"

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None, *,
                 loading_text: str = "Loading games…") -> None:
        super().__init__(parent)
        self.grid = CoverGridView(loader)
        self.footer = ListFooter()
        self.loading = LoadingOverlay(loading_text)
        self.empty = EmptyState("search", "Nothing here")
        self.empty.action_clicked.connect(self.empty_action_clicked)
        self.error = EmptyState("warning", "Couldn't load games", "", "Retry")
        self.error.action_clicked.connect(self.retry_clicked)

        grid_page = QWidget()
        grid_layout = QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(0)
        grid_layout.addWidget(self.grid, 1)
        grid_layout.addWidget(self.footer)

        self._stack = QStackedWidget(self)
        self._pages = {self.LOADING: self.loading, self.GRID: grid_page, self.EMPTY: self.empty, self.ERROR: self.error}
        for page in self._pages.values():
            self._stack.addWidget(page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self._state = ""
        self._empty_content: tuple[str, str, str, str] = ("search", "Nothing here", "", "")
        self._error_content: tuple[str, str, str, str] = ("warning", "Couldn't load games", "", "Retry")
        self.grid.verticalScrollBar().rangeChanged.connect(self._sync_end_line)
        self.show_loading()

    @property
    def state(self) -> str:
        return self._state

    def _show(self, state: str) -> None:
        self._state = state
        self._stack.setCurrentWidget(self._pages[state])

    def show_loading(self, text: str = "") -> None:
        if text:
            self.loading.set_text(text)
        self._show(self.LOADING)

    def show_grid(self) -> None:
        self._show(self.GRID)

    def show_empty(self, icon_name: str, title: str, message: str = "", action_text: str = "") -> None:
        self._empty_content = (icon_name, title, message, action_text)
        self.empty.set_content(*self._empty_content)
        self._show(self.EMPTY)

    def show_error(self, message: str, title: str = "Couldn't load games") -> None:
        self._error_content = ("warning", title, message, "Retry")
        self.error.set_content(*self._error_content)
        self._show(self.ERROR)

    def show_end(self, text: str) -> None:
        """End-of-list footer, visible only while the grid scrolls (see module docstring)."""
        self.footer.show_end(text)
        self._sync_end_line()

    def refresh_theme(self) -> None:
        self.empty.set_content(*self._empty_content)
        self.error.set_content(*self._error_content)

    def _sync_end_line(self, *_args: object) -> None:
        bar = self.grid.verticalScrollBar()
        self.footer.set_end_visible(bar.maximum() > bar.minimum())

    def is_near_end(self) -> bool:
        """True when the grid is scrolled within ~1.5 rows of its end (after a fresh item layout).

        ``CoverGridView.near_end`` can fire before the view has laid out new items
        (scroll range still 0), which would load pages nobody scrolled to.
        """
        if self._state != self.GRID:
            return False
        grid = self.grid
        grid.doItemsLayout()
        bar = grid.verticalScrollBar()
        threshold = int(grid.cell_size().height() * 1.5)
        return bar.maximum() - bar.value() <= threshold
