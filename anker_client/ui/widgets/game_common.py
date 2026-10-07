"""Helpers shared by the Store and Game pages.

* ``open_url`` / ``copy_text`` / ``confirm`` — thin wrappers around Qt desktop
  services so tests can monkeypatch one place (never open a real browser or a
  blocking dialog from a test).
* ``no_token`` — adapts a token-less service call to ``run_async``.
* Text helpers: ``genre_slug``, ``display_version``, ``format_date``,
  ``relative_day``.
* Overlay widgets for text and buttons drawn over artwork (hero banners,
  screenshots, the lightbox). They paint with colours derived from the active
  palette (``overlay`` scrim, light foreground) so they stay legible in light
  *and* dark themes, where the regular QSS text colour would not be.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import QAbstractButton, QLabel, QMessageBox, QSizePolicy, QWidget

from anker_client.core.formatting import normalize_version
from anker_client.core.tasks import CancelToken
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.flow_layout import FlowWidget

log = logging.getLogger(__name__)


# --- desktop services -------------------------------------------------------------------


def open_url(url: str) -> bool:
    """Open ``url`` in the user's browser."""
    if not url:
        return False
    return bool(QDesktopServices.openUrl(QUrl(url)))


def copy_text(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def confirm(parent: QWidget | None, title: str, text: str, accept_label: str, reject_label: str = "Cancel") -> bool:
    """Blocking yes/no question with explicit button labels; True when accepted."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    accept = box.addButton(accept_label, QMessageBox.ButtonRole.AcceptRole)
    reject = box.addButton(reject_label, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(reject)
    box.exec()
    return box.clickedButton() is accept


def no_token(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Callable[..., Any]:
    """Wrap a call that takes no ``token`` so it can run through ``run_async``."""

    def run(*, token: CancelToken) -> Any:
        token.raise_if_cancelled()
        return fn(*args, **kwargs)

    run.__qualname__ = getattr(fn, "__qualname__", "call")
    return run


# --- text helpers -------------------------------------------------------------------------


def genre_slug(name: str) -> str:
    """``"Open World"`` → ``"open-world"`` (the site's ``/genre/{slug}`` form)."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def display_version(version: str) -> str:
    """``"V 1.5"`` / ``"1.5"`` → ``"v1.5"``; non-numeric versions are returned trimmed."""
    norm = normalize_version(version)
    if not norm:
        return ""
    return f"v{norm}" if norm[0].isdigit() else version.strip()


def _parse_day(iso: str) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(iso[:10])
        except ValueError:
            return None


def format_date(iso: str) -> str:
    """``"2017-03-14"`` → ``"14 Mar 2017"``; unparsable input is returned unchanged."""
    day = _parse_day(iso)
    return f"{day.day} {day:%b %Y}" if day else iso


def relative_day(iso: str, *, today: date | None = None) -> str:
    """Day-granular relative date: "today", "yesterday", "3 days ago", "2 months ago", or the date."""
    day = _parse_day(iso)
    if day is None:
        return iso
    days = ((today or date.today()) - day).days
    if days < 0:
        return format_date(iso)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    return format_date(iso)


# --- overlay colours ------------------------------------------------------------------------

_RGBA_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")


def parse_color(text: str, fallback: QColor | None = None) -> QColor:
    """Parse ``#rrggbb``/names and CSS ``rgba(r, g, b, a)`` (a in 0..1)."""
    match = _RGBA_RE.fullmatch(text.strip())
    if match:
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        alpha = float(match.group(4)) if match.group(4) is not None else 1.0
        return QColor(r, g, b, max(0, min(255, round(alpha * 255))))
    color = QColor(text)
    if color.isValid():
        return color
    return QColor(fallback) if fallback is not None else QColor(0, 0, 0, 180)


def overlay_scrim(min_alpha: int = 0) -> QColor:
    """The palette's scrim colour (used behind text drawn over artwork)."""
    color = parse_color(palette.current().overlay, QColor(0, 0, 0, 180))
    if color.alpha() < min_alpha:
        color.setAlpha(min_alpha)
    return color


def overlay_text(muted: bool = False) -> QColor:
    """A light foreground for text over a scrim, in every theme."""
    pal = palette.current()
    base = QColor(pal.text if pal.dark else pal.surface)
    if muted:
        base.setAlpha(205)
    return base


class OverlayButton(QAbstractButton):
    """Round, translucent icon button painted over artwork (hero, screenshots, lightbox)."""

    def __init__(self, icon_name: str, tooltip: str = "", *, diameter: int = 36,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._diameter = diameter
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or icon_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(diameter, diameter)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._diameter, self._diameter)

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        scrim = overlay_scrim(min_alpha=150)
        if not self.isEnabled():
            scrim.setAlpha(scrim.alpha() // 2)
        elif self.isDown():
            scrim = scrim.darker(130)
        elif self.underMouse() or self.hasFocus():
            scrim.setAlpha(min(255, scrim.alpha() + 50))
        path = QPainterPath()
        path.addEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        p.fillPath(path, scrim)
        if self.hasFocus():
            p.setPen(QColor(palette.current().accent))
            p.drawPath(path)
        fg = overlay_text(muted=not self.isEnabled())
        size = max(12, int(self._diameter * 0.5))
        pm = icons.pixmap(self._icon_name, size, fg)
        x = (self.width() - size) / 2
        y = (self.height() - size) / 2
        p.drawPixmap(QRectF(x, y, size, size), pm, QRectF(pm.rect()))
        p.end()


class OverlayLabel(QLabel):
    """Plain-text label painted in a light overlay colour with a soft shadow.

    Font, size and wrapping come from QSS (``role``) like any label; only the
    colour is overridden at paint time, so a theme switch just needs a repaint.
    """

    def __init__(self, text: str = "", role: str = "", *, muted: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._muted = muted
        if role:
            self.setProperty("role", role)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def paintEvent(self, event: Any) -> None:
        if not self.text():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self.font())
        rect = self.contentsRect()
        flags = int(self.alignment())
        if self.wordWrap():
            flags |= int(Qt.TextFlag.TextWordWrap)
        p.setPen(QColor(0, 0, 0, 150))
        p.drawText(rect.translated(0, 1), flags, self.text())
        p.setPen(overlay_text(self._muted))
        p.drawText(rect, flags, self.text())
        p.end()


class ChipFlow(FlowWidget):
    """``FlowWidget`` for chips that always lays every chip out.

    ``FlowLayout`` skips children that are not visible during a layout pass, and
    children added to a visible parent are only shown by a queued call — so a
    chip could keep its default 640×480 geometry and cover its neighbours. Here
    chips are shown synchronously and the layout is re-run after every change
    and after this widget is shown.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8) -> None:
        super().__init__(parent, spacing=spacing)
        self._relayout_pending = False

    def add(self, widget: QWidget) -> None:
        super().add(widget)
        widget.show()
        self._schedule_relayout()

    def clear(self) -> None:
        """Remove every chip *now* (``FlowLayout.clear`` only schedules ``deleteLater``)."""
        layout = self.flow
        while layout.count():
            item = layout.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.hide()
                child.deleteLater()
        self._schedule_relayout()

    def chips(self) -> list[QWidget]:
        items = (self.flow.itemAt(i) for i in range(self.flow.count()))
        return [w for w in (item.widget() for item in items if item is not None) if w is not None]

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._schedule_relayout()

    def _schedule_relayout(self) -> None:
        if not self._relayout_pending:
            self._relayout_pending = True
            QTimer.singleShot(0, self._relayout)

    def _relayout(self) -> None:
        self._relayout_pending = False
        self.flow.invalidate()
        self.flow.setGeometry(self.rect())
        self.updateGeometry()


# --- live theme switching ------------------------------------------------------------------------

_ICON_PROPERTY = "icon_name"


def tag_icon(widget: QAbstractButton, icon_name: str) -> QAbstractButton:
    """Remember ``widget``'s icon name so :func:`retint_icons` can recolour it after a theme switch."""
    widget.setProperty(_ICON_PROPERTY, icon_name)
    return widget


def retint_icons(root: QWidget) -> None:
    """Re-create the icons of every tagged button under ``root`` in the current palette."""
    pal = palette.current()
    for btn in root.findChildren(QAbstractButton):
        name = btn.property(_ICON_PROPERTY)
        if not name:
            continue
        variant = btn.property("variant") or ""
        color = pal.accent_text if variant in ("primary", "success") else (pal.accent if variant == "link" else None)
        btn.setIcon(icons.icon(str(name), color))


class ThemeWatcher(QObject):
    """Calls ``callback`` once whenever the active palette changes (debounced).

    ``ThemeManager.apply`` changes the application palette and stylesheet; every
    widget then receives palette/style change events. Pages that bake palette
    colours into icons or pixmaps use this to refresh them.
    """

    _EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange, QEvent.Type.StyleChange)

    def __init__(self, widget: QWidget, callback: Callable[[], None]) -> None:
        super().__init__(widget)
        self._key = palette.current().key
        self._callback = callback
        self._pending = False
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if event is not None and event.type() in self._EVENTS and not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._check)
        return False

    def _check(self) -> None:
        self._pending = False
        key = palette.current().key
        if key != self._key:
            self._key = key
            self._callback()
