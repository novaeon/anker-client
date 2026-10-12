"""Achievement widgets themed with the app palette: the Library summary block and list rows.

* :class:`AchievementSummary` — "ACHIEVEMENTS · 5 / 9" with a progress bar, the
  most recently unlocked icons (+N) and a "View all" link (``view_requested``).
* :class:`AchievementRow` — one entry of the achievements window: colour icon
  when unlocked, greyed icon when locked; name, description, progress bar for
  progress achievements, unlock date (Steam's "Unlocked 7 Oct, 2026 @ 1:58am")
  and global rarity with a "Rare" badge under 10 %.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from anker_client.core.models import Achievement, GameAchievements
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.common import Badge, button, label
from anker_client.ui.widgets.image_label import AsyncImage

STRIP_ICONS = 6
STRIP_SIZE = 36


def sort_like_steam(achievements: list[Achievement]) -> list[Achievement]:
    """Unlocked first (newest first), then locked from most to least common."""
    unlocked = sorted((a for a in achievements if a.unlocked), key=lambda a: -(a.unlocked_at or 0))
    locked = sorted((a for a in achievements if not a.unlocked),
                    key=lambda a: -(a.global_percent if a.global_percent is not None else -1))
    return unlocked + locked


def format_unlock_time(timestamp: int) -> str:
    """Steam's style: ``Unlocked 7 Oct, 2026 @ 1:58am`` (local time)."""
    if not timestamp:
        return "Unlocked"
    moment = datetime.fromtimestamp(timestamp)
    hour = moment.hour % 12 or 12
    return f"Unlocked {moment.day} {moment:%b}, {moment.year} @ {hour}:{moment:%M}{'am' if moment.hour < 12 else 'pm'}"


def format_percent(value: float | None) -> str:
    if value is None:
        return ""
    return "<0.1%" if value < 0.1 else f"{value:.1f}%"


class AchievementSummary(QWidget):
    view_requested = pyqtSignal()

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        self._loader = loader
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(label("ACHIEVEMENTS", "caption"))
        head.addStretch(1)
        self.count_label = label("", "muted")
        head.addWidget(self.count_label)
        col.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setRange(0, 100)
        col.addWidget(self.bar)
        self.message = label("", "caption", wrap=True)
        col.addWidget(self.message)
        strip = QHBoxLayout()
        strip.setSpacing(6)
        self._icons: list[AsyncImage] = []
        for _ in range(STRIP_ICONS):
            image = AsyncImage(loader, radius=3)
            image.setFixedSize(STRIP_SIZE, STRIP_SIZE)
            image.hide()
            self._icons.append(image)
            strip.addWidget(image)
        self.more_label = label("", "muted")
        strip.addWidget(self.more_label)
        strip.addStretch(1)
        self.view_button = button("View all", variant="link", on_click=self.view_requested.emit)
        self.view_button.setObjectName("action_achievements")
        strip.addWidget(self.view_button)
        col.addLayout(strip)
        self.set_data(None)

    def set_loading(self) -> None:
        self.count_label.setText("")
        self.bar.setValue(0)
        self.message.setText("Loading achievements…")
        self.message.show()
        for image in self._icons:
            image.hide()
        self.more_label.setText("")
        self.view_button.hide()

    def set_data(self, data: GameAchievements | None) -> None:
        """Show ``data``; the caller hides the whole block when there is none."""
        for image in self._icons:
            image.hide()
        self.more_label.setText("")
        if data is None:
            self.count_label.setText("")
            self.bar.setValue(0)
            self.message.hide()
            self.view_button.hide()
            return
        unlocked = [a for a in sort_like_steam(data.achievements) if a.unlocked]
        if data.schema_available and data.total:
            self.count_label.setText(f"{data.unlocked_count} / {data.total}")
            self.bar.setValue(data.percent)
            self.bar.show()
        else:
            self.count_label.setText(f"{len(unlocked)} unlocked")
            self.bar.hide()
        self.message.setText("" if unlocked else "No achievements unlocked yet.")
        self.message.setVisible(not unlocked)
        for image, achievement in zip(self._icons, unlocked, strict=False):
            image.set_image(achievement.icon_url, placeholder=achievement.display_name)
            image.setToolTip(achievement.display_name)
            image.show()
        extra = len(unlocked) - STRIP_ICONS
        self.more_label.setText(f"+{extra}" if extra > 0 else "")
        self.view_button.show()


class AchievementRow(QFrame):
    ICON = 64

    def __init__(self, achievement: Achievement, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.achievement = achievement
        self.setProperty("role", "card")
        self.setObjectName(f"achievement_{achievement.api_name}")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 14, 10)
        row.setSpacing(14)
        self.icon = AsyncImage(loader, radius=3)
        self.icon.setFixedSize(self.ICON, self.ICON)
        url = achievement.icon_url if achievement.unlocked else (achievement.icon_gray_url or achievement.icon_url)
        self.icon.set_image(url, placeholder=achievement.display_name)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        self.name_label = label(achievement.display_name, "heading", wrap=True)
        text.addWidget(self.name_label)
        description = achievement.description or ("Hidden achievement" if achievement.hidden else "")
        self.description_label = label(description, "muted", wrap=True)
        self.description_label.setVisible(bool(description))
        text.addWidget(self.description_label)
        self.progress_bar: QProgressBar | None = None
        if achievement.has_progress:
            bar_row = QHBoxLayout()
            bar_row.setSpacing(8)
            self.progress_bar = QProgressBar()
            self.progress_bar.setTextVisible(False)
            self.progress_bar.setFixedHeight(6)
            self.progress_bar.setRange(0, achievement.max_progress or 1)
            self.progress_bar.setValue(min(achievement.progress or 0, achievement.max_progress or 0))
            bar_row.addWidget(self.progress_bar, 1)
            bar_row.addWidget(label(f"{achievement.progress or 0} / {achievement.max_progress}", "caption"))
            text.addLayout(bar_row)
        text.addStretch(1)
        row.addLayout(text, 1)

        side = QVBoxLayout()
        side.setSpacing(4)
        side.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.status_label = label(format_unlock_time(achievement.unlocked_at) if achievement.unlocked else "",
                                  "caption")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.status_label.setVisible(achievement.unlocked)
        side.addWidget(self.status_label)
        rarity = QHBoxLayout()
        rarity.setSpacing(6)
        rarity.addStretch(1)
        self.rare_badge: Badge | None = None
        if achievement.rare:
            self.rare_badge = Badge("Rare", "warning")
            rarity.addWidget(self.rare_badge)
        self.percent_label = label(f"{format_percent(achievement.global_percent)} of players"
                                   if achievement.global_percent is not None else "", "caption")
        self.percent_label.setVisible(achievement.global_percent is not None)
        rarity.addWidget(self.percent_label)
        side.addLayout(rarity)
        row.addLayout(side)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if not achievement.unlocked:
            self.setProperty("locked", True)
            for widget in (self.name_label,):
                widget.setProperty("role", "muted")


def hidden_summary(count: int) -> QLabel:
    text = (f"<b>{count} hidden achievement{'s' if count != 1 else ''} remaining</b><br>"
            "Details for each achievement will be revealed once unlocked")
    lbl = label(text, "muted", wrap=True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl
