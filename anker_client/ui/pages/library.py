"""Library page: installed games grid/list + detail side panel + management actions.

Layout (docs/ARCHITECTURE.md §UI "Library page")
* Header: "Library" + game count + "N updates" notice chip; "Check for updates"
  and the "Add games" menu (Import archive…, Import existing folders…,
  Rescan libraries).
* Toolbar: filter box, filter combo (All, Favorites, Updates available, Needs
  setup, Unmanaged, Hidden — with counts), sort combo (Title, Recently played,
  Playtime, Recently installed, Size) and the grid/list toggle. View and sort
  persist in ``settings.library_view`` / ``settings.library_sort``; the filter
  text and filter combo persist in the database ``meta`` table (there are no
  settings keys for them). ``set_filter(key)`` (used by the shell's "N
  updates" notice) overrides the saved filter, even before the first load.
* Body: ``CoverGridView`` (badges Running/Update/Needs setup/Unmanaged,
  favourite heart, hidden games dimmed) or a sortable table, plus the
  :class:`LibraryDetailPanel` on the right. Double-click or Enter plays,
  Delete asks to uninstall. Right-click opens a context menu with the same
  actions as the panel. Play handles ``ExecutableNotSetError`` by calling
  ``nav.choose_executable``; Update/Repair fetch fresh details and call
  ``nav.request_install`` (the pending patch option when there is one);
  Uninstall is confirmed (naming the game, folder and size), re-checked
  (still installed, not running) and runs on a worker while the panel shows a
  busy state.
* Per-game work (play, stop, update, repair, check, prerequisites, shortcuts,
  uninstall) is tracked per ``<action>:<install id>``: a second click while it
  runs is ignored and the panel shows the busy state again when the game is
  re-selected. Favourite/Hide apply optimistically: writes for one game run
  one at a time, the last click wins, unsaved values survive reloads and a
  failed save reloads the real state.
* States: loading, empty ("Your library is empty" + Browse the store + Import
  existing games), no results (+ Clear filters), error (+ Try again).

Data flow: ``library.games()`` and ``launcher.running()`` are read on a worker
(the real service may hold its scan lock) every time ``library_changed`` /
``game_installed`` / ``game_uninstalled`` / ``updates_found`` fire (coalesced
over 120 ms; only the newest load is applied). Running state also follows
``game_launched`` / ``game_exited`` directly; launches/exits seen while a load
is in flight are re-applied on top of its (possibly older) snapshot. Grid and
table are updated in place when the visible set is unchanged (no
scroll/selection resets). ``refresh()`` (F5) rescans the library folders.
Every service call goes through ``run_async``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from PyQt6.QtCore import QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QContextMenuEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.errors import ExecutableNotSetError
from anker_client.core.formatting import format_bytes, pluralize
from anker_client.core.models import GameDetails, GameUpdate, InstalledGame
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.game_dialogs import GamePropertiesDialog, ImportArchiveDialog, ImportFoldersDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.navigator import Navigator
from anker_client.ui.widgets.common import EmptyState, LoadingOverlay, button, label, repolish
from anker_client.ui.widgets.cover_grid import KEY_ROLE, CoverGridView
from anker_client.ui.widgets.library_common import (
    Connections,
    IconTinter,
    SearchIcon,
    confirm,
    tokenless,
    widen_empty_state,
)
from anker_client.ui.widgets.library_detail import LibraryDetailPanel
from anker_client.ui.widgets.library_model import (
    FILTER_KEYS,
    FILTERS,
    INSTALL_ID_ROLE,
    SORT_KEYS,
    SORT_TO_COLUMN,
    SORTS,
    THUMB_SIZE,
    LibrarySortProxy,
    LibraryTableModel,
    cover_item,
    filter_counts,
    filter_games,
    needs_setup,
    sort_games,
)

log = logging.getLogger(__name__)

_DETAILS_MAX_AGE = timedelta(minutes=10)

META_FILTER = "ui.library.filter"
META_QUERY = "ui.library.query"

_EMPTY_MESSAGE = (
    "Games you install from the store show up here. Already have games on this PC? "
    "Import their folders or an archive you downloaded."
)

_NO_RESULTS: dict[str, tuple[str, str]] = {
    "all": ("No games to show", "Every game in your library is hidden."),
    "favorites": ("No favorites yet", "Use the heart next to Play to keep the games you love at hand."),
    "updates": ("Everything is up to date", "None of your games has an update waiting."),
    "needs_setup": ("Nothing needs setup", "Every game knows which program starts it."),
    "unmanaged": ("No unmanaged games", "AnkerClient manages every game in your library folders."),
    "hidden": ("No hidden games", "Hidden games stay installed and are listed here."),
}

_STACK_LOADING, _STACK_EMPTY, _STACK_NO_RESULTS, _STACK_ERROR, _STACK_GRID, _STACK_TABLE = range(6)


class _LibraryTable(QTableView):
    """Table view whose context menu reports the install id under the cursor."""

    def __init__(self, page: LibraryPage) -> None:
        super().__init__()
        self._page = page

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:  # noqa: N802
        if event is None:
            return
        index = self.indexAt(event.pos())
        if index.isValid():
            self.selectRow(index.row())
            install_id = index.data(INSTALL_ID_ROLE)
            if install_id:
                self._page._show_context_menu(install_id, event.globalPos())


class LibraryPage(QWidget):
    RELOAD_DELAY_MS = 120
    PERSIST_DELAY_MS = 600

    def __init__(self, ctx: AppContext, bridge: QtEventBridge, nav: Navigator, loader: ImageLoader,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self._ctx = ctx
        self._bridge = bridge
        self._nav = nav
        self._loader = loader
        self._tint = IconTinter(self)

        settings = ctx.settings.get()
        self._view = settings.library_view if settings.library_view in ("grid", "list") else "grid"
        self._sort = settings.library_sort if settings.library_sort in SORT_KEYS else "title"
        self._show_hidden = settings.show_hidden_games
        self._filter = "all"
        self._query = ""
        self._filters_explicit = False  # set_filter() before the first load beats the saved filter

        self._games: dict[str, InstalledGame] = {}
        self._visible: list[str] = []
        self._running: set[str] = set()
        self._running_since_load: dict[str, bool] = {}  # launch/exit events newer than the load request
        self._selected = ""
        self._pending_select = ""
        self._loaded = False
        self._load_seq = 0
        self._load_handle: TaskHandle[Any] | None = None
        self._size_handle: TaskHandle[Any] | None = None
        self._size_for = ""
        self._size_attempted: set[str] = set()
        self._tasks: dict[str, TaskHandle[Any]] = {}
        self._busy_text: dict[str, str] = {}  # "<action>:<install id>" → panel caption while it runs
        self._flag_wanted: dict[str, bool] = {}  # "<favorite|hidden>:<install id>" → value not saved yet
        self._flag_writes: set[str] = set()  # keys with a write in flight
        self._syncing = False
        self._dialog: QDialog | None = None
        self._error_message = ""
        self._shut_down = False
        self._connections = Connections()

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(self.RELOAD_DELAY_MS)
        self._reload_timer.timeout.connect(self._reload)
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(self.PERSIST_DELAY_MS)
        self._persist_timer.timeout.connect(self._persist_filters)

        self._build()
        self._connect_bridge()
        self._tint.on_theme_changed(self._refresh_theme)
        self._show_stack(_STACK_LOADING)
        self._reload()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        root.addLayout(self._build_header())
        self._toolbar = QWidget()
        toolbar = self._build_toolbar()
        toolbar.setContentsMargins(0, 0, 0, 0)
        self._toolbar.setLayout(toolbar)
        root.addWidget(self._toolbar)

        body = QHBoxLayout()
        body.setSpacing(20)
        self._stack = QStackedWidget()
        self._stack.addWidget(LoadingOverlay("Loading your library…"))
        self._stack.addWidget(self._build_empty_state())
        self._no_results = EmptyState("search", "No games match", "", "Clear filters")
        widen_empty_state(self._no_results)
        self._no_results.action_clicked.connect(self._clear_filters)
        self._stack.addWidget(self._no_results)
        self._error_state = EmptyState("error", "Couldn't load your library", "", "Try again")
        widen_empty_state(self._error_state)
        self._error_state.action_clicked.connect(self._retry_load)
        self._stack.addWidget(self._error_state)
        self._stack.addWidget(self._build_grid())
        self._stack.addWidget(self._build_table())
        body.addWidget(self._stack, 1)

        self._panel = LibraryDetailPanel(self._loader)
        self._panel.action_requested.connect(self._on_panel_action)
        self._panel.hide()
        body.addWidget(self._panel)
        root.addLayout(body, 1)

    def _build_header(self) -> QHBoxLayout:
        self._title = label("Library", "display")
        self._count = label("", "muted")
        self._updates_chip = button("", variant="notice", on_click=self._show_updates)
        self._updates_chip.setProperty("tone", "warning")
        self._updates_chip.hide()
        self._check_button = button("Check for updates", on_click=self._check_all_updates)
        self._tint.set(self._check_button, "refresh", size=16)
        self._add_button = button("Add games", variant="primary")
        self._tint.set(self._add_button, "plus", tint="on_accent", size=16)
        menu = QMenu(self._add_button)
        self._act_import_archive = menu.addAction("Import archive…", self._open_import_archive)
        self._act_import_folders = menu.addAction("Import existing folders…", self._open_import_folders)
        menu.addSeparator()
        self._act_rescan = menu.addAction("Rescan libraries", self._rescan)
        self._add_menu = menu
        self._add_button.setMenu(menu)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._count, 0, Qt.AlignmentFlag.AlignBottom)
        row.addSpacing(4)
        row.addWidget(self._updates_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        row.addWidget(self._check_button)
        row.addWidget(self._add_button)
        return row

    def _build_toolbar(self) -> QHBoxLayout:
        self._search = QLineEdit()
        self._search.setProperty("role", "search")
        self._search.setPlaceholderText("Filter by title")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(240)
        self._search_icon = SearchIcon(self._search)
        self._search.textChanged.connect(self._on_query_changed)

        self._filter_combo = QComboBox()
        for key, text in FILTERS:
            self._filter_combo.addItem(text, key)
        self._filter_combo.setMinimumWidth(175)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)

        self._sort_combo = QComboBox()
        for key, text in SORTS:
            self._sort_combo.addItem(text, key)
        self._sort_combo.setMinimumWidth(150)
        self._sort_combo.setCurrentIndex(max(0, self._sort_combo.findData(self._sort)))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self._grid_toggle = self._view_button("grid", "Grid view")
        self._list_toggle = self._view_button("list", "List view")
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._grid_toggle)
        group.addButton(self._list_toggle)
        (self._grid_toggle if self._view == "grid" else self._list_toggle).setChecked(True)
        self._grid_toggle.toggled.connect(lambda on: on and self._set_view("grid"))
        self._list_toggle.toggled.connect(lambda on: on and self._set_view("list"))

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._search)
        row.addWidget(self._filter_combo)
        row.addSpacing(6)
        row.addWidget(label("Sort by", "muted"))
        row.addWidget(self._sort_combo)
        row.addStretch(1)
        row.addWidget(self._grid_toggle)
        row.addWidget(self._list_toggle)
        return row

    def _view_button(self, icon_name: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setCheckable(True)
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(34, 34)
        self._tint.set(btn, icon_name, tint="muted", size=18)
        return btn

    def _build_empty_state(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty = EmptyState("library", "Your library is empty", _EMPTY_MESSAGE, "Browse the store")
        self._empty.action_clicked.connect(self._nav.show_store)
        widen_empty_state(self._empty)
        import_button = button("Import existing games", variant="link", on_click=self._open_import_folders)
        layout.addWidget(self._empty)
        layout.addWidget(import_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._empty_import_button = import_button
        return holder

    def _build_grid(self) -> CoverGridView:
        self._grid = CoverGridView(self._loader)
        self._grid.item_activated.connect(self._on_view_activated)
        self._grid.doubleClicked.connect(self._on_view_double_clicked)
        self._grid.context_requested.connect(self._show_context_menu)
        self._grid.selectionModel().currentChanged.connect(self._on_grid_current_changed)
        self._add_play_shortcuts(self._grid)
        return self._grid

    def _build_table(self) -> QTableView:
        self._table_model = LibraryTableModel(self._loader, self)
        self._proxy = LibrarySortProxy(self)
        self._proxy.setSourceModel(self._table_model)
        table = _LibraryTable(self)
        table.setModel(self._proxy)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setIconSize(THUMB_SIZE)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(46)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = table.horizontalHeader()
        header.setSortIndicatorClearable(True)
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(70)
        for col, width in enumerate((0, 110, 130, 100, 110, 140)):
            if col:
                table.setColumnWidth(col, width)
        self._table = table
        table.setSortingEnabled(True)
        self._apply_table_sort_indicator()
        table.doubleClicked.connect(self._on_view_double_clicked)
        table.selectionModel().currentRowChanged.connect(self._on_table_current_changed)
        self._add_play_shortcuts(table)
        return table

    def _add_play_shortcuts(self, view: QWidget) -> None:
        """Enter plays the selected game, Delete asks to uninstall it (while the view has focus)."""
        bindings = ((Qt.Key.Key_Return, self._play), (Qt.Key.Key_Enter, self._play),
                    (Qt.Key.Key_Delete, self._uninstall))
        for key, handler in bindings:
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda h=handler: self._selected and h(self._selected))

    def _connect_bridge(self) -> None:
        bridge, connect = self._bridge, self._connections.connect
        connect(bridge.library_changed, self._on_library_changed)
        connect(bridge.game_installed, self._on_game_installed)
        connect(bridge.game_uninstalled, self._on_game_uninstalled)
        connect(bridge.game_launched, self._on_game_launched)
        connect(bridge.game_exited, self._on_game_exited)
        connect(bridge.updates_found, self._on_updates_found)
        connect(bridge.settings_changed, self._on_settings_changed)

    # ------------------------------------------------------------------ Page API
    def select(self, install_id: str) -> None:
        """Show ``install_id`` (clearing filters that hide it) and open its details."""
        if not install_id:
            return
        if not self._loaded or install_id not in self._games:
            self._pending_select = install_id
            self._schedule_reload()
            return
        self._pending_select = ""
        if install_id not in self._visible:
            game = self._games[install_id]
            self._set_filters(query="", filter_key="hidden" if game.hidden and not self._show_hidden else "all")
        self._set_selected(install_id, scroll=True)

    def set_filter(self, filter_key: str) -> None:
        """Show only ``filter_key`` games (all|favorites|updates|needs_setup|unmanaged|hidden); clears the text."""
        self._filters_explicit = True
        self._set_filters(query="", filter_key=filter_key if filter_key in FILTER_KEYS else "all")

    def refresh(self) -> None:
        """F5: rescan the library folders (picks up folders copied in by hand)."""
        self._rescan(quiet=True)

    def on_activated(self) -> None:
        self._schedule_reload()  # also refreshes relative times ("5 minutes ago")

    def on_deactivated(self) -> None:
        if self._persist_timer.isActive():  # flush a pending filter save now
            self._persist_timer.stop()
            self._persist_filters()

    def shutdown(self) -> None:
        """Stop listening and cancel background reads; the page starts no new work afterwards."""
        if self._persist_timer.isActive():
            self._persist_timer.stop()
            self._persist_filters()
        self._shut_down = True
        self._connections.disconnect_all()
        self._reload_timer.stop()
        for handle in (self._load_handle, self._size_handle, *self._tasks.values()):
            if handle is not None:
                handle.cancel()
        self._tasks.clear()
        self._busy_text.clear()

    # ------------------------------------------------------------------ loading
    def _schedule_reload(self) -> None:
        if not self._shut_down:
            self._reload_timer.start()

    def _retry_load(self) -> None:
        self._show_stack(_STACK_LOADING)
        self._reload()

    def _reload(self) -> None:
        self._reload_timer.stop()
        if self._shut_down:
            return
        self._load_seq += 1
        seq = self._load_seq
        self._running_since_load.clear()
        if self._load_handle is not None:
            self._load_handle.cancel()
        ctx = self._ctx
        read_prefs = not self._loaded

        def work(*, token: CancelToken) -> tuple[list[InstalledGame], set[str], tuple[str, str] | None]:
            games = ctx.library.games(include_hidden=True)
            token.raise_if_cancelled()
            running = set(ctx.launcher.running())
            prefs = None
            if read_prefs:
                try:
                    prefs = (ctx.db.get_meta(META_FILTER) or "", ctx.db.get_meta(META_QUERY) or "")
                except Exception:  # preferences are a convenience; never fail the load for them
                    log.debug("Could not read library filter preferences", exc_info=True)
            return games, running, prefs

        self._load_handle = run_async(
            self, ctx.runner, work,
            on_result=lambda result, seq=seq: self._on_loaded(seq, result),
            on_error=lambda exc, seq=seq: self._on_load_failed(seq, exc),
        )

    def _on_loaded(self, seq: int, result: tuple[list[InstalledGame], set[str], tuple[str, str] | None]) -> None:
        if seq != self._load_seq:
            return
        games, running, prefs = result
        first = not self._loaded
        self._loaded = True
        if first and prefs is not None and not self._filters_explicit:
            saved_filter, saved_query = prefs
            self._set_filters(query=saved_query, filter_key=saved_filter if saved_filter in FILTER_KEYS else "all",
                              refresh=False, persist=False)
        self._games = {g.install_id: g for g in games}
        for key, value in self._flag_wanted.items():  # unsaved favourite/hidden clicks stay visible
            flag, _sep, install_id = key.partition(":")
            if install_id in self._games:
                setattr(self._games[install_id], flag, value)
        running = set(running)
        # The snapshot may predate launches/exits already delivered as events: those win.
        for install_id, is_running in self._running_since_load.items():
            if is_running:
                running.add(install_id)
            else:
                running.discard(install_id)
        self._running = {i for i in running if i in self._games}
        self._size_attempted &= set(self._games)
        self._refresh_view()
        if self._pending_select and self._pending_select in self._games:
            self.select(self._pending_select)

    def _on_load_failed(self, seq: int, exc: BaseException) -> None:
        if seq != self._load_seq:
            return
        log.warning("Loading the library failed: %s", exc)
        if self._loaded and self._games:
            self._nav.toast(f"Couldn't refresh your library: {error_text(exc)}", "error")
            return
        self._error_message = error_text(exc)
        self._error_state.set_content("error", "Couldn't load your library", self._error_message, "Try again")
        self._show_stack(_STACK_ERROR)
        self._panel.hide()
        for widget in (self._toolbar, self._check_button, self._count, self._updates_chip):
            widget.hide()  # nothing to filter or check until the library loads

    # ------------------------------------------------------------------ rendering
    def _visible_games(self) -> list[InstalledGame]:
        filtered = filter_games(self._games.values(), query=self._query, filter_key=self._filter,
                                show_hidden=self._show_hidden)
        return sort_games(filtered, self._sort)

    def _refresh_view(self) -> None:
        if not self._loaded:
            return
        visible = self._visible_games()
        self._visible = [g.install_id for g in visible]
        self._render_counts()

        has_games = bool(self._games)
        self._toolbar.setVisible(has_games)
        self._check_button.setVisible(has_games)
        self._count.setVisible(has_games)
        if not has_games:
            self._show_stack(_STACK_EMPTY)
            self._panel.set_game(None)
            self._panel.hide()
            self._selected = ""
            return
        if not visible:
            self._render_no_results()
            self._show_stack(_STACK_NO_RESULTS)
            self._panel.set_game(None)
            self._panel.hide()
            self._selected = ""
            return

        self._sync_grid(visible)
        self._table_model.set_games(visible, self._running)
        self._show_stack(_STACK_GRID if self._view == "grid" else _STACK_TABLE)
        self._panel.show()
        target = self._selected if self._selected in self._visible else self._visible[0]
        self._set_selected(target, scroll=False, force_panel=True)

    def _sync_grid(self, visible: list[InstalledGame]) -> None:
        items = [cover_item(g, g.install_id in self._running) for g in visible]
        model = self._grid.grid_model
        self._syncing = True
        try:
            if [i.key for i in model.items()] == [i.key for i in items]:
                for item in items:
                    if model.item(item.key) != item:
                        model.replace_item(item)
            else:
                model.set_items(items)
        finally:
            self._syncing = False

    def _render_counts(self) -> None:
        total = len(self._games)
        shown = len(self._visible)
        hidden_total = sum(1 for g in self._games.values() if g.hidden)
        base = total if self._show_hidden or self._filter == "hidden" else total - hidden_total
        if shown == base and self._filter == "all" and not self._query:
            self._count.setText(pluralize(base, "game"))
        else:
            self._count.setText(f"{shown} of {pluralize(base, 'game')}")
        counts = filter_counts(self._games.values(), show_hidden=self._show_hidden)
        for i, (key, text) in enumerate(FILTERS):
            self._filter_combo.setItemText(i, f"{text} ({counts[key]})" if key != "all" else f"{text} ({base})")
        updates = counts["updates"]
        self._updates_chip.setVisible(updates > 0 and self._filter != "updates")
        self._updates_chip.setText(f"{pluralize(updates, 'update')} available")
        self._check_button.setEnabled("check_all" not in self._tasks and any(
            g.slug and g.managed for g in self._games.values()))

    def _render_no_results(self) -> None:
        title, message = _NO_RESULTS.get(self._filter, _NO_RESULTS["all"])
        if self._query:
            title = "No games match"
            filter_name = dict(FILTERS).get(self._filter, "")
            message = (f"No game titles match “{self._query}”." if self._filter == "all"
                       else f"Nothing in {filter_name} matches “{self._query}”.")
        self._no_results.set_content("search", title, message, "Show all games")

    def _show_stack(self, index: int) -> None:
        if self._stack.currentIndex() != index:
            self._stack.setCurrentIndex(index)

    # ------------------------------------------------------------------ selection
    def _set_selected(self, install_id: str, *, scroll: bool, force_panel: bool = False) -> None:
        changed = install_id != self._selected
        self._selected = install_id
        self._select_in_views(install_id, scroll=scroll)
        if changed or force_panel:
            game = self._games.get(install_id)
            self._panel.set_game(game, running=install_id in self._running)
            if game is not None:
                if changed:
                    self._start_size(game)
                self._restore_busy(install_id)

    def _restore_busy(self, install_id: str) -> None:
        """Show work still running for ``install_id`` (the panel forgets it when another game is shown)."""
        for key, text in self._busy_text.items():
            action, _sep, owner = key.partition(":")
            if owner == install_id and not self._panel.is_busy(action):
                self._panel.set_busy(action, True, text)

    def _select_in_views(self, install_id: str, *, scroll: bool) -> None:
        self._syncing = True
        try:
            row = self._grid.grid_model.row_of(install_id)
            if row >= 0:
                index = self._grid.grid_model.index(row)
                if self._grid.currentIndex() != index:
                    self._grid.selectionModel().setCurrentIndex(
                        index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                if scroll:
                    self._grid.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
            source_row = self._table_model.row_of(install_id)
            if source_row >= 0:
                proxy_index = self._proxy.mapFromSource(self._table_model.index(source_row, 0))
                if proxy_index.isValid() and self._table.currentIndex().row() != proxy_index.row():
                    self._table.selectionModel().setCurrentIndex(
                        proxy_index,
                        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
                    )
                if scroll and proxy_index.isValid():
                    self._table.scrollTo(proxy_index, QAbstractItemView.ScrollHint.EnsureVisible)
        finally:
            self._syncing = False

    def _on_grid_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._syncing or not current.isValid():
            return
        key = current.data(KEY_ROLE)
        if key:
            self._set_selected(key, scroll=False)

    def _on_table_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._syncing or not current.isValid():
            return
        install_id = current.data(INSTALL_ID_ROLE)
        if install_id:
            self._set_selected(install_id, scroll=False)

    def _on_view_activated(self, key: str) -> None:
        if key and key != self._selected:
            self._set_selected(key, scroll=False)

    def _on_view_double_clicked(self, index: QModelIndex) -> None:
        install_id = index.data(INSTALL_ID_ROLE) if index.model() is self._proxy else index.data(KEY_ROLE)
        if install_id:
            self._play(install_id)

    # ------------------------------------------------------------------ size
    def _start_size(self, game: InstalledGame) -> None:
        self._cancel_size()
        if game.size_bytes is not None or game.install_id in self._size_attempted or self._shut_down:
            return
        install_id = game.install_id
        self._size_attempted.add(install_id)
        self._size_for = install_id
        self._panel.set_size(None, calculating=True)
        self._size_handle = run_async(
            self, self._ctx.runner, self._ctx.library.compute_size, install_id,
            on_result=lambda size, i=install_id: self._on_size(i, size),
            on_error=lambda exc, i=install_id: self._on_size_failed(i, exc),
        )

    def _cancel_size(self) -> None:
        """Stop measuring the previous game; it is measured again the next time it is selected."""
        if self._size_handle is None:
            return
        self._size_handle.cancel()
        self._size_attempted.discard(self._size_for)
        self._size_handle = None
        self._size_for = ""

    def _size_done(self, install_id: str) -> None:
        if self._size_for == install_id:
            self._size_handle = None
            self._size_for = ""

    def _on_size(self, install_id: str, size: int) -> None:
        self._size_done(install_id)
        game = self._games.get(install_id)
        if game is not None:
            game.size_bytes = size
            if self._sort == "size" or self._view == "list":
                self._refresh_view()
        if self._panel.install_id == install_id:
            self._panel.set_size(size)

    def _on_size_failed(self, install_id: str, exc: BaseException) -> None:
        self._size_done(install_id)
        log.info("Size of %s could not be computed: %s", install_id, exc)
        if self._panel.install_id == install_id:
            self._panel.set_size(None)

    # ------------------------------------------------------------------ filters / sort / view
    def _on_query_changed(self, text: str) -> None:
        self._query = text.strip()
        self._persist_timer.start()
        self._refresh_view()

    def _on_filter_changed(self, index: int) -> None:
        key = self._filter_combo.itemData(index) or "all"
        if key == self._filter:
            return
        self._filter = key
        self._persist_timer.start()
        self._refresh_view()

    def _set_filters(self, *, query: str, filter_key: str, refresh: bool = True, persist: bool = True) -> None:
        self._query = query.strip()
        self._filter = filter_key if filter_key in FILTER_KEYS else "all"
        for widget in (self._search, self._filter_combo):
            widget.blockSignals(True)
        try:
            self._search.setText(query)
            self._filter_combo.setCurrentIndex(max(0, self._filter_combo.findData(self._filter)))
        finally:
            for widget in (self._search, self._filter_combo):
                widget.blockSignals(False)
        if persist:
            self._persist_timer.start()
        if refresh:
            self._refresh_view()

    def _clear_filters(self) -> None:
        self._set_filters(query="", filter_key="all")

    def _show_updates(self) -> None:
        self._set_filters(query="", filter_key="updates")

    def _persist_filters(self) -> None:
        db = self._ctx.db
        values = {META_FILTER: self._filter, META_QUERY: self._query}

        def work(*, token: CancelToken) -> None:  # tiny local writes: not worth abandoning half-way
            for key, value in values.items():
                db.set_meta(key, value)

        try:
            run_async(self, self._ctx.runner, work,
                      on_error=lambda exc: log.debug("Could not save library filters: %s", exc))
        except RuntimeError:  # the task runner already shut down (application exit)
            log.debug("Library filters not saved: background tasks have stopped")

    def _on_sort_changed(self, index: int) -> None:
        key = self._sort_combo.itemData(index) or "title"
        if key == self._sort:
            return
        self._sort = key
        self._apply_table_sort_indicator()
        self._save_setting(library_sort=key)
        self._refresh_view()

    def _apply_table_sort_indicator(self) -> None:
        column, order = SORT_TO_COLUMN.get(self._sort, (-1, Qt.SortOrder.AscendingOrder))
        self._table.horizontalHeader().setSortIndicator(column, order)
        if column < 0:
            self._proxy.sort(-1)

    def _set_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        self._save_setting(library_view=view)
        self._refresh_view()
        if self._selected:
            self._select_in_views(self._selected, scroll=True)

    def _save_setting(self, **changes: Any) -> None:
        run_async(self, self._ctx.runner, tokenless(self._ctx.settings.update, **changes),
                  on_error=lambda exc: log.warning("Could not save %s: %s", sorted(changes), exc))

    # ------------------------------------------------------------------ bridge
    def _on_library_changed(self, _ids: object) -> None:
        self._schedule_reload()

    def _on_game_installed(self, _event: object) -> None:
        self._schedule_reload()

    def _on_game_uninstalled(self, event: Any) -> None:
        self._size_attempted.discard(getattr(event, "install_id", ""))
        self._schedule_reload()

    def _on_game_launched(self, event: Any) -> None:
        install_id = getattr(event, "install_id", "")
        if not install_id:
            return
        self._running_since_load[install_id] = True
        if install_id not in self._running:
            self._running.add(install_id)
            self._refresh_running(install_id)

    def _on_game_exited(self, event: Any) -> None:
        install_id = getattr(event, "install_id", "")
        if install_id:
            self._running_since_load[install_id] = False
        if install_id in self._running:
            self._running.discard(install_id)
            self._refresh_running(install_id)
        self._schedule_reload()  # playtime / last played changed

    def _refresh_running(self, install_id: str) -> None:
        if install_id not in self._games:
            return
        self._refresh_view()
        if self._panel.install_id == install_id:
            self._panel.set_running(install_id in self._running)

    def _on_updates_found(self, _updates: object) -> None:
        self._schedule_reload()

    def _on_settings_changed(self, keys: Any) -> None:
        keys = set(keys or ())
        if not keys & {"show_hidden_games", "library_view", "library_sort", "library_dirs"}:
            return
        settings = self._ctx.settings.get()
        refresh = False
        if settings.show_hidden_games != self._show_hidden:
            self._show_hidden = settings.show_hidden_games
            refresh = True
        if settings.library_sort != self._sort and settings.library_sort in SORT_KEYS:
            self._sort = settings.library_sort
            self._sort_combo.blockSignals(True)
            self._sort_combo.setCurrentIndex(max(0, self._sort_combo.findData(self._sort)))
            self._sort_combo.blockSignals(False)
            self._apply_table_sort_indicator()
            refresh = True
        if settings.library_view != self._view and settings.library_view in ("grid", "list"):
            (self._grid_toggle if settings.library_view == "grid" else self._list_toggle).setChecked(True)
        if "library_dirs" in keys:
            self._schedule_reload()
        if refresh:
            self._refresh_view()

    # ------------------------------------------------------------------ actions
    def _game(self, install_id: str) -> InstalledGame | None:
        return self._games.get(install_id)

    def _on_panel_action(self, action: str) -> None:
        install_id = self._panel.install_id
        if install_id:
            self._run_action(action, install_id)

    def _run_action(self, action: str, install_id: str) -> None:
        handlers = {
            "play": self._play,
            "stop": self._stop,
            "update": self._update,
            "choose_exe": self._nav.choose_executable,
            "properties": self._open_properties,
            "open_folder": self._open_folder,
            "favorite": self._toggle_favorite,
            "hide": self._toggle_hidden,
            "shortcuts": self._create_shortcuts,
            "redist": self._run_redist,
            "check_update": self._check_update,
            "repair": self._repair,
            "store": self._open_store_page,
            "uninstall": self._uninstall,
        }
        handler = handlers.get(action)
        if handler is not None:
            handler(install_id)

    def _set_panel_busy(self, install_id: str, action: str, busy: bool, text: str = "") -> None:
        if self._panel.install_id == install_id:
            self._panel.set_busy(action, busy, text)

    def _run_tracked(
        self,
        action: str,
        install_id: str,
        fn: Callable[..., Any],
        /,
        *args: Any,
        busy_text: str = "",
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Run per-game work once at a time; the panel shows it as busy while it runs.

        Returns False (and starts nothing) when the same action is already running for the game.
        """
        key = f"{action}:{install_id}"
        if key in self._tasks or self._shut_down:
            return False

        def finished() -> None:
            self._tasks.pop(key, None)
            self._busy_text.pop(key, None)
            self._set_panel_busy(install_id, action, False)

        self._busy_text[key] = busy_text
        self._set_panel_busy(install_id, action, True, busy_text)
        self._tasks[key] = run_async(self, self._ctx.runner, fn, *args, on_result=on_result, on_error=on_error,
                                     on_finished=finished, **kwargs)
        return True

    def _play(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None:
            return
        if install_id in self._running:
            self._nav.toast(f"{game.title} is already running.")
            return
        self._run_tracked(
            "play", install_id, tokenless(self._ctx.launcher.launch, install_id),
            on_error=lambda exc: self._on_launch_failed(install_id, game.title, exc),
        )

    def _on_launch_failed(self, install_id: str, title: str, exc: BaseException) -> None:
        if isinstance(exc, ExecutableNotSetError):
            self._nav.choose_executable(install_id)
            return
        self._nav.toast(f"Couldn't start {title}: {error_text(exc)}", "error")

    def _stop(self, install_id: str) -> None:
        game = self._game(install_id)
        title = game.title if game else install_id
        self._run_tracked(
            "stop", install_id, tokenless(self._ctx.launcher.stop, install_id),
            on_error=lambda exc: self._nav.toast(f"Couldn't stop {title}: {error_text(exc)}", "error"),
        )

    def _fetch_details_then(self, install_id: str, busy_action: str, busy_text: str, *, for_update: bool) -> None:
        game = self._game(install_id)
        if game is None or not game.slug:
            return
        ctx = self._ctx
        slug = game.slug

        def work(*, token: CancelToken) -> tuple[GameDetails, GameUpdate | None]:
            # fresh-ish details: the update/repair must offer the site's current download options
            details = ctx.catalog.details(slug, max_age=_DETAILS_MAX_AGE, token=token)
            update = None
            if for_update:
                update = next((u for u in ctx.updates.pending() if u.install_id == install_id), None)
            return details, update

        def done(result: tuple[GameDetails, GameUpdate | None]) -> None:
            details, update = result
            option = None
            if update is not None:
                option = update.patch_option or update.full_option
            self._nav.request_install(details, option or details.primary_option)

        self._run_tracked(
            busy_action, install_id, work, busy_text=busy_text,
            on_result=done,
            on_error=lambda exc: self._nav.toast(f"Couldn't load {game.title} from AnkerGames: {error_text(exc)}",
                                                 "error"),
        )

    def _update(self, install_id: str) -> None:
        self._fetch_details_then(install_id, "update", "Preparing update…", for_update=True)

    def _repair(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is not None and game.managed:  # an unmanaged folder would get a second, separate copy
            self._fetch_details_then(install_id, "repair", "Preparing…", for_update=False)

    def _open_store_page(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is not None and game.slug:
            self._nav.show_game(game.slug)

    def _open_folder(self, install_id: str) -> None:
        run_async(self, self._ctx.runner, tokenless(self._ctx.launcher.open_folder, install_id),
                  on_error=lambda exc: self._nav.toast(f"Couldn't open the folder: {error_text(exc)}", "error"))

    def _toggle_favorite(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is not None:
            self._set_flag(install_id, "favorite", not game.favorite)

    def _toggle_hidden(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None:
            return
        hide = not game.hidden
        self._set_flag(install_id, "hidden", hide)
        if hide and not self._show_hidden:
            self._nav.toast(f"{game.title} is hidden. Find it under the “Hidden” filter.")

    def _set_flag(self, install_id: str, flag: str, value: bool) -> None:
        """Apply a favourite/hidden change locally now and save it in the background.

        Writes for one game and flag run one at a time and the last click wins: two quick
        clicks must not reach the pool's threads in the wrong order.
        """
        game = self._game(install_id)
        if game is None or self._shut_down:
            return
        setattr(game, flag, value)
        self._refresh_view()
        key = f"{flag}:{install_id}"
        self._flag_wanted[key] = value
        if key not in self._flag_writes:
            self._write_flag(key, install_id, flag, value)

    def _write_flag(self, key: str, install_id: str, flag: str, value: bool) -> None:
        setter = self._ctx.library.set_favorite if flag == "favorite" else self._ctx.library.set_hidden

        def failed(exc: BaseException) -> None:
            self._flag_wanted.pop(key, None)
            self._nav.toast(error_text(exc), "error")
            self._schedule_reload()  # drop the optimistic change

        def finished() -> None:
            self._flag_writes.discard(key)
            wanted = self._flag_wanted.get(key)
            if wanted is None or wanted == value or self._shut_down:
                self._flag_wanted.pop(key, None)
            else:
                self._write_flag(key, install_id, flag, wanted)

        self._flag_writes.add(key)
        run_async(self, self._ctx.runner, tokenless(setter, install_id, value), on_error=failed,
                  on_finished=finished)

    def _create_shortcuts(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None:
            return
        if not game.executable:
            self._nav.choose_executable(install_id)
            return
        create = tokenless(self._ctx.shortcuts.create, game.title, game.executable_path,
                           arguments=game.launch_args, desktop=True, start_menu=True)
        self._run_tracked(
            "shortcuts", install_id, create, busy_text="Creating…",
            on_result=lambda _paths: self._nav.toast(
                f"Added {game.title} to the desktop and Start menu.", "success"),
            on_error=lambda exc: self._nav.toast(f"Couldn't create shortcuts: {error_text(exc)}", "error"),
        )

    def _run_redist(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None:
            return

        def done(count: object) -> None:
            n = count if isinstance(count, int) else 0
            if n:
                self._nav.toast(f"Ran {pluralize(n, 'prerequisite installer')} for {game.title}.", "success")
            else:
                self._nav.toast(f"No prerequisite installers were found for {game.title}.")

        self._run_tracked(
            "redist", install_id, self._ctx.launcher.run_redist, install_id, busy_text="Installing prerequisites…",
            on_result=done,
            on_error=lambda exc: self._nav.toast(f"Prerequisites failed: {error_text(exc)}", "error"),
        )

    def _check_update(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None:
            return

        def done(updates: list[GameUpdate]) -> None:
            found = next((u for u in updates or [] if u.install_id == install_id), None)
            if found is not None:
                self._nav.toast(f"Update available for {game.title}: {found.latest_version or 'new version'}.",
                                "warning")
            else:
                self._nav.toast(f"{game.title} is up to date.", "success")

        self._run_tracked(
            "check_update", install_id, self._ctx.updates.check, busy_text="Checking…", install_ids=[install_id],
            on_result=done,
            on_error=lambda exc: self._nav.toast(f"Couldn't check for updates: {error_text(exc)}", "error"),
        )

    def _check_all_updates(self) -> None:
        if "check_all" in self._tasks or self._shut_down:
            return

        def done(updates: list[GameUpdate]) -> None:
            count = len(updates or [])
            if count:
                self._nav.toast(f"{pluralize(count, 'update')} available.", "warning")
            else:
                self._nav.toast("All your games are up to date.", "success")

        def finished() -> None:
            self._tasks.pop("check_all", None)
            self._check_button.setText("Check for updates")
            self._render_counts()

        self._check_button.setEnabled(False)
        self._check_button.setText("Checking…")
        self._tasks["check_all"] = run_async(
            self, self._ctx.runner, self._ctx.updates.check,
            on_result=done,
            on_error=lambda exc: self._nav.toast(f"Couldn't check for updates: {error_text(exc)}", "error"),
            on_finished=finished,
        )

    def _uninstall(self, install_id: str) -> None:
        game = self._game(install_id)
        if game is None or f"uninstall:{install_id}" in self._tasks:
            return
        if install_id in self._running:
            self._nav.toast(f"Close {game.title} before uninstalling it.", "warning")
            return
        size = format_bytes(game.size_bytes) if game.size_bytes is not None else "size unknown"
        if not confirm(
            self,
            title="Uninstall game",
            text=f"Uninstall {game.title}?",
            informative=(f"This permanently deletes the game folder and everything in it ({size}):\n"
                         f"{game.path}\n\nYour playtime is kept."),
            confirm_text=f"Uninstall {game.title}" if len(game.title) <= 28 else "Uninstall",
        ):
            return
        # The confirmation is modal: the game may have been started or removed meanwhile.
        if self._shut_down or install_id not in self._games:
            return
        if install_id in self._running:
            self._nav.toast(f"Close {game.title} before uninstalling it.", "warning")
            return
        self._run_tracked(
            "uninstall", install_id, self._ctx.library.uninstall, install_id, busy_text="Uninstalling…",
            on_result=lambda _r: self._nav.toast(f"{game.title} was uninstalled.", "success"),
            on_error=lambda exc: self._nav.toast(f"Couldn't uninstall {game.title}: {error_text(exc)}", "error"),
        )

    def _rescan(self, *, quiet: bool = False) -> None:
        if "rescan" in self._tasks or self._shut_down:
            return

        def done(games: list[InstalledGame]) -> None:
            if not quiet:
                self._nav.toast(f"Library rescanned · {pluralize(len(games or []), 'game')} found.", "success")

        def failed(exc: BaseException) -> None:
            self._nav.toast(f"Rescan failed: {error_text(exc)}", "error")

        def finished() -> None:
            self._tasks.pop("rescan", None)
            self._act_rescan.setEnabled(True)
            self._act_rescan.setText("Rescan libraries")

        self._act_rescan.setEnabled(False)
        self._act_rescan.setText("Rescanning…")
        self._tasks["rescan"] = run_async(
            self, self._ctx.runner, self._ctx.library.scan,
            on_result=done, on_error=failed, on_finished=finished,
        )

    # ------------------------------------------------------------------ dialogs
    def _open_dialog(self, dialog: QDialog) -> QDialog:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._dialog = dialog
        dialog.open()
        return dialog

    def _open_properties(self, install_id: str) -> None:
        if install_id in self._games:
            self._open_dialog(GamePropertiesDialog(self._ctx, install_id, self))

    def _open_import_archive(self) -> None:
        dialog = ImportArchiveDialog(self._ctx, self)
        dialog.accepted.connect(lambda: self._nav.toast("Import started — follow it on the Downloads page.",
                                                        "success"))
        self._open_dialog(dialog)

    def _open_import_folders(self) -> None:
        dialog = ImportFoldersDialog(self._ctx, self)
        dialog.accepted.connect(lambda d=dialog: d.imported and self._nav.toast(
            f"Imported {pluralize(d.imported, 'game')} into your library.", "success"))
        self._open_dialog(dialog)

    # ------------------------------------------------------------------ context menu
    def _show_context_menu(self, install_id: str, global_pos: QPoint) -> None:
        menu = self._build_context_menu(install_id)
        if menu is not None:
            menu.popup(global_pos)

    def _build_context_menu(self, install_id: str) -> QMenu | None:
        game = self._game(install_id)
        if game is None:
            return None
        if install_id != self._selected:
            self._set_selected(install_id, scroll=False)
        running = install_id in self._running
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        def add(text: str, action: str, icon_name: str = "") -> QAction:
            act = menu.addAction(text)
            if icon_name:
                act.setIcon(icons.icon(icon_name))
            act.setData(action)
            act.setEnabled(f"{action}:{install_id}" not in self._tasks)
            act.triggered.connect(lambda _c=False, a=action: self._run_action(a, install_id))
            return act

        add("Stop" if running else "Play", "stop" if running else "play", "stop" if running else "play")
        if game.update_available and game.slug:
            add(f"Update to {game.latest_version}" if game.latest_version else "Update", "update", "update")
        if needs_setup(game):
            add("Choose program…", "choose_exe", "settings")
        add("Properties…", "properties", "settings")
        add("Open folder", "open_folder", "folder")
        if not needs_setup(game):
            add("Create shortcuts", "shortcuts", "shortcut")
        if game.has_redist and not game.redist_installed:
            add("Install prerequisites", "redist", "package")
        menu.addSeparator()
        add("Remove from favorites" if game.favorite else "Add to favorites", "favorite", "heart_filled")
        add("Unhide game" if game.hidden else "Hide game", "hide", "eye" if game.hidden else "eye_off")
        if game.slug:
            menu.addSeparator()
            add("Store page", "store", "store")
            if game.managed:
                add("Check for update", "check_update", "refresh")
                add("Repair (reinstall)", "repair", "wrench")
        menu.addSeparator()
        uninstall = add("Uninstall…", "uninstall", "trash")
        uninstall.setEnabled(uninstall.isEnabled() and not running)
        return menu

    # ------------------------------------------------------------------ theme
    def _refresh_theme(self) -> None:
        self._search_icon.refresh()
        self._empty.set_content("library", "Your library is empty", _EMPTY_MESSAGE, "Browse the store")
        self._render_no_results()
        self._error_state.set_content("error", "Couldn't load your library", self._error_message, "Try again")
        repolish(self._updates_chip)
