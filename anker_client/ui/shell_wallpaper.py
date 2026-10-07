"""Window background: the palette's ``bg`` plus an optional wallpaper.

Themes with ``Palette.wallpaper`` get the image painted behind the
(translucent) panels: scaled to cover the window, centred, then a scrim (the
palette's ``bg`` for dark themes, ``surface`` for light ones) keeps text drawn
directly on the transparent pages legible.
Animated GIFs play through ``QMovie``; repaints are throttled to ~15 fps and
the movie is paused while the window is minimised or hidden.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QElapsedTimer, QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QMovie, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from anker_client.core.paths import resource_path
from anker_client.ui.theme import palette

log = logging.getLogger(__name__)

MAX_FPS = 15
SCRIM_ALPHA_DARK = 150
SCRIM_ALPHA_LIGHT = 175


def cover_source_rect(image: QSize, target: QSize) -> QRectF:
    """The centred part of ``image`` that fills ``target`` without distortion (CSS ``cover``)."""
    if image.isEmpty() or target.isEmpty():
        return QRectF(0, 0, max(0, image.width()), max(0, image.height()))
    scale = max(target.width() / image.width(), target.height() / image.height())
    w = target.width() / scale
    h = target.height() / scale
    return QRectF((image.width() - w) / 2, (image.height() - h) / 2, w, h)


class WallpaperSurface(QWidget):
    """Central widget of the main window; paints the themed background."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._name = ""
        self._movie: QMovie | None = None
        self._still: QPixmap | None = None
        self._paused = False
        self._clock = QElapsedTimer()
        self._clock.start()
        self._frames_painted = 0

    # --- configuration -----------------------------------------------------------------------
    @property
    def wallpaper(self) -> str:
        return self._name

    @property
    def is_animated(self) -> bool:
        return self._movie is not None

    @property
    def is_playing(self) -> bool:
        return self._movie is not None and self._movie.state() == QMovie.MovieState.Running

    def set_wallpaper(self, name: str) -> None:
        """Load resource ``name`` ("" removes the wallpaper)."""
        if name == self._name:
            return
        self._unload()
        self._name = name
        if name:
            self._load(name)
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if self._movie is None:
            return
        if paused and self._movie.state() == QMovie.MovieState.Running:
            self._movie.setPaused(True)
        elif not paused:
            if self._movie.state() == QMovie.MovieState.Paused:
                self._movie.setPaused(False)
            elif self._movie.state() == QMovie.MovieState.NotRunning:
                self._movie.start()

    def _load(self, name: str) -> None:
        path = resource_path(name)
        if not path.exists():
            log.warning("Wallpaper %s not found", path)
            return
        if path.suffix.lower() == ".gif":
            movie = QMovie(str(path), parent=self)
            if movie.isValid() and movie.frameCount() != 1:
                # CacheNone (default): caching every decoded frame would cost ~75 MB for the 960×540 GIF.
                movie.frameChanged.connect(self._on_frame)
                self._movie = movie
                if not self._paused:
                    movie.start()
                else:
                    movie.jumpToFrame(0)
                return
            movie.deleteLater()
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            log.warning("Wallpaper %s could not be decoded", path)
            return
        self._still = pixmap

    def _unload(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.frameChanged.disconnect(self._on_frame)
            self._movie.deleteLater()
            self._movie = None
        self._still = None

    def _on_frame(self, _frame: int) -> None:
        if self._paused or not self.isVisible():
            return
        if self._clock.elapsed() < 1000 // MAX_FPS:
            return
        self._clock.restart()
        self.update()

    # --- painting ---------------------------------------------------------------------------------
    def _current_frame(self) -> QPixmap | None:
        if self._movie is not None:
            frame = self._movie.currentPixmap()
            return None if frame.isNull() else frame
        return self._still

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        pal = palette.current()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(pal.bg))
        frame = self._current_frame()
        if frame is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            source = cover_source_rect(frame.size(), self.size())
            p.drawPixmap(QRectF(self.rect()), frame, source)
            scrim = QColor(pal.bg if pal.dark else pal.surface)
            scrim.setAlpha(SCRIM_ALPHA_DARK if pal.dark else SCRIM_ALPHA_LIGHT)
            p.fillRect(self.rect(), scrim)
            self._frames_painted += 1
        p.end()
