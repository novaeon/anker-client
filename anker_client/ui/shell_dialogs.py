"""Small modal dialogs owned by the shell: what closing the window does, and quitting with downloads running.

Both functions are module-level so tests (and the main window) can swap them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from anker_client.core.formatting import pluralize
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, label


class CloseChoice(StrEnum):
    TRAY = "tray"
    QUIT = "quit"


@dataclass(frozen=True, slots=True)
class CloseDecision:
    choice: CloseChoice
    remember: bool


class _MessageDialog(QDialog):
    """Icon + title + message + optional checkbox + right-aligned buttons, styled by the theme."""

    def __init__(self, parent: QWidget | None, *, window_title: str, icon_name: str, icon_color: str,
                 title: str, message: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.result_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, 28, icon_color))
        head.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(6)
        column.addWidget(label(title, "title"))
        column.addWidget(label(message, "muted", wrap=True))
        head.addLayout(column, 1)
        layout.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(42, 0, 0, 0)
        self.body.setSpacing(4)
        layout.addLayout(self.body)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        layout.addLayout(self.buttons)

    def add_button(self, text: str, key: str, *, variant: str = "", default: bool = False) -> None:
        btn = button(text, variant=variant, on_click=lambda: self._finish(key))
        btn.setObjectName(f"button_{key}")
        if default:
            btn.setDefault(True)
            btn.setFocus()
        self.buttons.addWidget(btn)

    def _finish(self, key: str) -> None:
        self.result_key = key
        if key == "cancel":
            self.reject()
        else:
            self.accept()


def ask_close_action(parent: QWidget | None, *, tray_available: bool) -> CloseDecision | None:
    """Ask what closing the window should do. ``None`` = cancelled."""
    pal = palette.current()
    if tray_available:
        message = ("AnkerClient can keep running in the notification area so downloads and playtime "
                   "tracking continue. You can quit any time from the tray icon.")
        keep_text = "Keep running in tray"
    else:
        message = "AnkerClient can stay open (minimised) so downloads and playtime tracking continue."
        keep_text = "Minimise instead"
    dialog = _MessageDialog(parent, window_title="Close AnkerClient", icon_name="tray", icon_color=pal.accent,
                            title="Close AnkerClient?", message=message)
    remember = QCheckBox("Remember my choice")
    remember.setObjectName("remember")
    dialog.body.addWidget(remember)
    dialog.body.addWidget(label("You can change this later in Settings → General.", "caption"))
    dialog.add_button("Cancel", "cancel", variant="ghost")
    dialog.add_button("Quit", "quit")
    dialog.add_button(keep_text, "tray", variant="primary", default=True)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        choice = CloseChoice.QUIT if dialog.result_key == "quit" else CloseChoice.TRAY
        return CloseDecision(choice, remember.isChecked())
    finally:
        dialog.deleteLater()  # parented to the main window: would otherwise live as long as it


def confirm_quit_with_downloads(parent: QWidget | None, count: int) -> bool:
    pal = palette.current()
    dialog = _MessageDialog(
        parent,
        window_title="Quit AnkerClient",
        icon_name="download",
        icon_color=pal.warning,
        title="Quit while downloading?",
        message=f"{pluralize(count, 'download')} {'is' if count == 1 else 'are'} in progress. "
                "Downloads will pause and resume next time you open AnkerClient.",
    )
    dialog.add_button("Keep running", "cancel", variant="ghost", default=True)
    dialog.add_button("Quit", "quit", variant="danger")
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_key == "quit"
    finally:
        dialog.deleteLater()
