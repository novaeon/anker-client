"""Asynchronous image loading for widgets (disk cache → decode off-thread → LRU of QPixmaps).

Usage::

    pixmap = ctx_loader.request(url, QSize(200, 300))   # immediate hit or None
    loader.loaded.connect(lambda url, size_key: ...)     # then call request() again

Widgets connect once to ``loaded`` and re-request when ``url`` matches the one
they display. Requests are de-duplicated; decoding (and down-scaling with
``KeepAspectRatioByExpanding`` + smooth transform) happens on a worker thread
using ``QImage`` (thread-safe); conversion to ``QPixmap`` happens on the GUI
thread. The memory cache is bounded by total pixel bytes.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPixmap

from anker_client.core.tasks import CancelToken, TaskRunner
from anker_client.services.images import ImageCache
from anker_client.ui.async_ import run_async

log = logging.getLogger(__name__)


def _size_key(size: QSize | None) -> str:
    return f"{size.width()}x{size.height()}" if size is not None and size.isValid() else "orig"


def _decode(path: str, target: QSize | None) -> QImage:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    if target is not None and target.isValid():
        original = reader.size()
        if original.isValid() and (original.width() > target.width() * 2 or original.height() > target.height() * 2):
            # Cheap pre-scale while decoding (JPEG supports it natively) to ~2× target.
            scaled = original.scaled(target * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"Could not decode image: {reader.errorString()}")
    if target is not None and target.isValid():
        image = image.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if image.width() > target.width() or image.height() > target.height():
            x = max(0, (image.width() - target.width()) // 2)
            y = max(0, (image.height() - target.height()) // 2)
            image = image.copy(x, y, min(target.width(), image.width()), min(target.height(), image.height()))
    return image


class ImageLoader(QObject):
    loaded = pyqtSignal(str, str)  # url, size key
    failed = pyqtSignal(str)  # url

    MAX_CACHE_BYTES = 160 * 1024 * 1024

    def __init__(self, cache: ImageCache, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._runner = runner
        self._pixmaps: OrderedDict[tuple[str, str], QPixmap] = OrderedDict()
        self._bytes = 0
        self._pending: set[tuple[str, str]] = set()
        self._failed: set[str] = set()

    def request(self, url: str, size: QSize | None = None) -> QPixmap | None:
        """Cached pixmap (cropped to ``size`` when given) or None; starts loading on a miss."""
        if not url:
            return None
        key = (url, _size_key(size))
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            self._pixmaps.move_to_end(key)
            return pixmap
        if key in self._pending or url in self._failed:
            return None
        self._pending.add(key)
        target = QSize(size) if size is not None else None

        def work(*, token: CancelToken) -> QImage:
            path = self._cache.fetch(url, token=token)
            token.raise_if_cancelled()
            return _decode(str(path), target)

        run_async(
            self,
            self._runner,
            work,
            on_result=lambda image, key=key: self._store(key, image),
            on_error=lambda exc, key=key: self._fail(key, exc),
        )
        return None

    def is_failed(self, url: str) -> bool:
        return url in self._failed

    def retry_failed(self) -> None:
        self._failed.clear()

    def clear(self) -> None:
        self._pixmaps.clear()
        self._bytes = 0

    # --- internals ----------------------------------------------------------------------
    def _store(self, key: tuple[str, str], image: QImage) -> None:
        self._pending.discard(key)
        pixmap = QPixmap.fromImage(image)
        cost = max(1, pixmap.width() * pixmap.height() * 4)
        self._pixmaps[key] = pixmap
        self._bytes += cost
        while self._bytes > self.MAX_CACHE_BYTES and len(self._pixmaps) > 1:
            _, old = self._pixmaps.popitem(last=False)
            self._bytes -= max(1, old.width() * old.height() * 4)
        self.loaded.emit(key[0], key[1])

    def _fail(self, key: tuple[str, str], exc: BaseException) -> None:
        self._pending.discard(key)
        self._failed.add(key[0])
        log.debug("Image %s failed: %s", key[0], exc)
        self.failed.emit(key[0])
