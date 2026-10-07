"""Filesystem helpers for the library service: path containment, link detection and safe deletion.

Private to ``services.library`` / ``services.launcher``. Everything here is
careful about Windows specifics:

* paths are compared with ``os.path.normcase`` (case-insensitive on Windows);
* symbolic links and directory junctions are never followed — a link is
  removed as a link, its target is never touched;
* read-only attributes are cleared before retrying a failed delete;
* sharing violations (a file still held open by a game/AV scanner) are retried
  with a short back-off before giving up;
* deletion works on ``\\\\?\\`` extended-length paths, so trees deeper than
  ``MAX_PATH`` and names Win32 would otherwise rewrite (trailing dots/spaces,
  which 7-Zip happily extracts) are removed instead of "not found".
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime

from anker_client.core.errors import InstallError
from anker_client.core.tasks import NEVER, CancelToken

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

# Windows error codes that are usually transient while deleting: access denied
# (pending delete / AV scan), sharing violation, lock violation, directory not
# empty (children still "delete pending").
_TRANSIENT_WINERRORS = frozenset({5, 32, 33, 145})
_TRANSIENT_ERRNOS = frozenset({13, 39, 41})  # EACCES, ENOTEMPTY (POSIX), ENOTEMPTY (Windows CRT)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def norm_key(path: str | os.PathLike[str]) -> str:
    """Absolute, normalised, case-folded (on Windows) path used for comparisons and dict keys."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def same_path(a: str | os.PathLike[str] | None, b: str | os.PathLike[str] | None) -> bool:
    if not a or not b:
        return False
    return norm_key(a) == norm_key(b)


def is_strictly_inside(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when ``path`` is a descendant of ``root`` (lexically, never equal to it)."""
    p, r = norm_key(path), norm_key(root)
    if p == r:
        return False
    prefix = r if r.endswith(os.sep) else r + os.sep
    return p.startswith(prefix)


def is_safely_inside(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Strictly inside ``root`` both lexically and after resolving links/junctions."""
    if not is_strictly_inside(path, root):
        return False
    try:
        real_path = os.path.realpath(os.fspath(path))
        real_root = os.path.realpath(os.fspath(root))
    except (OSError, ValueError):
        return False
    return is_strictly_inside(real_path, real_root)


def is_link(path: str | os.PathLike[str]) -> bool:
    """Symbolic link or directory junction (mount point)."""
    try:
        return os.path.islink(path) or os.path.isjunction(path)
    except OSError:
        return False


def entry_is_link(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True  # unknown → treat as a link so we never descend into it


def entry_attributes(entry: os.DirEntry[str]) -> int:
    """Windows file attributes of a directory entry (0 elsewhere or when unreadable)."""
    try:
        return int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return 0


def mtime_iso(path: str | os.PathLike[str]) -> str:
    """Modification time of ``path`` as an ISO UTC string ("" when unavailable)."""
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, UTC).replace(microsecond=0).isoformat()


def extended_path(path: str | os.PathLike[str]) -> str:
    """Absolute ``\\\\?\\`` (or ``\\\\?\\UNC\\``) form of ``path`` on Windows; plain absolute path elsewhere.

    Win32 path parsing is skipped for such paths: no ``MAX_PATH`` limit and no
    silent stripping of trailing dots/spaces from names.
    """
    absolute = os.path.normpath(os.path.abspath(os.fspath(path)))
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def long_path(path: str) -> str:
    """Expand 8.3 short names (``C:\\PROGRA~1``) so paths compare equal to what Windows reports."""
    if os.name != "nt" or "~" not in path:
        return path
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetLongPathNameW(path, buffer, len(buffer))
        return buffer.value if 0 < length < len(buffer) else path
    except (OSError, AttributeError, ValueError):
        return path


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


def _is_transient(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return isinstance(exc, PermissionError) or exc.errno in _TRANSIENT_ERRNOS


def _clear_readonly(path: str) -> None:
    """Make a regular file/directory writable (never called on links: chmod would follow them)."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or is_link(path):
        return
    try:
        os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove_link(path: str) -> None:
    """Remove a symlink/junction itself; works for file and directory links on Windows."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)


def _retrying(
    action: Callable[[str], None],
    path: str,
    *,
    token: CancelToken,
    attempts: int,
    delay: float,
    clear_readonly: bool,
) -> None:
    cleared = False
    failures = 0
    while True:
        try:
            action(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if clear_readonly and not cleared and isinstance(exc, PermissionError):
                cleared = True
                _clear_readonly(path)
                continue  # retry immediately after clearing the read-only attribute
            failures += 1
            if failures >= attempts or not _is_transient(exc):
                raise
            log.debug("Retrying delete of %s after %s", path, exc)
            token.sleep(delay * failures)


class DeleteProgress:
    """Counts what :func:`delete_tree` removed (useful for logs and tests)."""

    __slots__ = ("dirs", "files", "links")

    def __init__(self) -> None:
        self.files = 0
        self.dirs = 0
        self.links = 0


def delete_tree(
    top: str,
    *,
    token: CancelToken | None = None,
    keep_last: str = "",
    attempts: int = 6,
    retry_delay: float = 0.25,
) -> DeleteProgress:
    """Delete the directory ``top`` bottom-up without ever following links.

    * Links/junctions found inside the tree are removed as links (targets untouched).
    * ``keep_last`` names a file directly inside ``top`` that is deleted only after
      everything else (the manifest — so an interrupted uninstall still leaves an
      identifiable, retryable game folder).
    * Cancellation is checked before every entry; ``OperationCancelled`` propagates.
    * Failures raise :class:`InstallError` naming the offending path.
    """
    token = token or NEVER
    progress = DeleteProgress()
    # Every I/O call uses the extended-length form; without it a file named "x." is reported
    # "not found" (and counted as deleted) while it is still there.
    io_top = extended_path(top)
    display_name = os.path.basename(os.path.normpath(os.path.abspath(top)))
    if is_link(io_top):
        raise InstallError("The game folder is a link to another location and was not deleted.")
    keep_folded = keep_last.casefold()
    stack: list[tuple[str, bool]] = [(io_top, False)]

    def fail(path: str, exc: OSError) -> InstallError:
        relative = path[len(io_top):].lstrip("\\/") if path.startswith(io_top) else path
        return InstallError(
            f'Could not delete "{relative or display_name}". Close the game and any program using its files, '
            "then try again.",
            detail=f"{type(exc).__name__}: {exc}",
        )

    def remove(action: Callable[[str], None], path: str, *, clear_readonly: bool = True) -> None:
        try:
            _retrying(action, path, token=token, attempts=attempts, delay=retry_delay, clear_readonly=clear_readonly)
        except OSError as exc:
            raise fail(path, exc) from exc

    while stack:
        path, children_done = stack.pop()
        if children_done:
            if path == io_top and keep_last:
                last = os.path.join(io_top, keep_last)
                if os.path.lexists(last):
                    token.raise_if_cancelled()
                    remove(os.remove, last)
                    progress.files += 1
            remove(os.rmdir, path)
            progress.dirs += 1
            continue

        stack.append((path, True))
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise fail(path, exc) from exc

        for entry in entries:
            token.raise_if_cancelled()
            if entry_is_link(entry):
                remove(_remove_link, entry.path, clear_readonly=False)
                progress.links += 1
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise fail(entry.path, exc) from exc
            if is_dir:
                stack.append((entry.path, False))
            elif keep_last and path == io_top and entry.name.casefold() == keep_folded:
                continue
            else:
                remove(os.remove, entry.path)
                progress.files += 1
    return progress
