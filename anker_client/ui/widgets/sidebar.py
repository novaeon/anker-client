"""Left navigation sidebar of the main window.

Layout (role=sidebar, 220 px): logo + "AnkerClient"; nav buttons Store,
Library (installed-games count badge), Downloads (unfinished-jobs badge + a
thin aggregate progress bar painted under the label); Settings pinned at the
bottom; then the account chip (initials avatar + name, or "Sign in").

The sidebar only emits intents (``navigate(key)``, ``account_clicked()``);
the main window decides what they do.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBitmap, QColor, QFont, QPainter, QPixmap, QRegion
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anker_client.constants import APP_NAME
from anker_client.core.models import UserInfo
from anker_client.core.paths import resource_path
from anker_client.ui import icons
from anker_client.ui.shell_summary import DownloadSummary
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Badge, Divider, label

SIDEBAR_WIDTH = 220
NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("store", "Store", "store"),
    ("library", "Library", "library"),
    ("downloads", "Downloads", "download"),
)
SETTINGS_ITEM = ("settings", "Settings", "settings")


def initials(name: str) -> str:
    """``"Jane Doe"`` → ``"JD"``, ``"player42"`` → ``"P"``, ``""`` → ``"?"``."""
    words = [w for w in name.replace("_", " ").replace(".", " ").split() if w[:1].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[1][0]).upper()


def load_logo(size: int) -> QPixmap:
    """The app logo cropped to its visible pixels (the PNG has generous transparent padding)."""
    path: Path = resource_path("icon.png")
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return QPixmap()
    image = pixmap.toImage()
    bounds = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
    if bounds.isValid() and not bounds.isEmpty():
        side = max(bounds.width(), bounds.height())
        cx, cy = bounds.center().x(), bounds.center().y()
        pixmap = pixmap.copy(cx - side // 2, cy - side // 2, side, side)
    dpr = 2.0
    scaled = pixmap.scaled(QSize(int(size * dpr), int(size * dpr)), Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    return scaled


class NavButton(QPushButton):
    """Checkable sidebar entry with an optional right-aligned badge and a thin progress bar."""

    def __init__(self, key: str, text: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.key = key
        self._icon_name = icon_name
        self._progress: float | None = None
        self.setProperty("variant", "nav")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(18, 18))
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.addStretch(1)
        self.badge = Badge("", "")
        self.badge.setVisible(False)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        pal = palette.current()
        self.setIcon(icons.icon(self._icon_name, pal.text if self.isChecked() else pal.text_muted))

    def nextCheckState(self) -> None:  # noqa: N802
        super().nextCheckState()
        self.refresh_icon()

    def checkStateSet(self) -> None:  # noqa: N802
        super().checkStateSet()
        self.refresh_icon()

    def set_badge(self, text: str, kind: str = "") -> None:
        self.badge.setText(text)
        self.badge.set_kind(kind)
        self.badge.setVisible(bool(text))

    def badge_text(self) -> str:
        return "" if self.badge.isHidden() else self.badge.text()

    def set_progress(self, value: float | None) -> None:
        value = None if value is None else max(0.0, min(1.0, float(value)))
        if value != self._progress:
            self._progress = value
            self.update()

    @property
    def progress(self) -> float | None:
        return self._progress

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._progress is None:
            return
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(12, self.height() - 6, self.width() - 24, 3)
        track_color = QColor(pal.text)
        track_color.setAlpha(28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_color)
        p.drawRoundedRect(track, 1.5, 1.5)
        if self._progress > 0:
            fill = QRectF(track.left(), track.top(), max(3.0, track.width() * self._progress), track.height())
            p.setBrush(QColor(pal.accent))
            p.drawRoundedRect(fill, 1.5, 1.5)
        p.end()


class Avatar(QWidget):
    """Circle with the user's initials (accent) or a generic user glyph when signed out."""

    def __init__(self, size: int = 32, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._text = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def text(self) -> str:
        return self._text

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        if self._text:
            p.setBrush(QColor(pal.accent))
            p.drawEllipse(rect)
            p.setPen(QColor(pal.accent_text))
            font = QFont(self.font())
            font.setBold(True)
            font.setPixelSize(max(9, int(self.height() * 0.38)))
            p.setFont(font)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)
        else:
            p.setBrush(QColor(pal.surface_alt))
            p.drawEllipse(rect)
            glyph = icons.pixmap("user", int(self.height() * 0.55), pal.text_muted)
            size = glyph.deviceIndependentSize()
            p.drawPixmap(int((self.width() - size.width()) / 2), int((self.height() - size.height()) / 2), glyph)
        p.end()


