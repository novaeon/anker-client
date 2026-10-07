"""Game page hero banner.

Wide artwork (``hero_url`` → first screenshot → nothing) cropped to fill, a
scrim painted from the palette's ``overlay`` colour (bottom and left heavier, so
text is legible on any image and theme), and overlaid content:

* top-left: round Back button (``back_clicked``);
* bottom: cover poster, title (``role=display``), meta line
  ("2019 · 12.4 GB · v1.2 · Updated 3 days ago") and genre chips
  (``genre_clicked(name)``).

``set_compact(True)`` shrinks it for narrow windows.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget

from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button
from anker_client.ui.widgets.game_common import ChipFlow, OverlayButton, OverlayLabel, overlay_scrim
from anker_client.ui.widgets.image_label import AsyncImage


class _Scrim(QWidget):
    """Transparent layer that paints the legibility gradient and hosts the hero content."""

    def paintEvent(self, event: Any) -> None:
        pal = palette.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, pal.radius, pal.radius)
        p.setClipPath(path)
        scrim = overlay_scrim(min_alpha=170)
        clear = QColor(scrim)
        clear.setAlpha(0)
        top_tint = QColor(scrim)
        top_tint.setAlpha(scrim.alpha() // 3)
        vertical = QLinearGradient(0, 0, 0, rect.height())
        vertical.setColorAt(0.0, top_tint)
        vertical.setColorAt(0.35, clear)
        vertical.setColorAt(1.0, scrim)
        p.fillRect(rect, vertical)
        side = QColor(scrim)
        side.setAlpha(scrim.alpha() // 2)
        horizontal = QLinearGradient(0, 0, rect.width(), 0)
        horizontal.setColorAt(0.0, side)
        horizontal.setColorAt(0.6, clear)
        p.fillRect(rect, horizontal)
        p.end()


class GameHero(QFrame):
    back_clicked = pyqtSignal()
    genre_clicked = pyqtSignal(str)

    HEIGHT = 340
    COMPACT_HEIGHT = 280
    POSTER = (132, 198)
    COMPACT_POSTER = (104, 156)

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "hero")
        self._compact = False
        self._genres: list[str] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.background = AsyncImage(loader)
        self.overlay = _Scrim()
        self.back_button = OverlayButton("arrow_left", "Back", parent=self.overlay)
        self.back_button.clicked.connect(self.back_clicked)
        self.poster = AsyncImage(loader, parent=self.overlay)
        self.poster.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.title_label = OverlayLabel("", "display", parent=self.overlay)
        self.meta_label = OverlayLabel("", "", muted=True, parent=self.overlay)
        self.chips = ChipFlow(self.overlay, spacing=6)

        text = QVBoxLayout()
        text.setSpacing(6)
        text.addStretch(1)
        text.addWidget(self.title_label)
        text.addWidget(self.meta_label)
        text.addSpacing(4)
        text.addWidget(self.chips)
        bottom = QHBoxLayout()
        bottom.setSpacing(20)
        bottom.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignBottom)
        bottom.addLayout(text, 1)
        top = QHBoxLayout()
        top.addWidget(self.back_button)
        top.addStretch(1)
        content = QVBoxLayout(self.overlay)
        content.setContentsMargins(20, 18, 24, 22)
        content.setSpacing(0)
        content.addLayout(top)
        content.addStretch(1)
        content.addLayout(bottom)

        stack = QStackedLayout(self)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        stack.addWidget(self.background)
        stack.addWidget(self.overlay)
        stack.setCurrentWidget(self.overlay)
        self._apply_size()

    # --- public ------------------------------------------------------------------------
    @property
    def title(self) -> str:
        return self.title_label.text()

    @property
    def meta(self) -> str:
        return self.meta_label.text()

    @property
    def genres(self) -> list[str]:
        return list(self._genres)

    def set_game(self, *, title: str, meta: str, genres: list[str], background_url: str, poster_url: str) -> None:
        self.title_label.setText(title)
        self.meta_label.setText(meta)
        self.meta_label.setVisible(bool(meta))
        if background_url != self.background.url():
            self.background.set_image(background_url)
        if poster_url != self.poster.url():
            self.poster.set_image(poster_url, title)
        self.poster.setVisible(bool(poster_url))
        self.set_genres(genres)

    def set_genres(self, genres: list[str]) -> None:
        genres = [g for g in genres if g]
        if genres == self._genres:
            return
        self._genres = genres
        self.chips.clear()
        for name in genres[:8]:
            chip = button(name, variant="chip", tooltip=f"Browse {name} games")
            chip.clicked.connect(lambda _c=False, n=name: self.genre_clicked.emit(n))
            self.chips.add(chip)
        self.chips.setVisible(bool(genres))

    def set_compact(self, compact: bool) -> None:
        if compact != self._compact:
            self._compact = compact
            self._apply_size()

    def _apply_size(self) -> None:
        self.setFixedHeight(self.COMPACT_HEIGHT if self._compact else self.HEIGHT)
        self.poster.setFixedSize(*(self.COMPACT_POSTER if self._compact else self.POSTER))
