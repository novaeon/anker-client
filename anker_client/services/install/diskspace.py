"""Free-space checks before downloading/extracting.

Volumes are identified by their mount root (``GetVolumePathNameW`` on Windows,
so folder mount points work; e.g. ``"C:\\\\"``). Paths that do not exist yet
are measured at their first existing parent. A path whose drive does not
exist at all has 0 free bytes.
"""

from __future__ import annotations

import logging
import math
import os
import shutil

from anker_client.constants import DISK_SPACE_FACTOR, DISK_SPACE_HEADROOM_BYTES
from anker_client.core.errors import DiskSpaceError
from anker_client.services.install._fsutil import IS_WINDOWS

log = logging.getLogger(__name__)


def _existing_ancestor(path: str) -> str | None:
    current = os.path.abspath(path)
    while True:
        if os.path.exists(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _volume_root(path: str) -> str:
    """Mount root of the volume holding ``path`` (``"C:\\\\"``, ``"\\\\\\\\server\\\\share\\\\"``, ``"/"``)."""
    existing = _existing_ancestor(path) or os.path.abspath(path)
    if IS_WINDOWS:
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetVolumePathNameW(existing, buffer, len(buffer)):  # type: ignore[attr-defined]
                return _normalize_root(buffer.value)
        except Exception:
            log.debug("GetVolumePathNameW failed for %s", existing, exc_info=True)
        drive, _ = os.path.splitdrive(existing)
        return _normalize_root(drive + "\\" if drive else existing)
    current = existing
    device = os.stat(current).st_dev
    while True:
        parent = os.path.dirname(current)
        if parent == current or os.stat(parent).st_dev != device:
            return current
        current = parent


def _normalize_root(root: str) -> str:
    if IS_WINDOWS and len(root) >= 2 and root[1] == ":":
        root = root[0].upper() + root[1:]
    if not root.endswith(("\\", "/")):
        root += os.sep
    return root


def free_bytes(path: str) -> int:
    """Free bytes on the volume holding ``path`` (walks up to the first existing parent)."""
    existing = _existing_ancestor(path)
    if existing is None:
        log.warning("No existing folder for %s; reporting 0 free bytes", path)
        return 0
    try:
        return int(shutil.disk_usage(existing).free)
    except OSError as exc:
        log.warning("Could not read free space of %s: %s", existing, exc)
        return 0


def same_volume(a: str, b: str) -> bool:
    existing_a, existing_b = _existing_ancestor(a), _existing_ancestor(b)
    if existing_a and existing_b:
        try:
            # st_dev is the volume serial number on Windows: robust for mount points and subst drives.
            return os.stat(existing_a).st_dev == os.stat(existing_b).st_dev
        except OSError:
            pass
    return os.path.normcase(_volume_root(a)) == os.path.normcase(_volume_root(b))


def required_bytes(archive_size: int | None, *, download_dir: str, library_root: str) -> dict[str, int]:
    """Bytes needed per volume root, e.g. ``{"C:\\\\": n}``.

    Archive needs ``archive_size`` on the download volume; extraction needs
    ``archive_size * (DISK_SPACE_FACTOR - 1)`` on the library volume (same volume → summed),
    plus ``DISK_SPACE_HEADROOM_BYTES`` once per volume. Unknown (``None``/negative)
    size → ``{}``. An empty ``download_dir`` means downloads live in the library root.
    """
    if archive_size is None or archive_size < 0:
        return {}
    download_dir = download_dir or library_root
    archive_need = int(archive_size)
    extract_need = math.ceil(archive_size * (DISK_SPACE_FACTOR - 1))
    library_volume = _volume_root(library_root)
    download_volume = _volume_root(download_dir)
    if os.path.normcase(download_volume) == os.path.normcase(library_volume) or same_volume(
        download_dir, library_root
    ):
        return {library_volume: archive_need + extract_need + DISK_SPACE_HEADROOM_BYTES}
    return {
        download_volume: archive_need + DISK_SPACE_HEADROOM_BYTES,
        library_volume: extract_need + DISK_SPACE_HEADROOM_BYTES,
    }


def ensure_space(
    archive_size: int | None, *, download_dir: str, library_root: str, already_downloaded: int = 0
) -> None:
    """Raise ``DiskSpaceError`` if any volume lacks the space from ``required_bytes``
    (minus bytes of a partial download that already exist on the download volume)."""
    needs = required_bytes(archive_size, download_dir=download_dir, library_root=library_root)
    if not needs:
        return
    credit = max(0, min(int(already_downloaded or 0), int(archive_size or 0)))
    # One entry means download and library share a volume; it then holds the partial file.
    download_volume = (
        next(iter(needs)) if len(needs) == 1 else _volume_root(download_dir or library_root)
    )
    for volume, gross_need in needs.items():
        is_download_volume = os.path.normcase(volume) == os.path.normcase(download_volume)
        need = max(0, gross_need - credit) if is_download_volume else gross_need
        available = free_bytes(volume)
        if available < need:
            raise DiskSpaceError(need, available, volume)
