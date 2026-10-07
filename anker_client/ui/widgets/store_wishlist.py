"""Store → Wishlist: grid of wishlisted games (``catalog.wishlist()``), newest first as stored.

Reloads whenever ``WishlistChanged`` arrives (cheap local query; a pure removal
drops just those cards, keeping the scroll position); while hidden it
only marks itself dirty and reloads on the next ``ensure_loaded``. Empty state:
"Your wishlist is empty" + "Browse the store".
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QPoint, pyqtSignal
from PyQt6.QtWidgets import QMenu, QVBoxLayout, QWidget

from anker_client.core.models import GameSummary
from anker_client.core.tasks import TaskHandle
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.widgets.common import hbox, label
from anker_client.ui.widgets.game_common import no_token
from anker_client.ui.widgets.store_cards import StoreEnv, show_card_menu
from anker_client.ui.widgets.store_common import ResultsPanel

log = logging.getLogger(__name__)


class WishlistView(QWidget):
    browse_requested = pyqtSignal()

    def __init__(self, env: StoreEnv, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._env = env
        self._dirty = True
        self._gen = 0
        self._handle: TaskHandle[Any] | None = None
        self.last_menu: QMenu | None = None

        self.count = label("", "muted")
        self.panel = ResultsPanel(env.loader, loading_text="Loading your wishlist…")
        self.panel.footer.hide()
        self.panel.retry_clicked.connect(self.reload)
        self.panel.empty_action_clicked.connect(self.browse_requested)
        self.panel.grid.item_activated.connect(self._open)
        self.panel.grid.context_requested.connect(self._menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.heading = label("Saved for later", "heading")
        layout.addLayout(hbox(self.heading, self.count, None, spacing=10))
        layout.addWidget(self.panel, 1)
        env.states.changed.connect(lambda slugs: env.states.apply_to(self.panel.grid, slugs))

    def ensure_loaded(self, *, force: bool = False) -> None:
        if force or self._dirty:
            self.reload()

    def invalidate(self) -> None:
        self._dirty = True

    def on_wishlist_changed(self) -> None:
        if self.isVisible():
            self.reload()
        else:
            self._dirty = True

    def reload(self) -> None:
        self.cancel()
        self._gen += 1
        gen = self._gen
        self._dirty = False
        if self.panel.grid.grid_model.rowCount() == 0:
            self.panel.show_loading()
        self._handle = run_async(
            self, self._env.ctx.runner, no_token(self._env.ctx.catalog.wishlist),
            on_result=lambda games: self._on_loaded(gen, games),
            on_error=lambda exc: self._on_error(gen, exc),
        )

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _on_loaded(self, gen: int, games: list[GameSummary]) -> None:
        if gen != self._gen:
            return
        self._handle = None
        model = self.panel.grid.grid_model
        items = self._env.items(games)
        new_keys = [i.key for i in items]
        old_keys = [i.key for i in model.items()]
        kept = set(new_keys)
        if new_keys != old_keys and [k for k in old_keys if k in kept] == new_keys:
            # Only removals: drop those cards in place so the grid doesn't jump back to the top.
            for key in old_keys:
                if key not in kept:
                    model.remove_key(key)
            old_keys = new_keys
        if new_keys != old_keys:
            self.panel.grid.set_items(items)
        else:
            for item in items:
                model.replace_item(item)
        count = len(items)
        self.count.setText(f"{count} game{'s' if count != 1 else ''}" if count else "")
        self.heading.setVisible(bool(count))
        if count:
            self.panel.show_grid()
        else:
            self.panel.show_empty(
                "heart", "Your wishlist is empty",
                "Right-click any game, or use “Add to wishlist” on its page, to save it for later.",
                "Browse the store",
            )

    def _on_error(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._handle = None
        self._dirty = True
        self.panel.show_error(error_text(exc), title="Couldn't load your wishlist")

    def _open(self, slug: str) -> None:
        item = self.panel.grid.grid_model.item(slug)
        summary = item.payload if item is not None and isinstance(item.payload, GameSummary) else None
        self._env.nav.show_game(slug, summary)

    def _menu(self, slug: str, pos: QPoint) -> None:
        self.last_menu = show_card_menu(self.panel.grid, slug, pos, self._env)
