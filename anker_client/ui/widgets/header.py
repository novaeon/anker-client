"""Header row above the page stack.

Left: Back button (navigation history) and the global search box
(role=search, search glyph inside, clear button; Enter searches immediately,
typing searches after a 400 ms pause, Escape clears). Right: catalog-sync
indicator ("Syncing catalog · 3/37" with a spinner, then "Catalog up to date"
— or "Catalog sync failed" with the reason as tooltip — for a few seconds),
the game-updates chip ("3 updates") and the AnkerClient
update chip. The header only emits intents; the main window acts on them.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QWidget

from anker_client.core.formatting import pluralize
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Spinner, button, icon_button, label, repolish

SEARCH_DEBOUNCE_MS = 400
SYNC_DONE_VISIBLE_MS = 5000
SYNC_FAILED_VISIBLE_MS = 15000


class SearchBox(QLineEdit):
    """Pill search field with a painted-in search glyph (QSS reserves the left padding)."""

    escape_pressed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "search")
        self.setPlaceholderText("Search the store")
        self.setClearButtonEnabled(True)
        self.setMinimumWidth(260)
        self.setMaximumWidth(460)
        self._glyph = QLabel(self)
        self._glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        self._glyph.setPixmap(icons.pixmap("search", 16, palette.current().text_faint))
        self._glyph.setFixedSize(16, 16)
        self._place_glyph()

    def _place_glyph(self) -> None:
        self._glyph.move(13, (self.height() - 16) // 2)

    def resizeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_glyph()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class Header(QFrame):
    back_requested = pyqtSignal()
    search_requested = pyqtSignal(str)  # stripped query; "" when the box was cleared
    updates_clicked = pyqtSignal()
    app_update_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, debounce_ms: int = SEARCH_DEBOUNCE_MS) -> None:
        super().__init__(parent)
        self.setProperty("role", "toolbar")
        self.setFixedHeight(60)
        self._programmatic = False
        self._last_emitted: str | None = None
        self._sync_failed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 4)
        layout.setSpacing(10)

        self.back_button = icon_button("arrow_left", "Back (Alt+Left)", on_click=self.back_requested.emit, size=18)
        self.back_button.setFixedSize(QSize(36, 36))
        self.back_button.setEnabled(False)
        layout.addWidget(self.back_button)

        self.search = SearchBox()
        self.search.setToolTip("Search the store (Ctrl+F)")
        layout.addWidget(self.search, 3)
        layout.addStretch(1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self._emit_search)
        self.search.textChanged.connect(self._on_text_changed)
        self.search.returnPressed.connect(self._on_return)
        self.search.escape_pressed.connect(self.search.clear)

        layout.addWidget(self._build_sync_indicator())

        self.updates_chip = button("", "update", variant="notice", on_click=self.updates_clicked.emit)
        self.updates_chip.setToolTip("Show games with updates in your library")
        self.updates_chip.setVisible(False)
        layout.addWidget(self.updates_chip)

        self.app_update_chip = button("", "sparkles", variant="notice", on_click=self.app_update_clicked.emit)
        self.app_update_chip.setProperty("tone", "success")
        self.app_update_chip.setVisible(False)
        layout.addWidget(self.app_update_chip)
        self.refresh_icons()

    def _build_sync_indicator(self) -> QWidget:
        self.sync_indicator = QWidget()
        row = QHBoxLayout(self.sync_indicator)
        row.setContentsMargins(0, 0, 4, 0)
        row.setSpacing(7)
        self._sync_spinner = Spinner(14)
        self._sync_done_icon = QLabel()
        self._sync_done_icon.setFixedSize(14, 14)
        self.sync_label = label("", "caption")
        row.addWidget(self._sync_spinner)
        row.addWidget(self._sync_done_icon)
        row.addWidget(self.sync_label)
        self.sync_indicator.setVisible(False)
        self._sync_hide = QTimer(self)
        self._sync_hide.setSingleShot(True)
        self._sync_hide.timeout.connect(lambda: self.sync_indicator.setVisible(False))
        return self.sync_indicator

    # --- search -------------------------------------------------------------------------------
    def _on_text_changed(self, _text: str) -> None:
        if self._programmatic:
            return
        self._debounce.start()

    def _on_return(self) -> None:
        self._debounce.stop()
        self._emit_search(force=True)

    def _emit_search(self, force: bool = False) -> None:
        query = self.search.text().strip()
        if not force and query == self._last_emitted:
            return
        self._last_emitted = query
        self.search_requested.emit(query)

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def search_text(self) -> str:
        return self.search.text().strip()

    def set_search_text(self, text: str) -> None:
        """Show ``text`` without triggering a search."""
        if self.search.text() == text:
            self._last_emitted = text.strip()
            return
        self._programmatic = True
        try:
            self._debounce.stop()
            self.search.setText(text)
            self._last_emitted = text.strip()
        finally:
            self._programmatic = False

    def set_back_enabled(self, enabled: bool) -> None:
        self.back_button.setEnabled(enabled)

    # --- right side -----------------------------------------------------------------------------
    def set_sync_progress(self, done: int, total: int | None) -> None:
        self._sync_hide.stop()
        self._sync_failed = False
        self._sync_spinner.setVisible(True)
        self._sync_done_icon.setVisible(False)
        progress = f"{done}/{total}" if total else f"page {done}"
        self.sync_label.setText(f"Syncing catalog · {progress}")
        self.sync_indicator.setToolTip("Updating the local store index in the background")
        self.sync_indicator.setVisible(True)

    def set_sync_finished(self, new_games: int = 0) -> None:
        self._sync_failed = False
        self._show_sync_result("check_circle", palette.current().success,
                               f"{pluralize(new_games, 'new game')} in the store" if new_games
                               else "Catalog up to date", "")

    def set_sync_failed(self, reason: str = "") -> None:
        """The background catalog sync stopped early; the store keeps its previous index."""
        self._sync_failed = True
        self._show_sync_result("warning", palette.current().warning, "Catalog sync failed",
                               reason or "The store index could not be updated; it will be retried later.")

    def _show_sync_result(self, icon_name: str, color: str, text: str, tooltip: str) -> None:
        self._sync_spinner.setVisible(False)
        self._sync_done_icon.setPixmap(icons.pixmap(icon_name, 14, color))
        self._sync_done_icon.setVisible(True)
        self.sync_label.setText(text)
        self.sync_indicator.setToolTip(tooltip)
        self.sync_indicator.setVisible(True)
        self._sync_hide.start(SYNC_FAILED_VISIBLE_MS if self._sync_failed else SYNC_DONE_VISIBLE_MS)

    def sync_text(self) -> str:
        return self.sync_label.text() if not self.sync_indicator.isHidden() else ""

    def set_updates(self, count: int) -> None:
        self.updates_chip.setText(pluralize(count, "update"))
        self.updates_chip.setVisible(count > 0)

    def set_app_update(self, version: str) -> None:
        self.app_update_chip.setText(f"AnkerClient {version}" if version else "")
        self.app_update_chip.setToolTip(f"AnkerClient {version} is available — open the release page"
                                        if version else "")
        self.app_update_chip.setVisible(bool(version))

    def refresh_icons(self) -> None:
        pal = palette.current()
        self.back_button.setIcon(icons.icon("arrow_left", pal.text, disabled_color=pal.text_faint))
        self.search.refresh_icon()
        if self._sync_failed:
            self._sync_done_icon.setPixmap(icons.pixmap("warning", 14, pal.warning))
        else:
            self._sync_done_icon.setPixmap(icons.pixmap("check_circle", 14, pal.success))
        self.updates_chip.setIcon(icons.icon("update", pal.accent))
        self.app_update_chip.setIcon(icons.icon("sparkles", pal.success))
        for chip in (self.updates_chip, self.app_update_chip):
            repolish(chip)
