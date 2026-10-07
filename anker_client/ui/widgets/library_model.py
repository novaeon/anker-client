"""Library presentation logic: filters, sorts, badges and the list-view table model.

Pure functions (``filter_games``, ``sort_games``, ``game_badges``…) are shared
by the cover grid and the table so both views always show the same games in
the same order. The table adds per-column sorting on top through
:class:`LibrarySortProxy` (a ``QSortFilterProxyModel`` sorting on raw values).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSize, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QColor, QIcon

from anker_client.core.formatting import format_bytes, format_playtime, format_relative_time
from anker_client.core.models import InstalledGame
from anker_client.core.paths import normalize_title
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette
from anker_client.ui.widgets.cover_grid import CoverBadge, CoverItem

# (key, label) — keys are stable identifiers persisted between sessions.
FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All games"),
    ("favorites", "Favorites"),
    ("updates", "Updates available"),
    ("needs_setup", "Needs setup"),
    ("unmanaged", "Unmanaged"),
    ("hidden", "Hidden"),
)
FILTER_KEYS = frozenset(k for k, _ in FILTERS)

# Keys match ``Settings.library_sort``.
SORTS: tuple[tuple[str, str], ...] = (
    ("title", "Title"),
    ("last_played", "Recently played"),
    ("playtime", "Playtime"),
    ("installed", "Recently installed"),
    ("size", "Size"),
)
SORT_KEYS = frozenset(k for k, _ in SORTS)


# --- predicates ---------------------------------------------------------------------------


def needs_setup(game: InstalledGame) -> bool:
    """No executable chosen yet — Play would ask which program to start."""
    return not game.executable


def matches_filter(game: InstalledGame, key: str, *, show_hidden: bool = False) -> bool:
    if key == "hidden":
        return game.hidden
    if game.hidden and not show_hidden:
        return False
    if key == "favorites":
        return game.favorite
    if key == "updates":
        return game.update_available
    if key == "needs_setup":
        return needs_setup(game)
    if key == "unmanaged":
        return not game.managed
    return True


def matches_query(game: InstalledGame, query: str) -> bool:
    """Every word of ``query`` appears in the title or folder name (accent/punctuation-insensitive)."""
    words = normalize_title(query).split()
    if not words:
        return True
    haystack = f"{normalize_title(game.title)} {normalize_title(game.folder_name)}"
    return all(word in haystack for word in words)


def filter_games(
    games: Iterable[InstalledGame], *, query: str = "", filter_key: str = "all", show_hidden: bool = False
) -> list[InstalledGame]:
    return [g for g in games if matches_filter(g, filter_key, show_hidden=show_hidden) and matches_query(g, query)]


def filter_counts(games: Iterable[InstalledGame], *, show_hidden: bool = False) -> dict[str, int]:
    games = list(games)
    return {key: sum(1 for g in games if matches_filter(g, key, show_hidden=show_hidden)) for key, _ in FILTERS}


def _title_key(game: InstalledGame) -> str:
    return game.title.casefold()


def sort_games(games: Iterable[InstalledGame], key: str) -> list[InstalledGame]:
    """Order for the grid/list. Ties (and unknown values) fall back to title A–Z."""
    ordered = sorted(games, key=_title_key)
    if key == "last_played":
        ordered.sort(key=lambda g: g.last_played or "", reverse=True)
    elif key == "playtime":
        ordered.sort(key=lambda g: g.playtime_seconds or 0, reverse=True)
    elif key == "installed":
        ordered.sort(key=lambda g: g.installed_at or "", reverse=True)
    elif key == "size":
        ordered.sort(key=lambda g: g.size_bytes if g.size_bytes is not None else -1, reverse=True)
    return ordered


# --- presentation -------------------------------------------------------------------------


def game_badges(game: InstalledGame, running: bool) -> tuple[CoverBadge, ...]:
    badges: list[CoverBadge] = []
    if running:
        badges.append(CoverBadge("Running", "accent"))
    if game.update_available:
        badges.append(CoverBadge("Update", "warning"))
    if needs_setup(game):
        badges.append(CoverBadge("Needs setup", "danger"))
    if not game.managed:
        badges.append(CoverBadge("Unmanaged", ""))
    return tuple(badges)


# (text, palette colour attribute, rank) — rank orders the Status column.
def status_info(game: InstalledGame, running: bool) -> tuple[str, str, int]:
    if running:
        return "Running", "accent", 0
    if needs_setup(game):
        return "Needs setup", "danger", 1
    if game.update_available:
        return "Update available", "warning", 2
    if not game.managed:
        return "Unmanaged", "text_muted", 3
    if game.hidden:
        return "Hidden", "text_faint", 5
    return "Ready to play", "text_muted", 4


def playtime_text(game: InstalledGame) -> str:
    return f"{format_playtime(game.playtime_seconds)} played" if game.playtime_seconds else "Never played"


def cover_item(game: InstalledGame, running: bool) -> CoverItem:
    return CoverItem(
        key=game.install_id,
        title=game.title,
        subtitle=playtime_text(game),
        cover_url=game.cover_url,
        badges=game_badges(game, running),
        dimmed=game.hidden,
        favorite=game.favorite,
        payload=game,
    )


# --- table model ----------------------------------------------------------------------------

COLUMNS: tuple[str, ...] = ("Title", "Playtime", "Last played", "Size", "Version", "Status")
COL_TITLE, COL_PLAYTIME, COL_LAST_PLAYED, COL_SIZE, COL_VERSION, COL_STATUS = range(len(COLUMNS))
SORT_ROLE = Qt.ItemDataRole.UserRole + 10
INSTALL_ID_ROLE = Qt.ItemDataRole.UserRole + 11
THUMB_SIZE = QSize(24, 36)

#: Sort combo key → (column, order) used to keep the table's header indicator in sync.
SORT_TO_COLUMN: dict[str, tuple[int, Qt.SortOrder]] = {
    "title": (COL_TITLE, Qt.SortOrder.AscendingOrder),
    "last_played": (COL_LAST_PLAYED, Qt.SortOrder.DescendingOrder),
    "playtime": (COL_PLAYTIME, Qt.SortOrder.DescendingOrder),
    "size": (COL_SIZE, Qt.SortOrder.DescendingOrder),
}


class LibraryTableModel(QAbstractTableModel):
    """Rows = installed games (already filtered and ordered by the page)."""

    def __init__(self, loader: ImageLoader | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._games: list[InstalledGame] = []
        self._running: set[str] = set()
        self._rows: dict[str, int] = {}
        self._loader = loader
        self._thumbs: dict[str, QIcon] = {}
        if loader is not None:
            loader.loaded.connect(self._on_image_loaded)

    # --- Qt API ---------------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self._games)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMNS):
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._games):
            return None
        game = self._games[index.row()]
        col = index.column()
        running = game.install_id in self._running
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(game, col, running)
        if role == SORT_ROLE:
            return self._sort_value(game, col, running)
        if role == INSTALL_ID_ROLE:
            return game.install_id
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(game, col, running)
        if role == Qt.ItemDataRole.DecorationRole and col == COL_TITLE:
            return self._thumbnail(game)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_TITLE:
                return f"{game.title}\n{game.path}"
            if col == COL_LAST_PLAYED and game.last_played:
                return game.last_played
        if role == Qt.ItemDataRole.SizeHintRole and col == COL_TITLE:
            return QSize(260, 44)
        return None

    # --- content ----------------------------------------------------------------------------
    def set_games(self, games: list[InstalledGame], running: set[str]) -> None:
        keys = [g.install_id for g in games]
        self._running = set(running)
        if keys == [g.install_id for g in self._games]:
            self._games = list(games)
            if games:
                self.dataChanged.emit(self.index(0, 0), self.index(len(games) - 1, len(COLUMNS) - 1))
            return
        self.beginResetModel()
        self._games = list(games)
        self._rows = {key: row for row, key in enumerate(keys)}
        self.endResetModel()

    def set_running(self, running: set[str]) -> None:
        changed = self._running ^ set(running)
        self._running = set(running)
        for install_id in changed:
            row = self._rows.get(install_id)
            if row is not None:
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def game_at(self, row: int) -> InstalledGame | None:
        return self._games[row] if 0 <= row < len(self._games) else None

    def row_of(self, install_id: str) -> int:
        return self._rows.get(install_id, -1)

    def install_ids(self) -> list[str]:
        return [g.install_id for g in self._games]

    # --- helpers ------------------------------------------------------------------------------
    @staticmethod
    def _display(game: InstalledGame, col: int, running: bool) -> str:
        if col == COL_TITLE:
            return game.title
        if col == COL_PLAYTIME:
            return format_playtime(game.playtime_seconds) if game.playtime_seconds else "—"
        if col == COL_LAST_PLAYED:
            return format_relative_time(game.last_played).capitalize() if game.last_played else "Never"
        if col == COL_SIZE:
            return format_bytes(game.size_bytes, unknown="—")
        if col == COL_VERSION:
            return game.version or "—"
        if col == COL_STATUS:
            return status_info(game, running)[0]
        return ""

    @staticmethod
    def _sort_value(game: InstalledGame, col: int, running: bool) -> Any:
        if col == COL_TITLE:
            return game.title.casefold()
        if col == COL_PLAYTIME:
            return game.playtime_seconds or 0
        if col == COL_LAST_PLAYED:
            return game.last_played or ""
        if col == COL_SIZE:
            return game.size_bytes if game.size_bytes is not None else -1
        if col == COL_VERSION:
            return game.version.casefold()
        if col == COL_STATUS:
            return status_info(game, running)[2]
        return None

    @staticmethod
    def _foreground(game: InstalledGame, col: int, running: bool) -> QColor | None:
        pal = palette.current()
        if col == COL_STATUS:
            return QColor(getattr(pal, status_info(game, running)[1]))
        if game.hidden:
            return QColor(pal.text_faint)
        if col != COL_TITLE:
            return QColor(pal.text_muted)
        return None

    def _thumbnail(self, game: InstalledGame) -> QIcon | None:
        if self._loader is None or not game.cover_url:
            return None
        cached = self._thumbs.get(game.cover_url)
        if cached is not None:
            return cached
        pixmap = self._loader.request(game.cover_url, THUMB_SIZE * 2)
        if pixmap is None or pixmap.isNull():
            return None
        thumb = QIcon(pixmap)
        if len(self._thumbs) > 512:
            self._thumbs.clear()
        self._thumbs[game.cover_url] = thumb
        return thumb

    def _on_image_loaded(self, url: str, _size_key: str) -> None:
        for row, game in enumerate(self._games):
            if game.cover_url == url:
                idx = self.index(row, COL_TITLE)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


class LibrarySortProxy(QSortFilterProxyModel):
    """Sorts on raw values (``SORT_ROLE``); ties keep the page's order (title A–Z by default)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        a = left.data(SORT_ROLE)
        b = right.data(SORT_ROLE)
        if a == b or a is None or b is None:
            return False  # the proxy's sort is stable: equal rows keep the page's order
        try:
            return bool(a < b)
        except TypeError:
            return str(a) < str(b)
