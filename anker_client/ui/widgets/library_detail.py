"""Right-hand detail panel of the Library page.

Shows the selected :class:`InstalledGame` (poster, title, genres, status
badges, Play/Stop + Update, playtime, last played, version, size, install
folder) and its management actions. The panel never calls services itself:
every click is emitted as ``action_requested(<action>)`` and the page runs the
work, then reports back with ``set_size`` / ``set_busy`` / ``set_running``.

Actions: play, stop, update, choose_exe, properties, open_folder, favorite,
hide, shortcuts, redist, check_update, repair, store, uninstall.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.formatting import format_bytes, format_playtime, format_relative_time
from anker_client.core.models import InstalledGame
from anker_client.ui import icons
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Badge, Divider, EmptyState, button, label, repolish
from anker_client.ui.widgets.image_label import AsyncImage
from anker_client.ui.widgets.library_common import ElidedLink, IconTinter
from anker_client.ui.widgets.library_model import needs_setup

PANEL_WIDTH = 380
POSTER_SIZE = (96, 144)


class _Stat(QWidget):
    """Caption above a value ("PLAYTIME" / "41.0 hours")."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.caption = label(caption.upper(), "caption")
        self.value = label("—")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set(self, text: str, tooltip: str = "") -> None:
        self.value.setText(text)
        self.value.setToolTip(tooltip)


