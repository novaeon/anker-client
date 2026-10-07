"""Small helpers shared by the Library page, the Downloads page and the library dialogs.

* :func:`tokenless` adapts a service method without a ``token`` keyword for
  :func:`anker_client.ui.async_.run_async` (which always passes one).
* :func:`confirm` shows a themed confirmation box with a named destructive
  button ("Uninstall", "Cancel download") — callers name the game in ``text``.
* :class:`IconTinter` remembers which icon each button shows so icons can be
  re-tinted when the theme changes (icons are rendered in a fixed colour).
* :func:`open_url` / :func:`format_date` are tiny conveniences.
* :class:`ElidedLabel` / :class:`ElidedLink` (single-line labels that elide
  to the available width; the link is clickable), :class:`SearchIcon`
  (magnifier inside a search box) and :func:`widen_empty_state`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QMessageBox, QSizePolicy, QWidget

from anker_client.core.tasks import CancelToken
from anker_client.ui import icons
from anker_client.ui.theme import palette

log = logging.getLogger(__name__)
T = TypeVar("T")


def tokenless(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Callable[..., T]:
    """Wrap ``fn(*args, **kwargs)`` as a ``run_async`` callable that accepts (and honours) ``token``."""

    def run(*, token: CancelToken) -> T:
        token.raise_if_cancelled()
        return fn(*args, **kwargs)

    run.__qualname__ = getattr(fn, "__qualname__", "call")
    return run


def confirm(
    parent: QWidget | None,
    *,
    title: str,
    text: str,
    informative: str = "",
    confirm_text: str = "OK",
    cancel_text: str = "Cancel",
    destructive: bool = True,
) -> bool:
    """Modal confirmation. Returns True only when the confirm button was clicked."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    role = QMessageBox.ButtonRole.DestructiveRole if destructive else QMessageBox.ButtonRole.AcceptRole
    ok = box.addButton(confirm_text, role)
    if destructive:
        ok.setProperty("variant", "danger")
        _repolish(ok)  # the message box already polished its buttons: re-apply the QSS
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    result = clicked is not None and clicked is ok
    box.deleteLater()
    return result


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def open_url(url: str) -> bool:
    if not url:
        return False
    return QDesktopServices.openUrl(QUrl(url))


def format_date(iso: str, *, unknown: str = "—") -> str:
    """ISO date/datetime → ``"01 Sep 2026"`` in local time."""
    if not iso:
        return unknown
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone().strftime("%d %b %Y")


# --- icon tinting -----------------------------------------------------------------------

#: Palette attribute used to tint an icon, by "tint" name.
_TINTS = {
    "text": "text",
    "muted": "text_muted",
    "faint": "text_faint",
    "on_accent": "accent_text",
    "accent": "accent",
    "danger": "danger",
    "warning": "warning",
    "success": "success",
}


def tint_color(tint: str) -> str:
    return getattr(palette.current(), _TINTS.get(tint, "text"))


class IconTinter(QObject):
    """Keeps button icons in the current theme's colours.

    ``set(button, "play", tint="on_accent")`` applies the icon now and again
    whenever the application palette/stylesheet changes (detected through the
    ``watch`` widget's change events).
    """

    def __init__(self, watch: QWidget) -> None:
        super().__init__(watch)
        self._entries: list[tuple[QAbstractButton, str, str, QSize | None]] = []
        self._pending = False
        self._callbacks: list[Callable[[], None]] = []
        watch.installEventFilter(self)

    def set(self, button: QAbstractButton, name: str, *, tint: str = "text", size: int | None = None) -> None:
        self._entries = [e for e in self._entries if e[0] is not button and not sip.isdeleted(e[0])]
        icon_size = QSize(size, size) if size else None
        self._entries.append((button, name, tint, icon_size))
        self._apply(button, name, tint, icon_size)

    def on_theme_changed(self, callback: Callable[[], None]) -> None:
        """Also run ``callback`` after a theme change (e.g. to repaint empty-state icons)."""
        self._callbacks.append(callback)

    def retint(self) -> None:
        self._pending = False
        alive = []
        for entry in self._entries:
            if sip.isdeleted(entry[0]):
                continue
            alive.append(entry)
            self._apply(*entry)
        self._entries = alive
        for callback in list(self._callbacks):
            try:
                callback()
            except Exception:  # a broken callback must not break theming
                log.exception("Theme callback failed")

    @staticmethod
    def _apply(button: QAbstractButton, name: str, tint: str, size: QSize | None) -> None:
        if not name:
            button.setIcon(QIcon())
            return
        button.setIcon(icons.icon(name, tint_color(tint)))
        if size is not None:
            button.setIconSize(size)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: N802
        if event is not None and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) \
                and not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._retint_if_alive)
        return False

    def _retint_if_alive(self) -> None:
        if not sip.isdeleted(self):
            self.retint()


