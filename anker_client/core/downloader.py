# anker_client/core/downloader.py
import os
import time
from PyQt6.QtCore import QThread, pyqtSignal


class DownloadThread(QThread):
    """
    Worker thread that streams a file download.
    Emits:
        progress(bytes_done, total_bytes, speed_bps)
        finished(local_path)
        error(message)
    """
    progress = pyqtSignal('qint64', 'qint64', float)   # done, total, bytes/sec
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, session, url: str, dest_path: str):
        super().__init__()
        self._session = session
        self._url = url
        self._dest = dest_path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._dest), exist_ok=True)
            resp = self._session.get(self._url, stream=True, timeout=30)
            resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            chunk_size = 1024 * 256  # 256 KB
            start_time = time.time()

            with open(self._dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if self._cancelled:
                        return
                    f.write(chunk)
                    done += len(chunk)
                    elapsed = time.time() - start_time or 0.001
                    speed = done / elapsed
                    self.progress.emit(done, total, speed)

            self.finished.emit(self._dest)
        except Exception as e:
            self.error.emit(str(e))
