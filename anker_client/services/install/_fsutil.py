"""Windows-tolerant filesystem and process helpers shared by the install package (private).

Windows makes "delete this folder" surprisingly hard: antivirus scanners,
the search indexer and Explorer briefly hold handles on freshly written files
(``ERROR_SHARING_VIOLATION``), game files ship with the read-only attribute,
and install trees routinely exceed ``MAX_PATH``. Everything here retries a few
times with short sleeps, clears read-only attributes and uses ``\\\\?\\``
extended-length paths for bulk I/O.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable

import psutil

from anker_client.core.errors import InstallError
from anker_client.core.paths import is_dangerous_delete_target, is_within

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

#: ``creationflags`` for every child process we spawn (no console window flashes).
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

# Access denied, sharing violation, lock violation, directory not empty (a scanner
# still holds a child), "the directory is not empty" race after deleting children.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33, 145})
_NOT_SAME_DEVICE_WINERROR = 17


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def long_path(path: str) -> str:
    """``path`` as an absolute extended-length path on Windows (unchanged elsewhere)."""
    if not IS_WINDOWS:
        return os.path.abspath(path)
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_strictly_within(path: str, root: str) -> bool:
    """True when ``path`` is inside ``root`` and is not ``root`` itself."""
    return is_within(path, root) and not same_path(os.path.realpath(path), os.path.realpath(root))


# ---------------------------------------------------------------------------
# attributes
# ---------------------------------------------------------------------------


def _kernel32():  # pragma: no cover - trivial accessor
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def set_hidden(path: str) -> None:
    """Best-effort: add the hidden attribute (no-op off Windows)."""
    if not IS_WINDOWS:
        return
    try:
        kernel32 = _kernel32()
        attrs = kernel32.GetFileAttributesW(str(path))
        if attrs == _INVALID_FILE_ATTRIBUTES:
            return
        if not attrs & _FILE_ATTRIBUTE_HIDDEN:
            kernel32.SetFileAttributesW(str(path), attrs | _FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        log.debug("Could not hide %s", path, exc_info=True)


def is_hidden(path: str) -> bool:
    if not IS_WINDOWS:
        return os.path.basename(path).startswith(".")
    try:
        attrs = _kernel32().GetFileAttributesW(str(path))
    except Exception:
        return False
    return attrs != _INVALID_FILE_ATTRIBUTES and bool(attrs & _FILE_ATTRIBUTE_HIDDEN)


def is_readonly(path: str) -> bool:
    try:
        return not os.lstat(path).st_mode & stat.S_IWRITE
    except OSError:
        return False


def set_readonly(path: str) -> None:
    """Best-effort: mark ``path`` read-only again (used when rolling back)."""
    try:
        mode = os.lstat(path).st_mode
        os.chmod(path, mode & ~(stat.S_IWRITE | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def clear_readonly(path: str) -> None:
    """Best-effort: make ``path`` writable/deletable."""
    try:
        mode = os.lstat(path).st_mode
        if not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


def is_transient(exc: BaseException) -> bool:
    """Errors that usually disappear when a scanner/indexer lets go of a handle."""
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in _TRANSIENT_WINERRORS:
        return True
    return isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EBUSY, errno.ENOTEMPTY)


def _retry(action: Callable[[], None], *, attempts: int, delay: float, what: str) -> None:
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            if attempt >= attempts or not is_transient(exc):
                raise
            log.debug("%s failed (%s); retrying (%d/%d)", what, exc, attempt, attempts)
            time.sleep(delay * attempt)


def rename_with_retry(src: str, dst: str, *, attempts: int = 5, delay: float = 0.2) -> None:
    """``os.replace(src, dst)`` retrying transient Windows lock errors."""
    _retry(lambda: os.replace(src, dst), attempts=attempts, delay=delay, what=f"Rename {src} -> {dst}")


def move_file(src: str, dst: str) -> None:
    """Move one file, falling back to copy+delete across volumes."""
    try:
        rename_with_retry(src, dst)
    except OSError as exc:
        cross_device = exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _NOT_SAME_DEVICE_WINERROR
        if not cross_device:
            raise
        shutil.copy2(src, dst)
        remove_file(src)


def remove_file(path: str, *, attempts: int = 5, delay: float = 0.2) -> None:
    """Delete one file; missing is fine; clears read-only; retries transient locks."""

    def action() -> None:
        try:
            os.remove(path)
        except PermissionError:
            clear_readonly(path)
            os.remove(path)

    try:
        _retry(action, attempts=attempts, delay=delay, what=f"Delete {path}")
    except FileNotFoundError:
        pass


def _rmtree_onexc(func: Callable[..., object], path: str, exc: BaseException) -> None:
    if isinstance(exc, FileNotFoundError):
        return
    if isinstance(exc, PermissionError):
        clear_readonly(path)
        try:
            func(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            pass
    raise exc


def remove_tree(path: str, *, within: str, attempts: int = 5, delay: float = 0.25) -> None:
    """Recursively delete ``path``, which must lie strictly inside ``within``.

    Junctions/symlinks inside the tree are unlinked, never followed. Refuses
    (``InstallError``) to touch anything outside ``within`` or any protected
    folder (drive roots, the user profile, system folders).
    """
    if not os.path.lexists(path):
        return
    if not is_strictly_within(path, within) or is_dangerous_delete_target(path):
        raise InstallError(
            "AnkerClient refused to delete a folder outside the game library.",
            detail=f"path={path!r} root={within!r}",
        )
    target = long_path(path)

    def action() -> None:
        if os.path.isdir(target) and not os.path.islink(target) and not _is_junction(target):
            shutil.rmtree(target, onexc=_rmtree_onexc)
        else:
            try:
                os.remove(target)
            except PermissionError:
                clear_readonly(target)
                os.remove(target)
            except IsADirectoryError:  # pragma: no cover - junction on non-Windows
                os.rmdir(target)

    try:
        _retry(action, attempts=attempts, delay=delay, what=f"Delete {path}")
    except FileNotFoundError:
        pass


def _is_junction(path: str) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def try_remove_tree(path: str, *, within: str) -> bool:
    """:func:`remove_tree` that logs instead of raising; returns success."""
    try:
        remove_tree(path, within=within)
        return True
    except Exception as exc:
        log.warning("Could not delete %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


def kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and all of its descendants without waiting (safe from any thread)."""
    if proc.poll() is not None:
        return
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        proc.kill()
    except OSError:
        pass
