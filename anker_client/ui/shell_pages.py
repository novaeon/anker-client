"""Page construction for the main window, isolated so one broken page never takes the shell down.

``create_page`` imports the page module lazily and constructs the page with
the signature from its contract. Any exception (import error, constructor
bug) is logged and replaced by a :class:`PagePlaceholder` that says "This page
failed to load", shows the details and offers "Try again".

``call_page`` invokes optional page hooks (``on_activated``, ``set_query``…)
and logs instead of raising, for the same reason.
"""

from __future__ import annotations

import importlib
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, label

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PageSpec:
    key: str
    title: str
    module: str
    class_name: str
    takes_theme: bool = False  # SettingsPage(ctx, bridge, nav, theme); the others take the image loader


PAGE_SPECS: dict[str, PageSpec] = {
    spec.key: spec
    for spec in (
        PageSpec("store", "Store", "anker_client.ui.pages.store", "StorePage"),
        PageSpec("game", "Game details", "anker_client.ui.pages.game", "GamePage"),
        PageSpec("library", "Library", "anker_client.ui.pages.library", "LibraryPage"),
        PageSpec("downloads", "Downloads", "anker_client.ui.pages.downloads", "DownloadsPage"),
        PageSpec("settings", "Settings", "anker_client.ui.pages.settings", "SettingsPage", takes_theme=True),
    )
}
PAGE_KEYS = tuple(PAGE_SPECS)


class PagePlaceholder(QWidget):
    """Shown instead of a page whose construction failed."""

    retry_requested = pyqtSignal(str)  # page key

    def __init__(self, spec: PageSpec, exc: BaseException, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self.key = spec.key
        self.failed = True
        self.error = exc
        self.details_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        # A fixed-width centred column: wrapped labels get their height-for-width right this way.
        body = QWidget()
        body.setFixedWidth(600)
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("warning", 44, palette.current().warning))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(glyph)
        self.title_label = label("This page failed to load", "title")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title_label)
        message = label(f"The {spec.title} page ran into an error while opening. The rest of AnkerClient keeps "
                        "working — try again, or copy the details below into a bug report.", "muted", wrap=True)
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(message)

        self.details = QPlainTextEdit(self.details_text)
        self.details.setProperty("role", "mono")
        self.details.setReadOnly(True)
        self.details.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setFixedHeight(150)
        column.addSpacing(6)
        column.addWidget(self.details)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.copy_button = button("Copy details", variant="ghost", on_click=self._copy)
        actions.addWidget(self.copy_button)
        self.retry_button = button("Try again", "retry", variant="primary",
                                   on_click=lambda: self.retry_requested.emit(self.key))
        actions.addWidget(self.retry_button)
        actions.addStretch(1)
        column.addLayout(actions)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(body)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.details_text)
        self.copy_button.setText("Copied")


def create_page(spec: PageSpec, ctx: Any, bridge: Any, nav: Any, loader: Any, theme: Any) -> QWidget:
    try:
        module = importlib.import_module(spec.module)
        cls = getattr(module, spec.class_name)
        page = cls(ctx, bridge, nav, theme) if spec.takes_theme else cls(ctx, bridge, nav, loader)
        if not isinstance(page, QWidget):
            raise TypeError(f"{spec.class_name} is not a QWidget")
        return page
    except Exception as exc:
        log.exception("Could not build the %s page", spec.title)
        return PagePlaceholder(spec, exc)


def is_placeholder(page: QWidget | None) -> bool:
    return isinstance(page, PagePlaceholder)


def call_page(page: QWidget | None, method: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``page.method(*args)`` if it exists; log (never raise) when it fails."""
    if page is None or is_placeholder(page):
        return None
    fn: Callable[..., Any] | None = getattr(page, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("%s.%s failed", type(page).__name__, method)
        return None