class AccountChip(QPushButton):
    """Sidebar account entry: avatar + name + caption. Clicking is handled by the owner."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("variant", "account")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 6, 6)
        layout.setSpacing(10)
        self.avatar = Avatar(32)
        layout.addWidget(self.avatar)
        column = QVBoxLayout()
        column.setSpacing(0)
        self.name_label = label("Sign in")
        self.name_label.setStyleSheet("font-weight: 600;")
        self.caption_label = label("AnkerGames account", "caption")
        for lbl in (self.name_label, self.caption_label):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        column.addWidget(self.name_label)
        column.addWidget(self.caption_label)
        layout.addLayout(column, 1)
        self.chevron = QLabel()
        self.chevron.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.chevron)
        self._user: UserInfo | None = None
        self.set_user(None)

    def sizeHint(self) -> QSize:  # noqa: N802
        # QPushButton ignores its child layout (and QSS min-height overrides setMinimumHeight).
        layout = self.layout()
        own = super().sizeHint()
        if layout is None:
            return own
        return layout.sizeHint().expandedTo(QSize(own.width(), 52))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    @property
    def user(self) -> UserInfo | None:
        return self._user

    def set_user(self, user: UserInfo | None) -> None:
        self._user = user
        if user is None:
            self.avatar.set_text("")
            self.name_label.setText("Sign in")
            self.caption_label.setText("AnkerGames account")
            self.setToolTip("Sign in to AnkerGames")
        else:
            name = user.display_name or user.email or "Signed in"
            self.avatar.set_text(initials(name))
            metrics = self.name_label.fontMetrics()
            self.name_label.setText(metrics.elidedText(name, Qt.TextElideMode.ElideRight, 120))
            self.caption_label.setText("Subscriber" if user.is_subscriber else "Signed in")
            self.setToolTip(f"Signed in as {name}")
        self.refresh_icons()

    def refresh_icons(self) -> None:
        name = "chevron_right" if self._user is None else "more"
        self.chevron.setPixmap(icons.pixmap(name, 16, palette.current().text_faint))
        self.avatar.update()


class Sidebar(QFrame):
    navigate = pyqtSignal(str)  # store | library | downloads | settings
    account_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(2)

        layout.addLayout(self._build_brand())
        layout.addSpacing(18)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, NavButton] = {}
        for key, text, icon_name in NAV_ITEMS:
            layout.addWidget(self._add_button(key, text, icon_name))
        layout.addStretch(1)
        layout.addWidget(self._add_button(*SETTINGS_ITEM))
        layout.addSpacing(8)
        layout.addWidget(Divider())
        layout.addSpacing(8)
        self.account = AccountChip()
        self.account.clicked.connect(lambda _checked=False: self.account_clicked.emit())
        layout.addWidget(self.account)

    def _build_brand(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(10)
        self.logo = QLabel()
        self.logo.setPixmap(load_logo(34))
        self.logo.setFixedSize(34, 34)
        row.addWidget(self.logo)
        column = QVBoxLayout()
        column.setSpacing(0)
        column.addWidget(label(APP_NAME, "brand"))
        column.addWidget(label("for AnkerGames", "caption"))
        row.addLayout(column, 1)
        return row

    def _add_button(self, key: str, text: str, icon_name: str) -> NavButton:
        btn = NavButton(key, text, icon_name)
        btn.setToolTip({"store": "Store (Ctrl+1)", "library": "Library (Ctrl+2)", "downloads": "Downloads (Ctrl+3)",
                        "settings": "Settings (Ctrl+4)"}.get(key, text))
        btn.clicked.connect(lambda _checked=False, k=key: self.navigate.emit(k))
        self._group.addButton(btn)
        self._buttons[key] = btn
        return btn

    # --- state ------------------------------------------------------------------------------
    def button(self, key: str) -> NavButton:
        return self._buttons[key]

    def current(self) -> str:
        checked = self._group.checkedButton()
        return checked.key if isinstance(checked, NavButton) else ""

    def set_current(self, key: str) -> None:
        btn = self._buttons.get(key)
        if btn is None:
            # Pages without their own entry (e.g. the game page): clear the selection.
            self._group.setExclusive(False)
            for b in self._buttons.values():
                b.setChecked(False)
            self._group.setExclusive(True)
        else:
            btn.setChecked(True)
        for b in self._buttons.values():
            b.refresh_icon()

    def set_library_count(self, count: int) -> None:
        self._buttons["library"].set_badge(str(count) if count > 0 else "", "")

    def set_downloads(self, summary: DownloadSummary) -> None:
        btn = self._buttons["downloads"]
        count = summary.unfinished
        if count:
            btn.set_badge(str(count), "accent" if summary.in_progress else "")
        elif summary.failed:
            btn.set_badge("!", "danger")
        else:
            btn.set_badge("")
        busy = summary.active or summary.waiting
        btn.set_progress(summary.progress if busy and summary.progress is not None else None)
        if summary.idle:
            btn.setToolTip("Downloads (Ctrl+3)")
        else:
            btn.setToolTip(f"Downloads (Ctrl+3)\n{summary.headline}")

    def set_user(self, user: UserInfo | None) -> None:
        self.account.set_user(user)

    def refresh_icons(self) -> None:
        for btn in self._buttons.values():
            btn.refresh_icon()
        self.account.refresh_icons()
        self.logo.setPixmap(load_logo(34))