# --- small widgets ------------------------------------------------------------------------


class ElidedLabel(QLabel):
    """Single-line label that elides its text to the available width (full text in the tooltip).

    It asks for its full text width but accepts being squeezed, so it can sit next to a
    badge in a row ending with a stretch: the badge stays right after the visible text.
    """

    def __init__(self, text: str = "", role: str = "", parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        if role:
            self.setProperty("role", role)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        text = text or ""
        if text == self._full and self.text():
            return
        self._full = text
        self.setToolTip(self._full)
        self.updateGeometry()
        self._elide()

    def sizeHint(self) -> QSize:  # noqa: N802
        margins = self.contentsMargins()
        width = self.fontMetrics().horizontalAdvance(self._full) + margins.left() + margins.right() + 2
        return QSize(width, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(min(40, self.sizeHint().width()), super().minimumSizeHint().height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: QEvent | None) -> None:  # noqa: N802
        super().changeEvent(event)
        if event is not None and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()  # the theme's font changes the text width
            self._elide()

    def _elide(self) -> None:
        width = max(10, self.width())
        elided = self.fontMetrics().elidedText(self._full, self._mode, width)
        if elided != self.text():
            self.setText(elided)


class ElidedLink(ElidedLabel):
    """Single-line link-styled label that elides long text (paths) and emits ``clicked``."""

    clicked = pyqtSignal()

    def __init__(self, text: str = "", parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle) -> None:
        super().__init__(text, "link", parent, mode=mode)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event is not None and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class SearchIcon(QLabel):
    """Magnifier drawn inside a ``QLineEdit[role="search"]`` (whose QSS reserves 34 px on the left)."""

    def __init__(self, edit: QLineEdit) -> None:
        super().__init__(edit)
        self._edit = edit
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(16, 16)
        self.refresh()
        edit.installEventFilter(self)
        self._place()

    def refresh(self) -> None:
        self.setPixmap(icons.pixmap("search", 16, palette.current().text_faint))

    def _place(self) -> None:
        self.move(13, max(0, (self._edit.height() - self.height()) // 2))

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: N802
        if obj is self._edit and event is not None and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place()
        return False


def widen_empty_state(widget: QWidget, width: int = 340) -> None:
    """Give word-wrapped labels inside an ``EmptyState`` a sensible width.

    ``EmptyState`` centres its message with ``AlignHCenter``, which makes a
    wrapped label shrink to its minimum width (one or two words per line).
    """
    for lbl in widget.findChildren(QLabel):
        if lbl.wordWrap():
            lbl.setMinimumWidth(width)


class Connections:
    """Remembers signal connections so a page can drop them all in ``shutdown()``."""

    def __init__(self) -> None:
        self._items: list[tuple[Any, Callable[..., Any]]] = []

    def connect(self, signal: Any, slot: Callable[..., Any]) -> None:
        signal.connect(slot)
        self._items.append((signal, slot))

    def disconnect_all(self) -> None:
        for signal, slot in self._items:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):  # already disconnected / sender destroyed
                pass
        self._items.clear()