class LibraryDetailPanel(QFrame):
    action_requested = pyqtSignal(str)

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")
        self.setFixedWidth(PANEL_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._loader = loader
        self._game: InstalledGame | None = None
        self._running = False
        self._busy: dict[str, str] = {}  # action -> busy caption
        self._tint = IconTinter(self)

        self._stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        self._placeholder = EmptyState("library", "No game selected", "Pick a game to see its details.")
        self._stack.addWidget(self._placeholder)
        self._stack.addWidget(self._build_content())
        self._tint.on_theme_changed(self._refresh_theme)
        self.set_game(None)

    # --- construction ---------------------------------------------------------------------
    def _build_content(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setProperty("role", "transparent")
        col = QVBoxLayout(body)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(12)

        # header: poster + title/genres/badges
        self.poster = AsyncImage(self._loader, radius=6)
        self.poster.setFixedSize(*POSTER_SIZE)
        self.title = label("", "title", wrap=True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.genres = label("", "muted", wrap=True)
        self._badge_box = QHBoxLayout()
        self._badge_box.setSpacing(4)
        self._badges: dict[str, Badge] = {}
        for key, text, kind in (("running", "Running", "accent"), ("update", "Update", "warning"),
                                ("setup", "Needs setup", "danger"), ("unmanaged", "Unmanaged", ""),
                                ("hidden", "Hidden", ""), ("favorite", "Favorite", "")):
            badge = Badge(text, kind)
            badge.hide()
            self._badges[key] = badge
            self._badge_box.addWidget(badge)
        self._badge_box.addStretch(1)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addWidget(self.title)
        info.addWidget(self.genres)
        info.addLayout(self._badge_box)
        info.addStretch(1)
        header = QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignTop)
        header.addLayout(info, 1)
        col.addLayout(header)

        # primary actions
        self.play_button = button("Play", variant="primary", size="lg",
                                  on_click=lambda: self._emit("stop" if self._running else "play"))
        self.play_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.favorite_button = button("", size="lg", on_click=lambda: self._emit("favorite"))
        self.favorite_button.setObjectName("action_favorite")
        self.favorite_button.setFixedWidth(52)
        play_row = QHBoxLayout()
        play_row.setSpacing(8)
        play_row.addWidget(self.play_button, 1)
        play_row.addWidget(self.favorite_button)
        col.addLayout(play_row)
        self.update_button = button("Update", variant="warning", on_click=lambda: self._emit("update"))
        col.addWidget(self.update_button)
        # one line, so the panel still fits without scrolling when an Update button is shown too
        self._setup_icon = QLabel()
        self.setup_hint = label("No program chosen yet", "warning")
        self.setup_button = button("Choose executable…", variant="link", on_click=lambda: self._emit("choose_exe"))
        setup_row = QHBoxLayout()
        setup_row.setContentsMargins(2, 0, 0, 0)
        setup_row.setSpacing(6)
        setup_row.addWidget(self._setup_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        setup_row.addWidget(self.setup_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        setup_row.addStretch(1)
        setup_row.addWidget(self.setup_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._setup_box = QWidget()
        self._setup_box.setLayout(setup_row)
        self._paint_setup_icon()
        col.addWidget(self._setup_box)

        col.addWidget(Divider())

        # facts
        stats = QGridLayout()
        stats.setHorizontalSpacing(16)
        stats.setVerticalSpacing(12)
        self.stat_playtime = _Stat("Playtime")
        self.stat_last_played = _Stat("Last played")
        self.stat_version = _Stat("Version")
        self.stat_size = _Stat("Size on disk")
        stats.addWidget(self.stat_playtime, 0, 0)
        stats.addWidget(self.stat_last_played, 0, 1)
        stats.addWidget(self.stat_version, 1, 0)
        stats.addWidget(self.stat_size, 1, 1)
        stats.setColumnStretch(0, 1)
        stats.setColumnStretch(1, 1)
        col.addLayout(stats)

        folder_box = QVBoxLayout()
        folder_box.setSpacing(2)
        folder_box.addWidget(label("INSTALL FOLDER", "caption"))
        self.folder_link = ElidedLink()
        self.folder_link.clicked.connect(lambda: self._emit("open_folder"))
        folder_box.addWidget(self.folder_link)
        col.addLayout(folder_box)

        col.addWidget(Divider())

        # management actions (2-column list)
        self._actions: dict[str, QPushButton] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(0)
        specs = (
            ("properties", "Properties", "settings"),
            ("open_folder", "Open folder", "folder"),
            ("shortcuts", "Create shortcuts", "shortcut"),
            ("hide", "Hide game", "eye_off"),
            ("check_update", "Check for update", "refresh"),
            ("repair", "Repair (reinstall)", "wrench"),
            ("redist", "Install prerequisites", "package"),
            ("store", "Store page", "store"),
        )
        for key, text, icon_name in specs:
            btn = button(text, variant="nav", on_click=lambda k=key: self._emit(k))
            btn.setObjectName(f"action_{key}")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._tint.set(btn, icon_name, tint="muted", size=16)
            self._actions[key] = btn
        self._action_grid = grid
        self._visible_actions: dict[str, bool] = {}
        self._action_order = [key for key, _t, _i in specs]
        col.addLayout(grid)

        col.addStretch(1)

        self.uninstall_button = button("Uninstall", variant="danger", on_click=lambda: self._emit("uninstall"))
        self._tint.set(self.uninstall_button, "trash", tint="danger", size=16)
        self.uninstall_progress = QProgressBar()
        self.uninstall_progress.setRange(0, 0)
        self.uninstall_progress.setProperty("state", "error")
        self.uninstall_progress.hide()
        col.addWidget(self.uninstall_button)
        col.addWidget(self.uninstall_progress)

        scroll.setWidget(body)
        return scroll

    # --- public API -------------------------------------------------------------------------
    @property
    def game(self) -> InstalledGame | None:
        return self._game

    @property
    def install_id(self) -> str:
        return self._game.install_id if self._game else ""

    def set_game(self, game: InstalledGame | None, *, running: bool = False) -> None:
        previous = self.install_id
        self._game = game.copy() if game is not None else None
        self._running = running
        if game is None:
            self._busy.clear()
            self._stack.setCurrentIndex(0)
            return
        if game.install_id != previous:
            self._busy.clear()
            self.set_size(game.size_bytes)
        elif game.size_bytes is not None:
            self.set_size(game.size_bytes)
        if game.install_id != previous or game.cover_url != self.poster.url():
            self.poster.set_image(game.cover_url, game.title)
        self._stack.setCurrentIndex(1)
        self._render()

    def set_running(self, running: bool) -> None:
        if running != self._running:
            self._running = running
            self._render()

    def set_size(self, size: int | None, *, calculating: bool = False) -> None:
        if calculating:
            self.stat_size.set("Calculating…")
        else:
            self.stat_size.set(format_bytes(size, unknown="Unknown"), f"{size:,} bytes" if size else "")

    def set_busy(self, action: str, busy: bool, text: str = "") -> None:
        if busy:
            self._busy[action] = text
        else:
            self._busy.pop(action, None)
        if self._game is not None:
            self._render()

    def is_busy(self, action: str) -> bool:
        return action in self._busy

    def action_button(self, action: str) -> QPushButton | None:
        """The button bound to ``action`` (for tests and keyboard focus)."""
        if action in ("play", "stop"):
            return self.play_button
        if action == "update":
            return self.update_button
        if action == "favorite":
            return self.favorite_button
        if action == "uninstall":
            return self.uninstall_button
        return self._actions.get(action)

    # --- rendering ---------------------------------------------------------------------------
    def _render(self) -> None:
        game = self._game
        if game is None:
            return
        self.title.setText(game.title)
        self.genres.setText(" · ".join(game.genres[:4]))
        self.genres.setVisible(bool(game.genres))
        setup = needs_setup(game)
        visible_badges = {
            "running": self._running,
            "update": game.update_available,
            "setup": setup,
            "unmanaged": not game.managed,
            "hidden": game.hidden,
            "favorite": game.favorite,
        }
        for key, badge in self._badges.items():
            badge.setVisible(visible_badges[key])

        self._render_play(setup)
        self._render_update(game)
        self._setup_box.setVisible(setup and not self._running)

        self.stat_playtime.set(format_playtime(game.playtime_seconds))
        self.stat_last_played.set(
            format_relative_time(game.last_played).capitalize() if game.last_played else "Never",
            game.last_played,
        )
        self.stat_version.set(game.version or "Unknown")
        self.folder_link.set_full_text(game.path)
        # the filled glyph in both states: the outline heart renders poorly at button sizes
        self._tint.set(self.favorite_button, "heart_filled", tint="danger" if game.favorite else "faint", size=20)
        self.favorite_button.setToolTip("Remove from favorites" if game.favorite else "Add to favorites")
        self.favorite_button.setAccessibleName(self.favorite_button.toolTip())

        hide_text, hide_icon = ("Unhide game", "eye") if game.hidden else ("Hide game", "eye_off")
        self._actions["hide"].setText(hide_text)
        self._tint.set(self._actions["hide"], hide_icon, tint="muted", size=16)
        visible = {
            "redist": game.has_redist and not game.redist_installed,
            # an unmanaged folder's slug is only a catalog guess: "repair" would install a second copy
            "repair": bool(game.slug) and game.managed,
            "store": bool(game.slug),
            "check_update": bool(game.slug) and game.managed,
            "shortcuts": not setup,
        }
        self._layout_actions({key: visible.get(key, True) for key in self._action_order})
        defaults = {"check_update": "Check for update", "redist": "Install prerequisites",
                    "shortcuts": "Create shortcuts", "repair": "Repair (reinstall)"}
        for key, default in defaults.items():
            btn = self._actions[key]
            busy = key in self._busy
            btn.setEnabled(not busy)
            btn.setText(self._busy.get(key) or default if busy else default)

        uninstalling = "uninstall" in self._busy
        self.uninstall_button.setEnabled(not uninstalling and not self._running)
        self.uninstall_button.setText(self._busy.get("uninstall") or "Uninstalling…" if uninstalling else "Uninstall")
        self.uninstall_button.setToolTip("Close the game before uninstalling it." if self._running else "")
        self.uninstall_progress.setVisible(uninstalling)
        self.play_button.setEnabled(not uninstalling and "play" not in self._busy and "stop" not in self._busy)

    def _render_play(self, setup: bool) -> None:
        btn = self.play_button
        if self._running:
            text, icon_name, variant, tint = "Stop", "stop", "danger", "danger"
        else:
            text, icon_name, variant, tint = "Play", "play", "primary", "on_accent"
        if "stop" in self._busy:
            text = "Stopping…"
        elif "play" in self._busy:
            text = "Starting…"
        btn.setText(text)
        btn.setToolTip("Choose which program starts this game first." if setup and not self._running else "")
        if btn.property("variant") != variant:
            btn.setProperty("variant", variant)
            repolish(btn)
        self._tint.set(btn, icon_name, tint=tint, size=20)

    def _render_update(self, game: InstalledGame) -> None:
        show = game.update_available and bool(game.slug)
        self.update_button.setVisible(show)
        if not show:
            return
        busy = "update" in self._busy
        target = game.latest_version
        if busy:
            self.update_button.setText(self._busy.get("update") or "Preparing update…")
        else:
            self.update_button.setText(f"Update to {target}" if target else "Update available")
        self.update_button.setEnabled(not busy)
        if game.version and target:
            self.update_button.setToolTip(f"Installed {game.version} → latest {target}")

    def _layout_actions(self, visible: dict[str, bool]) -> None:
        if visible == self._visible_actions:
            return
        self._visible_actions = dict(visible)
        grid = self._action_grid
        while grid.count():
            grid.takeAt(0)
        index = 0
        for key in self._action_order:
            btn = self._actions[key]
            if visible[key]:
                grid.addWidget(btn, index // 2, index % 2)
                btn.show()
                index += 1
            else:
                btn.hide()

    def _refresh_theme(self) -> None:
        self._placeholder.set_content("library", "No game selected", "Pick a game to see its details.")
        self._paint_setup_icon()

    def _paint_setup_icon(self) -> None:
        self._setup_icon.setPixmap(icons.pixmap("warning", 16, palette.current().warning))

    def _emit(self, action: str) -> None:
        if self._game is not None:
            self.action_requested.emit(action)
