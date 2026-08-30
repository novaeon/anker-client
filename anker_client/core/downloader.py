"""Cancellable, throttled file downloads for the shared task pool."""

from __future__ import annotations

import os
import threading
import time

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class DownloadSignals(QObject):
    progress = pyqtSignal("qint64", "qint64", float)
    completed = pyqtSignal(str)
    cancelled = pyqtSignal()
    error = pyqtSignal(str)
    finished = pyqtSignal()


class DownloadTask(QRunnable):
    """Stream a URL to a temporary file, then atomically publish it."""

    CHUNK_SIZE = 1024 * 512
    PROGRESS_INTERVAL_SECONDS = 0.10

    def __init__(self, session, url: str, dest_path: str):
        super().__init__()
        self.signals = DownloadSignals()
        self._session = session
        self._url = url
        self._dest = dest_path
        self._partial = f"{dest_path}.part"
        self._cancel_event = threading.Event()
        self._response_lock = threading.Lock()
        self._response = None

    def cancel(self) -> None:
        self._cancel_event.set()
        # Closing a response usually interrupts a blocked socket read.  The
        # event remains the source of truth if a platform delays that close.
        with self._response_lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def run(self) -> None:
        try:
            directory = os.path.dirname(self._dest)
            if directory:
                os.makedirs(directory, exist_ok=True)

            response = self._session.get(
                self._url,
                stream=True,
                timeout=(10, 30),
            )
            with self._response_lock:
                self._response = response
            response.raise_for_status()

            total = int(response.headers.get("Content-Length", 0) or 0)
            done = 0
            started = time.monotonic()
            last_progress = started - self.PROGRESS_INTERVAL_SECONDS

            with open(self._partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._cancel_event.is_set():
                        raise _DownloadCancelled
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)

                    now = time.monotonic()
                    if (
                        now - last_progress >= self.PROGRESS_INTERVAL_SECONDS
                        or (total and done >= total)
                    ):
                        elapsed = max(now - started, 0.001)
                        self.signals.progress.emit(done, total, done / elapsed)
                        last_progress = now

                handle.flush()
                os.fsync(handle.fileno())

            if self._cancel_event.is_set():
                raise _DownloadCancelled
            if total and done != total:
                raise OSError(
                    f"Download ended early ({done} of {total} bytes received)."
                )
            os.replace(self._partial, self._dest)
            self.signals.completed.emit(self._dest)
        except _DownloadCancelled:
            self._remove_partial()
            self.signals.cancelled.emit()
        except Exception as exc:
            self._remove_partial()
            if self._cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.error.emit(str(exc).strip() or type(exc).__name__)
        finally:
            with self._response_lock:
                response = self._response
                self._response = None
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self.signals.finished.emit()

    def _remove_partial(self) -> None:
        try:
            os.remove(self._partial)
        except OSError:
            pass


class _DownloadCancelled(Exception):
    pass
