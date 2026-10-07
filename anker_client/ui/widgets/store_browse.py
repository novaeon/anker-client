"""Store → Browse: sort combo, genre chips and an infinitely scrolling cover grid.

* Sort combo lists every ``SortOrder`` by its label; changes emit
  ``sort_changed`` (the page persists ``settings.store_sort``) and re-query.
* Genre chips (checkable, exclusive): "All" first, then ``client.genres()``,
  plus "VR" when the site list lacks it; "NSFW" only when ``show_nsfw``.
  ``select_genre`` accepts a slug or a display name ("Open World").
* Paging: page 1 replaces the grid; ``near_end`` loads the next page while
  ``has_next``; the footer shows "Loading more…", an end-of-list line, or an
  error with Retry. After a failed page only Retry tries again (scrolling must
  not re-send the failing request on every wheel tick). When a page doesn't
  fill the viewport the next one is requested automatically. Every query bumps
  a generation counter and cancels the in-flight request, so late pages of an
  old query are dropped.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QPoint, QTimer, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QComboBox, QMenu, QPushButton, QVBoxLayout, QWidget

from anker_client.core.models import GameSummary, Genre, ListingPage, SortOrder
from anker_client.core.tasks import TaskHandle
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.widgets.common import button, hbox, label
from anker_client.ui.widgets.game_common import ChipFlow, genre_slug
from anker_client.ui.widgets.store_cards import StoreEnv, show_card_menu
from anker_client.ui.widgets.store_common import ResultsPanel

log = logging.getLogger(__name__)

VR_GENRE = Genre("vr", "VR")


class BrowseView(QWidget):
    sort_changed = pyqtSignal(object)  # SortOrder chosen by the user

    def __init__(self, env: StoreEnv, parent: QWidget | None = None, *, sort: SortOrder = SortOrder.NEWEST) -> None:
        super().__init__(parent)
        self._env = env
        self._sort = sort
        self._genre = ""  # slug; "" = all games
        self._genres: list[Genre] = []
        self._extra_genre: Genre | None = None  # a requested genre the site list doesn't have
        self._page = 0
        self._has_next = False
        self._loading = False
        self._page_failed = False  # the last "load more" failed; wait for Retry
        self._gen = 0
        self._handle: TaskHandle[Any] | None = None
        self._loaded_key: tuple[SortOrder, str] | None = None
        self.last_menu: QMenu | None = None

        self.sort_combo = QComboBox()
        for order in SortOrder:
            self.sort_combo.addItem(order.label, order.value)
        self.sort_combo.setCurrentIndex(list(SortOrder).index(sort))
        self.sort_combo.setMinimumWidth(170)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_index)
        self.status = label("", "muted")

        self.chips = ChipFlow(spacing=8)
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)
        self._chip_group.buttonClicked.connect(self._on_chip_clicked)

        self.panel = ResultsPanel(env.loader)
        self.panel.retry_clicked.connect(self.reload)
        self.panel.footer.retry_clicked.connect(self._retry_more)
        self.panel.grid.near_end.connect(self._on_near_end)
        self.panel.grid.item_activated.connect(self._open)
        self.panel.grid.context_requested.connect(self._menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addLayout(hbox(label("Sort by", "muted"), self.sort_combo, None, self.status, spacing=10))
        layout.addWidget(self.chips)
        layout.addWidget(self.panel, 1)
        self._rebuild_chips()
        env.states.changed.connect(lambda slugs: env.states.apply_to(self.panel.grid, slugs))

    # --- public ------------------------------------------------------------------------
    @property
    def sort(self) -> SortOrder:
        return self._sort

    @property
    def genre(self) -> str:
        return self._genre

    @property
    def page(self) -> int:
        return self._page

    @property
    def has_next(self) -> bool:
        return self._has_next

    @property
    def is_loading(self) -> bool:
        return self._loading

    def chip_buttons(self) -> list[QPushButton]:
        return [b for b in self._chip_group.buttons() if isinstance(b, QPushButton)]

    def set_genres(self, genres: list[Genre]) -> None:
        self._genres = list(genres)
        if self._genre:
            self._genre = self._resolve(self._genre)
        self._rebuild_chips()

    def refresh_filters(self) -> None:
        """Re-apply the NSFW preference to the chips (and re-query)."""
        if not self._env.show_nsfw() and self._genre == "nsfw":
            self._genre = ""
        self._rebuild_chips()
        self._loaded_key = None

    def select_genre(self, value: str, *, reload: bool = True) -> None:
        """Filter by a genre slug or display name ("" = all games)."""
        slug = self._resolve(value)
        if slug == self._genre and self._loaded_key == (self._sort, slug):
            return
        self._genre = slug
        self._rebuild_chips()
        if reload:
            self.reload()

    def set_sort(self, sort: SortOrder, *, reload: bool = True) -> None:
        if sort == self._sort:
            return
        self._sort = sort
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(list(SortOrder).index(sort))
        self.sort_combo.blockSignals(False)
        if reload and self._loaded_key is not None:
            self.reload()

    def ensure_loaded(self, *, force: bool = False) -> None:
        if force or self._loaded_key != (self._sort, self._genre):
            self.reload()

    def invalidate(self) -> None:
        self._loaded_key = None

    def reload(self) -> None:
        self.cancel()
        self._gen += 1
        self._page = 0
        self._has_next = False
        self._page_failed = False
        self._loaded_key = (self._sort, self._genre)
        self.panel.grid.set_items([])
        self.panel.footer.clear()
        self.panel.show_loading("Loading games…")
        self.status.setText("")
        self._fetch(1)

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._loading = False

    # --- chips ---------------------------------------------------------------------------
    def _chip_genres(self) -> list[Genre]:
        show_nsfw = self._env.show_nsfw()
        genres = [g for g in self._genres if show_nsfw or g.slug.casefold() != "nsfw"]
        if not any(g.slug.casefold() == VR_GENRE.slug for g in genres):
            genres.append(VR_GENRE)
        known = {g.slug for g in genres}
        if self._genre and self._genre not in known:
            name = self._extra_genre.name if self._extra_genre and self._extra_genre.slug == self._genre \
                else self._genre.replace("-", " ").title()
            genres.append(Genre(self._genre, name))
        return [Genre("", "All"), *genres]

    def _rebuild_chips(self) -> None:
        for chip in self._chip_group.buttons():
            self._chip_group.removeButton(chip)
        self.chips.clear()
        for genre in self._chip_genres():
            chip = button(genre.name, variant="chip")
            chip.setCheckable(True)
            chip.setProperty("slug", genre.slug)
            chip.setChecked(genre.slug == self._genre)
            self._chip_group.addButton(chip)
            self.chips.add(chip)

    def _resolve(self, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        folded = text.casefold()
        for genre in [*self._genres, VR_GENRE]:
            if folded in (genre.slug.casefold(), genre.name.casefold()) or genre_slug(genre.name) == genre_slug(text):
                return genre.slug
        slug = genre_slug(text)
        if slug != folded:  # remember a nicer display name for the extra chip
            self._extra_genre = Genre(slug, text)
        return slug

    def _on_chip_clicked(self, chip: Any) -> None:
        slug = str(chip.property("slug") or "")
        if slug != self._genre:
            self._genre = slug
            self.reload()

    def _on_sort_index(self, index: int) -> None:
        sort = list(SortOrder)[index]
        if sort == self._sort:
            return
        self._sort = sort
        self.sort_changed.emit(sort)
        self.reload()

    # --- paging ----------------------------------------------------------------------------
    def _fetch(self, page: int) -> None:
        gen = self._gen
        self._loading = True
        if page > 1:
            self.panel.footer.show_loading("Loading more…")
        self._handle = run_async(
            self, self._env.ctx.runner, self._env.ctx.client.browse,
            page=page, sort=self._sort, genre=self._genre or None,
            on_result=lambda result: self._on_page(gen, page, result),
            on_error=lambda exc: self._on_error(gen, page, exc),
        )

    def _on_near_end(self) -> None:
        if not self._loading and self._has_next and not self._page_failed and self.panel.is_near_end():
            self._load_more()

    def _retry_more(self) -> None:
        self._page_failed = False
        self._load_more()

    def _load_more(self) -> None:
        if self._loading or not self._has_next or self._page_failed or self.panel.state != ResultsPanel.GRID:
            return
        self._fetch(self._page + 1)

    def _on_page(self, gen: int, page: int, result: ListingPage) -> None:
        if gen != self._gen:
            return
        self._handle = None
        self._loading = False
        self._page_failed = False
        self._page = page
        self._has_next = bool(result.has_next)
        games = self._env.visible(result.games)
        self._env.remember(list(result.games))
        grid = self.panel.grid
        if page == 1:
            grid.set_items(self._env.items(games))
        else:
            grid.append_items(self._env.items(games))
        count = grid.grid_model.rowCount()
        if count == 0 and self._has_next:
            self._fetch(page + 1)  # everything on this page was filtered out
            return
        if count == 0:
            self._show_empty()
            return
        self.panel.show_grid()
        if self._has_next:
            self.panel.footer.clear()
        else:
            self.panel.show_end(f"That's everything · {count} games")
        self._update_status()
        QTimer.singleShot(0, self._fill_viewport)

    def _on_error(self, gen: int, page: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._handle = None
        self._loading = False
        log.info("Browse page %s failed: %s", page, exc)
        if page == 1 or self.panel.grid.grid_model.rowCount() == 0:
            self.panel.show_error(error_text(exc))
            self.status.setText("")
        else:
            self._page_failed = True
            self.panel.footer.show_error("Couldn't load more games.")

    def _show_empty(self) -> None:
        name = next((g.name for g in self._chip_genres() if g.slug == self._genre), "")
        title = f"No {name} games found" if self._genre and name else "No games found"
        self.panel.show_empty("store", title, "Try another genre or sort order.")
        self.status.setText("")

    def _update_status(self) -> None:
        count = self.panel.grid.grid_model.rowCount()
        noun = "game" if count == 1 else "games"
        self.status.setText(f"Showing {count} {noun}" if self._has_next else f"{count} {noun}")

    def _fill_viewport(self) -> None:
        if not self.isVisible() or self._loading or not self._has_next:
            return
        self._on_near_end()

    # --- cards -------------------------------------------------------------------------------
    def _open(self, slug: str) -> None:
        item = self.panel.grid.grid_model.item(slug)
        summary = item.payload if item is not None and isinstance(item.payload, GameSummary) else None
        self._env.nav.show_game(slug, summary)

    def _menu(self, slug: str, pos: QPoint) -> None:
        self.last_menu = show_card_menu(self.panel.grid, slug, pos, self._env)
