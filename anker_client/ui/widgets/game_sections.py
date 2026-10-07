"""Content sections of the Game page.

* :class:`ExpandableText` — the "About" text, collapsed to a few lines with
  "Show more" / "Show less" (the toggle only appears when the text overflows).
* :class:`RequirementsCard` — OS, Processor, Memory, Graphics, DirectX, Storage
  in a two-column grid (falls back to the raw text block); hidden when empty.
* :class:`FactsCard` — label/value rows (Size, Version, Released, Updated…).
* :class:`AddonsCard` — ADDON download options with Install buttons, enabled
  only when the base game is installed; applied add-ons show "Installed".
* :class:`InlineBanner` — one-line warning/error with an optional action.
* :class:`GameErrorView` — full-page error with Retry and Back.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from anker_client.core.models import DownloadOption, InstalledGame, SystemRequirements
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Badge, Divider, button, hbox, label
from anker_client.ui.widgets.game_common import tag_icon

_QWIDGETSIZE_MAX = 16777215
_SIZE_SUFFIX_RE = re.compile(r"\s*\(\s*[\d.,]+\s*(?:B|KB|MB|GB|TB)\s*\)\s*$", re.IGNORECASE)


def option_title(option: DownloadOption) -> str:
    """Option label without the trailing "(1.2 GB)" size (shown separately)."""
    return _SIZE_SUFFIX_RE.sub("", option.label).strip() or option.label


def _card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    if title:
        layout.addWidget(label(title, "heading"))
    return frame, layout


class ExpandableText(QWidget):
    COLLAPSED_LINES = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = False
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.toggle = button("Show more", "chevron_down", variant="link", on_click=self.toggle_expanded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.text_label)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.toggle.hide()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def text(self) -> str:
        return self.text_label.text()

    def set_text(self, text: str) -> None:
        cleaned = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r\n", "\n").strip())
        if cleaned != self.text_label.text():
            self.text_label.setText(cleaned)
            self._expanded = False
        self._update()

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._update()

    def is_overflowing(self) -> bool:
        width = max(1, self.text_label.width() if self.text_label.width() > 1 else self.width())
        return self.text_label.heightForWidth(width) > self._collapsed_height() + 2

    def _collapsed_height(self) -> int:
        return self.text_label.fontMetrics().lineSpacing() * self.COLLAPSED_LINES

    def _update(self) -> None:
        overflow = self.is_overflowing()
        self.toggle.setVisible(overflow)
        collapsed = overflow and not self._expanded
        self.text_label.setMaximumHeight(self._collapsed_height() if collapsed else _QWIDGETSIZE_MAX)
        self.toggle.setText("Show less" if self._expanded else "Show more")
        self.toggle.setIcon(icons.icon("chevron_up" if self._expanded else "chevron_down", palette.current().accent))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update()


class RequirementsCard(QFrame):
    FIELDS = (
        ("os", "OS", "globe"),
        ("processor", "Processor", "cpu"),
        ("memory", "Memory", "hdd"),
        ("graphics", "Graphics", "image"),
        ("directx", "DirectX", "package"),
        ("storage", "Storage", "archive"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)
        layout.addWidget(label("System requirements", "heading"))
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(28)
        self._grid.setVerticalSpacing(16)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        layout.addLayout(self._grid)
        self.raw = label("", wrap=True, selectable=True)
        layout.addWidget(self.raw)
        self.values: dict[str, str] = {}

    def set_requirements(self, req: SystemRequirements | None) -> bool:
        """Show ``req``; returns False (and hides) when there is nothing to show."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.hide()
                child.deleteLater()
        self.values = {}
        if req is None:
            self.hide()
            return False
        cells: list[tuple[str, str, str]] = []
        for key, name, icon_name in self.FIELDS:
            value = str(getattr(req, key) or "").strip()
            if value:
                cells.append((name, icon_name, value))
        for index, (name, icon_name, value) in enumerate(cells):
            self.values[name] = value
            self._grid.addWidget(self._cell(name, icon_name, value), index // 2, index % 2)
        self.raw.setText(req.raw.strip() if not cells else "")
        self.raw.setVisible(not cells and bool(req.raw.strip()))
        has_content = bool(cells) or bool(req.raw.strip())
        self.setVisible(has_content)
        return has_content

    @staticmethod
    def _cell(name: str, icon_name: str, value: str) -> QWidget:
        cell = QWidget()
        icon = QLabel()
        icon.setPixmap(icons.pixmap(icon_name, 18, palette.current().text_muted))
        icon.setFixedWidth(22)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(name, "caption"))
        text.addWidget(label(value, wrap=True, selectable=True))
        row = QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)
        return cell


@dataclass(frozen=True, slots=True)
class Fact:
    name: str
    value: str
    tooltip: str = ""
    action_text: str = ""  # optional link button after the value
    action: Callable[[], None] | None = None


