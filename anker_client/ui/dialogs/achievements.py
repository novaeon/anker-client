"""The achievements window of one installed game, laid out like Steam's achievements page.

Header: game title, "You've unlocked 5/9 (56%)" with a progress bar and which
emulator's files were read. List: unlocked achievements first (newest first),
then locked ones from most to least common, with greyed icons, progress bars
and global rarity. Locked hidden achievements are summarised as Steam does
("2 hidden achievements remaining · Details for each achievement will be
revealed once unlocked") with a "Show" link for players who want spoilers.

Loads through ``ctx.achievements.achievements_for`` off the GUI thread and
reloads itself on ``achievements_changed`` for its game, so it updates live
while the game is running. "Refresh" re-fetches Steam's list.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QFrame, QProgressBar, QScrollArea, QVBoxLayout, QWidget

from anker_client.core.models import GameAchievements
from anker_client.core.tasks import TaskHandle
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.achievement_widgets import AchievementRow, hidden_summary, sort_like_steam
from anker_client.ui.widgets.common import EmptyState, button, hbox, label

log = logging.getLogger(__name__)


class AchievementsDialog(QDialog):
    def __init__(self, ctx: Any, bridge: QtEventBridge, loader: ImageLoader, install_id: str, title: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._loader = loader
        self.install_id = install_id
        self.data: GameAchievements | None = None
        self._handle: TaskHandle[Any] | None = None
        self._show_hidden = False
        self.setObjectName("achievementsDialog")
        self.setWindowTitle(f"Achievements · {title}" if title else "Achievements")
        self.setMinimumSize(560, 420)
        self.resize(760, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        self.title_label = label(title or "Achievements", "title")
        self.refresh_button = button("Refresh", "refresh", variant="ghost", size="sm",
                                     on_click=lambda: self.reload(refresh=True))
        root.addLayout(hbox(self.title_label, None, self.refresh_button))
        self.summary_label = label("", "muted")
        root.addWidget(self.summary_label)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 100)
        root.addWidget(self.bar)
        self.source_label = label("", "caption", wrap=True)
        root.addWidget(self.source_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)
        self.empty = EmptyState("trophy", "No achievements", "")
        self.empty.hide()
        root.addWidget(self.empty, 1)
        close = button("Close", on_click=self.accept)
        root.addLayout(hbox(None, close))

        bridge.achievements_changed.connect(self._on_changed)
        self.reload()

    # --- loading ----------------------------------------------------------------------------
    def reload(self, *, refresh: bool = False) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self.refresh_button.setEnabled(False)
        if self.data is None:
            self.summary_label.setText("Loading achievements…")
        self._handle = run_async(
            self, self._ctx.runner, self._ctx.achievements.achievements_for, self.install_id, refresh=refresh,
            on_result=self._on_loaded, on_error=self._on_failed,
            on_finished=lambda: self.refresh_button.setEnabled(True),
        )

    def _on_changed(self, event: Any) -> None:
        if self.install_id in getattr(event, "install_ids", ()) or (
                self.data is not None and self.data.app_id in getattr(event, "app_ids", ())):
            self.reload()

    def _on_failed(self, exc: BaseException) -> None:
        log.warning("Achievements could not be loaded for %s: %s", self.install_id, exc)
        self.summary_label.setText(f"Achievements could not be loaded: {error_text(exc)}")

    def _on_loaded(self, data: GameAchievements | None) -> None:
        self.data = data
        self._render()

    # --- rendering --------------------------------------------------------------------------
    def toggle_hidden(self) -> None:
        self._show_hidden = not self._show_hidden
        self._render()

    def _render(self) -> None:
        data = self.data
        if data is None or not data.achievements:
            self.scroll.hide()
            self.bar.hide()
            self.empty.show()
            message = ("This game does not use a Steam emulator AnkerClient can read."
                       if data is None else "This game has no Steam achievements.")
            self.empty.set_content("trophy", "No achievements", message)
            self.summary_label.setText("")
            self.source_label.setText("")
            return
        self.empty.hide()
        self.scroll.show()
        if data.title:
            self.title_label.setText(data.title)
        if data.schema_available:
            self.summary_label.setText(
                f"You've unlocked {data.unlocked_count}/{data.total} ({data.percent}%)")
            self.bar.setValue(data.percent)
            self.bar.show()
        else:
            self.summary_label.setText(f"You've unlocked {data.unlocked_count} achievements")
            self.bar.hide()
        sources = ", ".join(data.emulators) or "no unlock files yet"
        note = "" if data.schema_available else " Names, icons and rarity need a connection to Steam."
        self.source_label.setText(f"Read from {html.escape(sources)} save files (Steam app {data.app_id}).{note}")

        body = QWidget()
        body.setProperty("role", "transparent")
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(8)
        ordered = sort_like_steam(data.achievements)
        hidden_locked = [a for a in ordered if a.hidden and not a.unlocked]
        for achievement in ordered:
            if achievement.hidden and not achievement.unlocked and not self._show_hidden:
                continue
            col.addWidget(AchievementRow(achievement, self._loader))
        if hidden_locked:
            note_box = QFrame()
            note_box.setProperty("role", "card")
            inner = QVBoxLayout(note_box)
            inner.setContentsMargins(16, 12, 16, 12)
            inner.setSpacing(6)
            if not self._show_hidden:
                inner.addWidget(hidden_summary(len(hidden_locked)))
            self.hidden_button = button("Hide details" if self._show_hidden else "Show details", variant="link",
                                        on_click=self.toggle_hidden)
            self.hidden_button.setObjectName("toggle_hidden")
            inner.addLayout(hbox(None, self.hidden_button))
            col.addWidget(note_box)
        col.addStretch(1)
        position = self.scroll.verticalScrollBar().value()
        self.scroll.setWidget(body)
        self.scroll.verticalScrollBar().setValue(position)

    def rows(self) -> list[AchievementRow]:
        widget = self.scroll.widget()
        return widget.findChildren(AchievementRow) if widget is not None else []

    def done(self, result: int) -> None:
        if self._handle is not None:
            self._handle.cancel()
        super().done(result)
