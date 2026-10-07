"""Store → Discover: a vertical page of horizontal rows.

Rows are the site's home sections ("Trending Games", "Upcoming Games",
"Latest Games", collections…) followed by "Top games", then a "Browse by
genre" chip cloud. Rows whose section has a Browse equivalent get "See all"
(see :func:`section_sort`). Results are cached for ``CACHE_SECONDS``; a stale
cache is refreshed silently (current rows stay visible). If only one of
``home_sections``/``top_games`` fails, the other is still shown.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QPoint, pyqtSignal
from PyQt6.QtWidgets import QMenu, QStackedWidget, QVBoxLayout, QWidget

from anker_client.core.errors import OperationCancelled
from anker_client.core.models import GameSummary, Genre, HomeSection, SortOrder
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.widgets.common import EmptyState, LoadingOverlay, button, label
from anker_client.ui.widgets.discover_row import DiscoverRow, LatchingScrollArea, WheelLatch
from anker_client.ui.widgets.game_common import ChipFlow
from anker_client.ui.widgets.store_cards import StoreEnv, show_card_menu

log = logging.getLogger(__name__)

TOP_GAMES_TITLE = "Top games"


def section_sort(title: str) -> SortOrder | None:
    """Browse order equivalent to a home section, or None when there is none."""
    text = title.casefold()
    if re.search(r"\b(latest|new|newest|recent(ly)?)\b", text):
        return SortOrder.NEWEST
    if re.search(r"\b(trending|popular|most viewed)\b", text):
        return SortOrder.MOST_VIEWED
    if re.search(r"\b(liked|favou?rites?)\b", text):
        return SortOrder.MOST_LIKED
    if re.search(r"\b(top|rated|best)\b", text):
        return SortOrder.TOP_RATED
    return None


@dataclass(slots=True)
class DiscoverData:
    sections: list[HomeSection] = field(default_factory=list)
    top_games: list[GameSummary] = field(default_factory=list)


class DiscoverView(QWidget):
    see_all_requested = pyqtSignal(object)  # SortOrder
    genre_requested = pyqtSignal(str)  # genre slug

    CACHE_SECONDS = 600.0

    def __init__(self, env: StoreEnv, parent: QWidget | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        self._env = env
        self._clock = clock
        self._loaded_at: float | None = None
        self._handle: TaskHandle[Any] | None = None
        self._rows: list[DiscoverRow] = []
        self._row_pool: dict[str, DiscoverRow] = {}
        self._genres: list[Genre] = []
        self.latch = WheelLatch()
        self.last_menu: QMenu | None = None

        self.loading = LoadingOverlay("Loading the store…")
        self._error_content: tuple[str, str, str, str] = ("warning", "Couldn't load the store", "", "Retry")
        self.error = EmptyState(*self._error_content)
        self.error.action_clicked.connect(lambda: self.ensure_loaded(force=True))

        self._content = QWidget()
        self._content.setProperty("role", "transparent")
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(28)
        self.genre_heading = label("Browse by genre", "title")
        self.genre_flow = ChipFlow(spacing=8)
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 4, 12, 24)
        content_layout.setSpacing(28)
        content_layout.addLayout(self._rows_layout)
        genre_box = QVBoxLayout()
        genre_box.setSpacing(10)
        genre_box.addWidget(self.genre_heading)
        genre_box.addWidget(self.genre_flow)
        content_layout.addLayout(genre_box)
        content_layout.addStretch(1)
        self.scroll = LatchingScrollArea(self.latch)
        self.scroll.setWidget(self._content)
        self._set_genre_section_visible(False)

        self._stack = QStackedWidget(self)
        for page in (self.loading, self.scroll, self.error):
            self._stack.addWidget(page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        env.states.changed.connect(self._apply_states)

    # --- public ------------------------------------------------------------------------
    @property
    def rows(self) -> list[DiscoverRow]:
        return list(self._rows)

    @property
    def state(self) -> str:
        current = self._stack.currentWidget()
        return {self.loading: "loading", self.scroll: "content", self.error: "error"}.get(current, "")

    def is_stale(self) -> bool:
        return self._loaded_at is None or self._clock() - self._loaded_at >= self.CACHE_SECONDS

    def ensure_loaded(self, *, force: bool = False) -> None:
        """Load (or silently refresh) when never loaded, stale, or ``force``."""
        if self._handle is not None and not force:
            return
        if not force and not self.is_stale():
            return
        self.cancel()
        if not self._rows:
            self._stack.setCurrentWidget(self.loading)
        client = self._env.ctx.client
        self._handle = run_async(self, self._env.ctx.runner, _fetch_discover, client,
                                 on_result=self._on_loaded, on_error=self._on_error)

    def invalidate(self) -> None:
        self._loaded_at = None

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def refresh_theme(self) -> None:
        """Re-render the error icon (a pixmap tinted with the old palette)."""
        self.error.set_content(*self._error_content)

    def set_genres(self, genres: list[Genre]) -> None:
        self._genres = list(genres)
        self.genre_flow.clear()
        show_nsfw = self._env.show_nsfw()
        for genre in self._genres:
            if genre.slug.casefold() == "nsfw" and not show_nsfw:
                continue
            chip = button(genre.name, variant="chip")
            chip.clicked.connect(lambda _c=False, slug=genre.slug: self.genre_requested.emit(slug))
            self.genre_flow.add(chip)
        self._set_genre_section_visible(bool(self._genres) and bool(self._rows))

    # --- loading -----------------------------------------------------------------------
    def _on_loaded(self, data: DiscoverData) -> None:
        self._handle = None
        self._loaded_at = self._clock()
        self._build_rows(data)
        if self._rows:
            self._stack.setCurrentWidget(self.scroll)
        else:
            self._show_error("store", "Nothing to show right now",
                             "The store didn't return any games. Try again in a moment.")
        self._set_genre_section_visible(bool(self._genres) and bool(self._rows))

    def _on_error(self, exc: BaseException) -> None:
        self._handle = None
        log.info("Discover failed: %s", exc)
        if self._rows:  # silent refresh failed; keep what we have and retry next activation
            return
        self._show_error("warning", "Couldn't load the store", error_text(exc))

    def _show_error(self, icon_name: str, title: str, message: str) -> None:
        self._error_content = (icon_name, title, message, "Retry")
        self.error.set_content(*self._error_content)
        self._stack.setCurrentWidget(self.error)

    def _build_rows(self, data: DiscoverData) -> None:
        # Rows are reused (by title) and hidden rather than deleted: CoverGridView keeps a
        # lambda connected to the shared ImageLoader that would touch a deleted model.
        sections = [(s.title, s.games) for s in data.sections]
        if data.top_games:
            sections.append((TOP_GAMES_TITLE, data.top_games))
        seen: list[GameSummary] = []
        rows: list[DiscoverRow] = []
        for title, games in sections:
            visible = self._env.visible(games)
            if not visible or any(r.title == title for r in rows):
                continue
            seen.extend(visible)
            row = self._row_pool.get(title) or self._create_row(title)
            row.set_items(self._env.items(visible))
            rows.append(row)
        for row in self._row_pool.values():
            self._rows_layout.removeWidget(row)
            row.hide()
        for row in rows:
            self._rows_layout.addWidget(row)
            row.show()
        self._rows = rows
        self._env.remember(seen)

    def _create_row(self, title: str) -> DiscoverRow:
        sort = section_sort(title)
        row = DiscoverRow(title, self._env.loader, latch=self.latch, see_all=sort is not None)
        row.item_activated.connect(lambda slug, r=row: self._open(r, slug))
        row.context_requested.connect(lambda slug, pos, r=row: self._menu(r, slug, pos))
        if sort is not None:
            row.see_all_clicked.connect(lambda s=sort: self.see_all_requested.emit(s))
        self._row_pool[title] = row
        return row

    def _open(self, row: DiscoverRow, slug: str) -> None:
        item = row.view.grid_model.item(slug)
        summary = item.payload if item is not None and isinstance(item.payload, GameSummary) else None
        self._env.nav.show_game(slug, summary)

    def _menu(self, row: DiscoverRow, slug: str, pos: QPoint) -> None:
        self.last_menu = show_card_menu(row.view, slug, pos, self._env)

    def _apply_states(self, slugs: frozenset[str]) -> None:
        for row in self._rows:
            self._env.states.apply_to(row.view, slugs)

    def _set_genre_section_visible(self, visible: bool) -> None:
        self.genre_heading.setVisible(visible)
        self.genre_flow.setVisible(visible)


def _fetch_discover(client: Any, *, token: CancelToken) -> DiscoverData:
    """Home sections + top games; partial success is fine, total failure re-raises."""
    data = DiscoverData()
    first_error: BaseException | None = None
    try:
        data.sections = [s for s in client.home_sections(token=token) if s.games]
    except OperationCancelled:
        raise
    except Exception as exc:
        log.info("Home sections failed: %s", exc)
        first_error = exc
    token.raise_if_cancelled()
    try:
        data.top_games = list(client.top_games(token=token))
    except OperationCancelled:
        raise
    except Exception as exc:
        log.info("Top games failed: %s", exc)
        first_error = first_error or exc
    if not data.sections and not data.top_games and first_error is not None:
        raise first_error
    return data