class FactsCard(QFrame):
    def __init__(self, title: str = "Details", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(10)
        layout.addWidget(label(title, "heading"))
        self._rows = QVBoxLayout()
        self._rows.setSpacing(0)
        layout.addLayout(self._rows)
        self.facts: list[Fact] = []

    def set_facts(self, facts: list[Fact]) -> None:
        if facts == self.facts:
            return
        self.facts = list(facts)
        while self._rows.count():
            item = self._rows.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.hide()
                child.deleteLater()
        for index, fact in enumerate(facts):
            if index:
                self._rows.addWidget(Divider())
            self._rows.addWidget(self._row(fact))
        self.setVisible(bool(facts))

    def value_of(self, name: str) -> str:
        return next((f.value for f in self.facts if f.name == name), "")

    @staticmethod
    def _row(fact: Fact) -> QWidget:
        row = QWidget()
        name = label(fact.name, "muted")
        value = label(fact.value, wrap=True)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if fact.tooltip:
            value.setToolTip(fact.tooltip)
        items: list[QWidget | int | None] = [name, None, value]
        if fact.action_text and fact.action is not None:
            action = fact.action
            items.append(button(fact.action_text, variant="link", on_click=action))
        row.setLayout(hbox(*items, spacing=10, margins=(0, 8, 0, 8)))
        return row


class AddonsCard(QFrame):
    install_requested = pyqtSignal(object)  # DownloadOption

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(label("Add-ons", "heading"))
        self.hint = label("", "caption", wrap=True)
        layout.addWidget(self.hint)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(0)
        layout.addLayout(self._rows)
        self.buttons: dict[int, QPushButton] = {}
        self._key: tuple[Any, ...] | None = None
        self.hide()

    def set_addons(self, options: list[DownloadOption], installed: InstalledGame | None, *, busy: bool) -> None:
        applied_labels = frozenset(a.casefold() for a in (installed.applied_options if installed else []))
        # The page re-renders on every download progress tick; rebuilding the rows each time would
        # drop hover and keyboard focus on the Install buttons.
        key = (tuple(options), installed is not None, applied_labels, busy)
        if key == self._key:
            return
        self._key = key
        while self._rows.count():
            item = self._rows.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.hide()
                child.deleteLater()
        self.buttons = {}
        for index, option in enumerate(options):
            if index:
                self._rows.addWidget(Divider())
            applied = option.label.casefold() in applied_labels
            self._rows.addWidget(self._row(option, installed is not None, applied, busy))
        if installed is None:
            self.hint.setText("Install the game first to add these.")
        elif busy:
            self.hint.setText("Available when the current download finishes.")
        else:
            self.hint.setText("")
        self.hint.setVisible(bool(self.hint.text()))
        self.setVisible(bool(options))

    def _row(self, option: DownloadOption, base_installed: bool, applied: bool, busy: bool) -> QWidget:
        row = QWidget()
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(option_title(option), wrap=True))
        if option.size_text:
            text.addWidget(label(option.size_text, "caption"))
        if applied:
            action: QWidget = Badge("Installed", "success")
        else:
            install = button("Install", "download", size="sm",
                             on_click=lambda o=option: self.install_requested.emit(o))
            tag_icon(install, "download")  # rows survive theme switches; let retint_icons recolour it
            install.setEnabled(base_installed and not busy)
            self.buttons[option.download_id] = install
            action = install
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(10)
        layout.addLayout(text, 1)
        layout.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
        return row


class InlineBanner(QFrame):
    """A one-line notice with an icon and an optional action button."""

    action_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.icon = QLabel()
        self.text = label("", wrap=True)
        self.action = button("Retry", "retry", size="sm", on_click=self.action_clicked.emit)
        tag_icon(self.action, "retry")
        self._kind = "warning"
        row = hbox(self.icon, self.text, self.action, spacing=10, margins=(14, 10, 12, 10))
        row.setStretch(1, 1)  # the message takes the width; the button hugs the right edge
        self.setLayout(row)
        self.hide()

    def show_message(self, text: str, *, kind: str = "warning", action_text: str = "Retry") -> None:
        self._kind = kind
        self.refresh_icon()
        self.text.setText(text)
        self.action.setText(action_text)
        self.action.setVisible(bool(action_text))
        self.show()

    def refresh_icon(self) -> None:
        pal = palette.current()
        color = {"error": pal.danger, "warning": pal.warning}.get(self._kind, pal.info)
        self.icon.setPixmap(icons.pixmap("warning" if self._kind != "info" else "info", 18, color))


class GameErrorView(QWidget):
    retry_clicked = pyqtSignal()
    back_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = label("", "title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = label("", "muted", wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setMaximumWidth(460)
        self.retry_button = button("Retry", "retry", variant="primary", on_click=self.retry_clicked.emit)
        self.back_button = button("Go back", "arrow_left", on_click=self.back_clicked.emit)
        tag_icon(self.retry_button, "retry")
        tag_icon(self.back_button, "arrow_left")
        # No stretches anywhere: an expanding row would widen the centred column and the
        # wrapped message would then get a height computed for the wrong width (clipped).
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.back_button)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addWidget(self.message, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addLayout(buttons)
        layout.setAlignment(buttons, Qt.AlignmentFlag.AlignHCenter)

    def refresh_icon(self) -> None:
        self.icon.setPixmap(icons.pixmap("warning", 48, palette.current().text_faint))

    def show_error(self, title: str, message: str, *, retry: bool = True) -> None:
        self.refresh_icon()
        self.title.setText(title)
        self.message.setText(message)
        self.retry_button.setVisible(retry)
