"""Store page: discover rows, browse with sort/genre, search, wishlist.

Layout: a header (title "Store", tabs "Discover" | "Browse" | "Wishlist", and a
transient "Search" tab with a close button while a query is active, refresh
button) over a stack of views implemented in ``ui/widgets/store_*.py``:

* Discover (``store_discover``) — home sections + "Top games" as horizontal
  rows ("See all" → Browse with the matching sort), "Browse by genre" chips.
  Cached ~10 minutes; refreshed silently on activation when stale.
* Browse (``store_browse``) — sort combo (persisted as ``settings.store_sort``),
  genre chips, infinite scroll.
* Wishlist (``store_wishlist``) — wishlisted games; empty state → Browse.
* Search (``store_search``) — ``set_query(q)``: instant local results merged
  with paginated server results; ``set_query("")`` closes it and returns to
  the previous tab.

Card badges/progress/hearts come from one shared ``CardStates`` index updated
live from the bridge (downloads, library, launches, wishlist). Settings
changes apply live: ``show_nsfw`` re-filters everything, ``store_sort``
re-sorts Browse. ``set_genre`` opens Browse filtered by a genre slug or name.
A theme switch re-tints icons and the empty/error artwork. The transient
Search tab never changes the header height (the page below doesn't jump).
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget

from anker_client.core.models import Genre, SortOrder
from anker_client.core.tasks import TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.navigator import Navigator
from anker_client.ui.widgets.common import hbox, icon_button, label
from anker_client.ui.widgets.game_common import ThemeWatcher, retint_icons, tag_icon
from anker_client.ui.widgets.store_browse import BrowseView
from anker_client.ui.widgets.store_cards import CardStates, StoreEnv
from anker_client.ui.widgets.store_discover import DiscoverView
from anker_client.ui.widgets.store_search import SearchView
from anker_client.ui.widgets.store_wishlist import WishlistView

log = logging.getLogger(__name__)

DISCOVER, BROWSE, WISHLIST, SEARCH = "discover", "browse", "wishlist", "search"
_TAB_ORDER = (DISCOVER, BROWSE, WISHLIST)


def _sort_setting(ctx: AppContext) -> SortOrder:
    try:
        return SortOrder(ctx.settings.get().store_sort)
    except ValueError:
        return SortOrder.NEWEST


class StorePage(QWidget):
    def __init__(self, ctx: AppContext, bridge: QtEventBridge, nav: Navigator, loader: ImageLoader,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self._ctx = ctx
        self._nav = nav
        self._genres: list[Genre] = []
        self._genres_handle: TaskHandle[Any] | None = None
        self._last_tab = DISCOVER

        self.states = CardStates(ctx, bridge, self)
        env = StoreEnv(ctx, nav, loader, self.states, self)
        self.discover = DiscoverView(env)
        self.browse = BrowseView(env, sort=_sort_setting(ctx))
        self.wishlist = WishlistView(env)
        self.search = SearchView(env)
        self._views: dict[str, QWidget] = {
            DISCOVER: self.discover, BROWSE: self.browse, WISHLIST: self.wishlist, SEARCH: self.search,
        }

        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        for name in ("Discover", "Browse", "Wishlist"):
            self.tabs.addTab(name)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.refresh_button = tag_icon(icon_button("refresh", "Refresh (F5)", on_click=self.refresh), "refresh")

        self._stack = QStackedWidget()
        for view in self._views.values():
            self._stack.addWidget(view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 0)
        layout.setSpacing(14)
        layout.addLayout(hbox(label("Store", "display"), self.tabs, None, self.refresh_button,
                              spacing=24))
        layout.addWidget(self._stack, 1)

        self.discover.see_all_requested.connect(self._see_all)
        self.discover.genre_requested.connect(self.set_genre)
        self.browse.sort_changed.connect(self._persist_sort)
        self.search.clear_requested.connect(lambda: self.set_query(""))
        self.search.browse_requested.connect(self._open_browse)
        self.wishlist.browse_requested.connect(self._open_browse)
        # Bound methods only: PyQt disconnects them when this page is destroyed, whereas a
        # lambda on the (longer-lived) bridge would call into a deleted widget.
        bridge.wishlist_changed.connect(self._on_wishlist_changed)
        bridge.settings_changed.connect(self._on_settings_changed)
        bridge.catalog_updated.connect(self._on_catalog_updated)
        self._theme_watcher = ThemeWatcher(self, self._on_theme_changed)

    # --- contract ------------------------------------------------------------------------
    def set_query(self, query: str) -> None:
        """Show search results for ``query`` ("" returns to browsing)."""
        query = " ".join((query or "").split())
        if not query:
            self._leave_search()
            self._select(self._last_tab)
            return
        self._ensure_search_tab(query)
        self.search.set_query(query)
        self._select(SEARCH)

    def set_genre(self, genre: str) -> None:
        self._leave_search()
        self.browse.select_genre(genre, reload=False)
        self._select(BROWSE)
        self.browse.ensure_loaded()

    def on_activated(self) -> None:
        self.states.refresh()
        self._ensure_genres()
        self._ensure_current()

    def on_deactivated(self) -> None:
        """Nothing to pause: views only react to bridge signals and user input."""

    def shutdown(self) -> None:
        self.states.cancel()
        for view in (self.discover, self.browse, self.wishlist, self.search):
            view.cancel()
        if self._genres_handle is not None:
            self._genres_handle.cancel()
            self._genres_handle = None

    # --- extras ----------------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload the visible view (F5)."""
        self.states.refresh()
        self._ensure_genres()
        current = self.current_view()
        if current == DISCOVER:
            self.discover.ensure_loaded(force=True)
        elif current == BROWSE:
            self.browse.ensure_loaded(force=True)
        elif current == WISHLIST:
            self.wishlist.ensure_loaded(force=True)
        else:
            self.search.set_query(self.search.query, force=True)

    def current_view(self) -> str:
        widget = self._stack.currentWidget()
        return next((name for name, view in self._views.items() if view is widget), DISCOVER)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self.states.loaded:
            self.states.refresh()
        self._ensure_genres()
        self._ensure_current()

    # --- tabs ----------------------------------------------------------------------------------
    def _search_tab_index(self) -> int:
        return self.tabs.count() - 1 if self.tabs.count() > len(_TAB_ORDER) else -1

    def _ensure_search_tab(self, query: str) -> None:
        text = f"Search: {query}" if len(query) <= 24 else f"Search: {query[:23]}…"
        index = self._search_tab_index()
        if index < 0:
            index = self.tabs.addTab(text)
            close = QToolButton()
            close.setProperty("variant", "icon")
            tag_icon(close, "close")
            close.setIcon(icons.icon("close"))
            close.setIconSize(QSize(12, 12))
            # Compact so the tab bar (and the page below it) keeps its height when the tab appears.
            close.setFixedSize(18, 18)
            close.setStyleSheet("QToolButton { padding: 2px; }")
            close.setToolTip("Clear search")
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.clicked.connect(lambda _c=False: self.set_query(""))
            self.tabs.setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
        else:
            self.tabs.setTabText(index, text)
        self.tabs.setTabToolTip(index, f"Results for “{query}”")

    def _leave_search(self) -> None:
        self.search.set_query("")
        index = self._search_tab_index()
        if index >= 0:
            self.tabs.blockSignals(True)  # removing the current tab must not switch views behind our back
            self.tabs.removeTab(index)
            self.tabs.blockSignals(False)

    def _select(self, name: str) -> None:
        index = self._search_tab_index() if name == SEARCH else _TAB_ORDER.index(name)
        self._stack.setCurrentWidget(self._views[name])
        if name != SEARCH:
            self._last_tab = name
        if self.tabs.currentIndex() != index:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(index)
            self.tabs.blockSignals(False)
        self._ensure_current()

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        name = SEARCH if index == self._search_tab_index() else _TAB_ORDER[index]
        self._select(name)

    def _ensure_current(self) -> None:
        if not self.isVisible():
            return
        current = self.current_view()
        if current == DISCOVER:
            self.discover.ensure_loaded()
        elif current == BROWSE:
            self.browse.ensure_loaded()
        elif current == WISHLIST:
            self.wishlist.ensure_loaded()

    def _open_browse(self) -> None:
        self._leave_search()
        self._select(BROWSE)

    def _see_all(self, sort: SortOrder) -> None:
        self._leave_search()
        self.browse.set_sort(sort, reload=False)
        self.browse.select_genre("", reload=False)
        self._select(BROWSE)
        self.browse.ensure_loaded()

    # --- live updates ---------------------------------------------------------------------------
    def _on_wishlist_changed(self, _slug: str, _wishlisted: bool) -> None:
        self.wishlist.on_wishlist_changed()

    def _on_catalog_updated(self, _event: object) -> None:
        self.search.refresh_local()

    def _on_theme_changed(self) -> None:
        """Icons and empty/error pixmaps bake palette colours in; rebuild them."""
        retint_icons(self)
        self.discover.refresh_theme()
        for panel in (self.browse.panel, self.search.panel, self.wishlist.panel):
            panel.refresh_theme()

    # --- data ---------------------------------------------------------------------------------
    def _ensure_genres(self) -> None:
        if self._genres or self._genres_handle is not None:
            return

        def loaded(genres: list[Genre]) -> None:
            self._genres_handle = None
            self._genres = list(genres)
            self.browse.set_genres(self._genres)
            self.discover.set_genres(self._genres)

        def failed(exc: BaseException) -> None:
            self._genres_handle = None
            log.info("Genres unavailable: %s", exc)

        self._genres_handle = run_async(self, self._ctx.runner, self._ctx.client.genres,
                                        on_result=loaded, on_error=failed)

    def _persist_sort(self, sort: SortOrder) -> None:
        try:
            self._ctx.settings.update(store_sort=sort.value)
        except (OSError, KeyError, ValueError):
            log.warning("Could not save the store sort order", exc_info=True)

    def _on_settings_changed(self, keys: frozenset[str]) -> None:
        if "show_nsfw" in keys:
            self.discover.invalidate()
            self.discover.set_genres(self._genres)
            self.browse.refresh_filters()
            self.wishlist.invalidate()
            if self.search.query:
                self.search.set_query(self.search.query, force=True)
            self._ensure_current()
        if "store_sort" in keys:
            sort = _sort_setting(self._ctx)
            if sort != self.browse.sort:
                self.browse.set_sort(sort, reload=self.current_view() == BROWSE)
                if self.current_view() != BROWSE:
                    self.browse.invalidate()
