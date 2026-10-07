"""Store → Search results.

``set_query(q)`` runs two requests in parallel:

* local — ``catalog.search`` (instant, ranked) — shown first;
* server — ``client.search(q, page=N)`` (fuzzy, paginated) — appended,
  de-duplicated by slug; ``near_end`` fetches the next server page.

A new query bumps a generation counter and cancels both in-flight handles, so
results of superseded queries are never shown. If the server fails but local
results exist, they stay visible and the footer offers Retry; with nothing at
all the panel shows the error with Retry. Server results are fed to
``catalog.upsert``. ``refresh_local`` re-runs the local part (after a catalog
sync) and appends anything new.

After every server page the view re-checks whether the user is still near the
end (or the results don't fill the viewport) and loads the next page: a page
whose games were all already shown by the local search adds no rows, so no
scroll event would ever ask for more.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QPoint, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMenu, QVBoxLayout, QWidget

from anker_client.core.models import GameSummary, ListingPage
from anker_client.core.tasks import TaskHandle
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.widgets.common import button, hbox, label
from anker_client.ui.widgets.game_common import no_token, tag_icon
from anker_client.ui.widgets.store_cards import StoreEnv, show_card_menu
from anker_client.ui.widgets.store_common import ResultsPanel

log = logging.getLogger(__name__)

LOCAL_LIMIT = 120


class SearchView(QWidget):
    clear_requested = pyqtSignal()
    browse_requested = pyqtSignal()

    def __init__(self, env: StoreEnv, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._env = env
        self._query = ""
        self._gen = 0
        self._local_handle: TaskHandle[Any] | None = None
        self._server_handle: TaskHandle[Any] | None = None
        self._local_done = False
        self._server_page = 0
        self._server_has_next = False
        self._server_loading = False
        self._server_error: BaseException | None = None
        self.last_menu: QMenu | None = None

        self.title = label("", "title")
        self.count = label("", "muted")
        self.clear_button = button("Clear search", "close", variant="ghost", on_click=self.clear_requested.emit)
        tag_icon(self.clear_button, "close")
        self.panel = ResultsPanel(env.loader, loading_text="Searching…")
        self.panel.retry_clicked.connect(self.retry)
        self.panel.footer.retry_clicked.connect(self.retry)
        self.panel.empty_action_clicked.connect(self.browse_requested)
        self.panel.grid.near_end.connect(self._on_near_end)
        self.panel.grid.item_activated.connect(self._open)
        self.panel.grid.context_requested.connect(self._menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addLayout(hbox(self.title, self.count, None, self.clear_button, spacing=10))
        layout.addWidget(self.panel, 1)
        env.states.changed.connect(lambda slugs: env.states.apply_to(self.panel.grid, slugs))

    # --- public ------------------------------------------------------------------------
    @property
    def query(self) -> str:
        return self._query

    @property
    def server_page(self) -> int:
        return self._server_page

    def set_query(self, query: str, *, force: bool = False) -> None:
        query = " ".join(query.split())
        if query == self._query and not force:
            return
        self.cancel()
        self._gen += 1
        self._query = query
        self._local_done = False
        self._server_page = 0
        self._server_has_next = False
        self._server_error = None
        self.panel.grid.set_items([])
        self.panel.footer.clear()
        self.title.setText(f"Results for “{query}”" if query else "")
        self.count.setText("")
        if not query:
            return
        self.panel.show_loading("Searching…")
        self._run_local(prepend=True)
        self._fetch_server(1)

    def refresh_local(self) -> None:
        if self._query:
            self._run_local(prepend=False)

    def retry(self) -> None:
        if not self._query:
            return
        if self.panel.grid.grid_model.rowCount() == 0:
            self.set_query(self._query, force=True)
            return
        self._server_error = None
        self._fetch_server(self._server_page + 1)

    def cancel(self) -> None:
        for handle in (self._local_handle, self._server_handle):
            if handle is not None:
                handle.cancel()
        self._local_handle = None
        self._server_handle = None
        self._server_loading = False

    # --- requests ------------------------------------------------------------------------
    def _run_local(self, *, prepend: bool) -> None:
        gen = self._gen
        search = no_token(self._env.ctx.catalog.search, self._query, limit=LOCAL_LIMIT)
        self._local_handle = run_async(
            self, self._env.ctx.runner, search,
            on_result=lambda games: self._on_local(gen, games, prepend),
            on_error=lambda exc: self._on_local_error(gen, exc),
        )

    def _fetch_server(self, page: int) -> None:
        gen = self._gen
        self._server_loading = True
        self._server_handle = run_async(
            self, self._env.ctx.runner, self._env.ctx.client.search, self._query, page=page,
            on_result=lambda result: self._on_server(gen, page, result),
            on_error=lambda exc: self._on_server_error(gen, exc),
        )
        self._update_view()

    def _on_near_end(self) -> None:
        if self.panel.is_near_end():
            self._load_more()

    def _load_more(self) -> None:
        if self._server_loading or not self._server_has_next or self._server_error is not None:
            return
        self._fetch_server(self._server_page + 1)

    def _on_local(self, gen: int, games: list[GameSummary], prepend: bool) -> None:
        if gen != self._gen:
            return
        self._local_handle = None
        self._local_done = True
        items = self._env.items(self._env.visible(games))
        model = self.panel.grid.grid_model
        if prepend and model.rowCount():
            keys = {i.key for i in items}
            model.set_items(items + [i for i in model.items() if i.key not in keys])
        elif prepend:
            self.panel.grid.set_items(items)
        else:
            model.append_items(items)
        self._update_view()

    def _on_local_error(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        log.info("Local search failed: %s", exc)
        self._local_handle = None
        self._local_done = True
        self._update_view()

    def _on_server(self, gen: int, page: int, result: ListingPage) -> None:
        if gen != self._gen:
            return
        self._server_handle = None
        self._server_loading = False
        self._server_page = page
        self._server_has_next = bool(result.has_next)
        self._env.remember(list(result.games))
        self.panel.grid.append_items(self._env.items(self._env.visible(result.games)))
        self._update_view()
        if self._server_has_next:
            QTimer.singleShot(0, self._fill_viewport)

    def _fill_viewport(self) -> None:
        if not self._query:
            return
        if self.panel.grid.grid_model.rowCount() == 0:
            self._load_more()  # everything so far was filtered out (NSFW): keep looking
        elif self.isVisible():
            self._on_near_end()

    def _on_server_error(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        log.info("Server search failed: %s", exc)
        self._server_handle = None
        self._server_loading = False
        self._server_error = exc
        self._update_view()

    # --- presentation ----------------------------------------------------------------------
    def _update_view(self) -> None:
        if not self._query:
            return
        count = self.panel.grid.grid_model.rowCount()
        server_done = self._server_page >= 1 and not self._server_has_next and not self._server_loading
        if count:
            self.panel.show_grid()
        elif self._local_done and self._server_error is not None:
            self.panel.show_error(error_text(self._server_error), title="Search failed")
        elif self._local_done and server_done:
            self.panel.show_empty(
                "search", f"No results for “{self._query}”",
                "Check the spelling, or browse the store by genre.", "Browse the store",
            )
        else:
            self.panel.show_loading("Searching…")
        footer = self.panel.footer
        if self._server_loading and count:
            footer.show_loading("Searching AnkerGames…" if self._server_page == 0 else "Loading more…")
        elif self._server_error is not None and count:
            footer.show_error("Couldn't reach AnkerGames — showing saved results.")
        elif server_done and count:
            self.panel.show_end(f"End of results · {count} games")
        else:
            footer.clear()
        more = "+" if self._server_has_next else ""
        self.count.setText(f"{count}{more} result{'s' if count != 1 or more else ''}" if count else "")

    def _open(self, slug: str) -> None:
        item = self.panel.grid.grid_model.item(slug)
        summary = item.payload if item is not None and isinstance(item.payload, GameSummary) else None
        self._env.nav.show_game(slug, summary)

    def _menu(self, slug: str, pos: QPoint) -> None:
        self.last_menu = show_card_menu(self.panel.grid, slug, pos, self._env)
