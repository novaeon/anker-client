"""The ``.part`` file of a download: preallocation, positional writes, disk-error mapping.

* One handle per transfer, shared by every connection; ``write_at`` holds a
  lock around seek + write (writes land in the OS cache, so the critical
  section is a memcpy). After ``close()`` further writes raise
  :class:`PartFileClosed`, so a straggling connection thread can never touch
  the file once ``download()`` has returned.
* On Windows the file is flagged *sparse* before it is extended to its full
  size, and it is extended with ``SetEndOfFile`` (``FileIO.truncate`` goes
  through the CRT's ``_chsize_s``, which writes zeros explicitly — minutes for
  a 100 GB archive). Without the sparse flag NTFS/exFAT zero-fill everything
  below an offset the first time a later segment writes there, so callers use
  a single, sequential connection when ``sparse`` is False.
* No fsync while downloading; ``finish`` flushes and fsyncs once.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
from typing import Any, BinaryIO

from anker_client.core.errors import AnkerError, DiskSpaceError, DownloadError

log = logging.getLogger(__name__)

_FSCTL_SET_SPARSE = 0x000900C4
_FILE_ATTRIBUTE_SPARSE_FILE = 0x200
_FILE_BEGIN = 0
_WINERROR_DISK_FULL = frozenset({39, 112})  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL


class PartFileClosed(Exception):
    """A write arrived after the transfer closed the file (internal)."""


class PartFile:
    def __init__(self, path: str, handle: BinaryIO, *, sparse: bool = True) -> None:
        self.path = path
        #: False when out-of-order writes would make the file system zero-fill (see module doc).
        self.sparse = sparse
        self._fh = handle
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def create(cls, path: str, size: int | None) -> PartFile:
        """Create/truncate ``path`` and extend it to ``size`` (sparse where supported)."""
        handle = open(path, "w+b", buffering=0)  # noqa: SIM115 - owned by the PartFile
        try:
            sparse = _make_sparse(handle) if os.name == "nt" else True
            if not sparse:
                log.info("%s: the volume does not support sparse files", path)
            if size:
                _set_length(handle, size)
        except BaseException:
            handle.close()
            raise
        return cls(path, handle, sparse=sparse)

    @classmethod
    def open_existing(cls, path: str) -> PartFile:
        handle = open(path, "r+b", buffering=0)  # noqa: SIM115 - owned by the PartFile
        sparse = True
        if os.name == "nt":
            attributes = getattr(os.fstat(handle.fileno()), "st_file_attributes", _FILE_ATTRIBUTE_SPARSE_FILE)
            sparse = bool(attributes & _FILE_ATTRIBUTE_SPARSE_FILE)
        return cls(path, handle, sparse=sparse)

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        """Write all of ``data`` at ``offset`` (raises ``OSError`` or :class:`PartFileClosed`)."""
        view = memoryview(data)
        with self._lock:
            if self._closed:
                raise PartFileClosed()
            self._fh.seek(offset)
            while view:
                written = self._fh.write(view)
                if not written:
                    raise OSError(errno.EIO, "write returned 0 bytes")
                view = view[written:]

    def finish(self, size: int) -> None:
        """Set the final length to ``size``, flush to stable storage and close."""
        with self._lock:
            if self._closed:
                raise PartFileClosed()
            if os.fstat(self._fh.fileno()).st_size != size:
                _set_length(self._fh, size)
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._closed = True
            self._fh.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fh.close()
            except OSError as exc:
                log.warning("Closing %s failed: %s", self.path, exc)


def _set_length(handle: BinaryIO, size: int) -> None:
    """Set the file length without writing zeros (raises ``OSError``)."""
    if os.name != "nt":
        handle.truncate(size)
        return
    import ctypes
    import msvcrt

    kernel32 = _kernel32()
    os_handle = msvcrt.get_osfhandle(handle.fileno())
    if not kernel32.SetFilePointerEx(os_handle, size, None, _FILE_BEGIN) or not kernel32.SetEndOfFile(os_handle):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


_KERNEL32: Any = None


def _kernel32() -> Any:
    """kernel32 with the prototypes the part file needs (built once)."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
        ]
        kernel32.SetFilePointerEx.restype = wintypes.BOOL
        kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
        kernel32.SetEndOfFile.restype = wintypes.BOOL
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    return _KERNEL32


def _make_sparse(handle: BinaryIO) -> bool:
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        returned = wintypes.DWORD(0)
        os_handle = msvcrt.get_osfhandle(handle.fileno())
        kernel32 = _kernel32()
        ok = kernel32.DeviceIoControl(
            os_handle, _FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None
        )
        return bool(ok)
    except (OSError, AttributeError, ValueError):
        return False


# --- disk space -------------------------------------------------------------------------


def existing_parent(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def free_bytes(path: str) -> int | None:
    """Free bytes on the volume holding ``path`` (None when it cannot be determined)."""
    try:
        return shutil.disk_usage(existing_parent(path)).free
    except OSError:
        return None


def volume_label(path: str) -> str:
    """``"D:\\"`` for a path on drive D (falls back to the directory itself)."""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    return drive + os.sep if drive else os.path.dirname(os.path.abspath(path))


def is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in _WINERROR_DISK_FULL


def disk_error(exc: OSError, path: str, *, required: int | None, action: str = "write the download to") -> AnkerError:
    """Map an ``OSError`` raised while touching ``path`` to a user-presentable error."""
    if is_disk_full(exc):
        available = free_bytes(path) or 0
        return DiskSpaceError(
            required=max(required or 0, available + 1),
            available=available,
            path=volume_label(path),
            detail=f"{exc!r} while writing {path}",
        )
    reason = exc.strerror or str(exc)
    return DownloadError(f"Could not {action} disk: {reason}.", detail=f"{exc!r} ({path})")
