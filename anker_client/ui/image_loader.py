"""Shared asynchronous image loading with request de-duplication and an LRU."""

from __future__ import annotations

from collections import OrderedDict

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)


class ImageLoader(QObject):
    """Load each URL once and share decoded pixmaps between all widgets.

    Qt already limits connections per host; the extra pending map prevents the
    application from issuing the same request repeatedly when a cover appears
    in both search results and the library.  The memory cache is cost-bounded
    so browsing many games cannot grow memory indefinitely.
    """

    image_loaded = pyqtSignal(str, QPixmap)
    image_failed = pyqtSignal(str)

    MAX_CACHE_BYTES = 64 * 1024 * 1024
    MAX_DOWNLOAD_BYTES = 24 * 1024 * 1024

    def __init__(self) -> None:
        super().__init__()
        self._manager = QNetworkAccessManager(self)
        self._pending: dict[str, QNetworkReply] = {}
        self._cache: OrderedDict[str, tuple[QPixmap, int]] = OrderedDict()
        self._cache_bytes = 0

    def get(self, url: str) -> QPixmap | None:
        entry = self._cache.get(url)
        if entry is None:
            return None
        self._cache.move_to_end(url)
        return entry[0]

    def request(self, url: str) -> QPixmap | None:
        """Return a cached image or start one asynchronous request."""

        if not url:
            return None
        cached = self.get(url)
        if cached is not None:
            return cached
        if url in self._pending:
            return None

        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15_000)
        request.setRawHeader(b"Accept", b"image/avif,image/webp,image/*,*/*;q=0.8")
        reply = self._manager.get(request)
        self._pending[url] = reply
        reply.finished.connect(lambda url=url, reply=reply: self._finish(url, reply))
        return None

    def _finish(self, url: str, reply: QNetworkReply) -> None:
        self._pending.pop(url, None)
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.image_failed.emit(url)
                return

            length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
            if length is not None and int(length) > self.MAX_DOWNLOAD_BYTES:
                self.image_failed.emit(url)
                return

            data = bytes(reply.readAll())
            if not data or len(data) > self.MAX_DOWNLOAD_BYTES:
                self.image_failed.emit(url)
                return

            pixmap = QPixmap()
            if not pixmap.loadFromData(data) or pixmap.isNull():
                self.image_failed.emit(url)
                return

            self._insert(url, pixmap)
            self.image_loaded.emit(url, pixmap)
        finally:
            reply.deleteLater()

    def _insert(self, url: str, pixmap: QPixmap) -> None:
        previous = self._cache.pop(url, None)
        if previous:
            self._cache_bytes -= previous[1]

        cost = max(1, pixmap.width() * pixmap.height() * 4)
        if cost > self.MAX_CACHE_BYTES:
            return
        self._cache[url] = (pixmap, cost)
        self._cache_bytes += cost

        while self._cache_bytes > self.MAX_CACHE_BYTES and self._cache:
            _, (_, evicted_cost) = self._cache.popitem(last=False)
            self._cache_bytes -= evicted_cost


_loader: ImageLoader | None = None


def get_image_loader() -> ImageLoader:
    global _loader
    if _loader is None:
        _loader = ImageLoader()
    return _loader
