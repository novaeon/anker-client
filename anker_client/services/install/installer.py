"""Archive → installed game.

Terms: the *target root* is the folder that receives the game folder —
``request.library_root``, or the parent of ``request.existing_install_path``
when that is set. Staging always lives on the target root's volume, in
``<target root>\\.ankerclient\\staging\\<uuid>`` (the ``.ankerclient`` folder is hidden).

FULL install (``request.option.kind == FULL``)
1. Stage: extract into ``<staging>\\x`` (same volume as the destination so the
   final move is a rename). For zip archives the exact uncompressed size is
   checked against the free space first (``DiskSpaceError``). Progress phase
   ``"extracting"`` 0..1.
2. Locate the game root inside the staging dir: descend while the directory
   contains exactly one sub-directory and no files other than junk
   (``JUNK_FILENAMES``/``JUNK_EXTENSIONS``, Thumbs.db/desktop.ini, ``__MACOSX``);
   if several directories and all but one are prerequisite folders
   (``_CommonRedist``…), descend into that one when it contains an ``.exe``
   (depth ≤ 2) and is not a binaries folder (``bin``/``x64``…, i.e. a flat
   archive), moving the prerequisite folders into it. Any other mix of folders
   (``System`` + ``Maps`` + ``Textures``, ``Game`` + ``Soundtrack``…) makes that
   level the game root, so no archive content is ever dropped.
   Delete junk at the root. An archive without files → ``InstallError``.
3. Destination ``<library_root>\\<sanitize_windows_name(title)>``. If
   ``request.existing_install_path`` is set (reinstall/update of the same
   game) that path is the destination. If the destination exists and is NOT
   the same game (manifest slug differs or no manifest), use
   ``"<name> (2)"``, ``"(3)"``… (an existing folder with the same slug is replaced).
4. Detect the executable (``executables``) + candidates, prerequisite folders
   (``REDIST_DIRNAMES`` at depth ≤ 2) and the installed size on the staged
   root, and write the manifest (``.ankerclient.json``, hidden) *into the staged
   root* so the game appears complete with its manifest in one rename. The old
   manifest's ``executable``/``launch_args``/``run_as_admin`` are preserved when
   that executable still exists; ``redist_installed`` survives a same-game reinstall.
5. Replace atomically-ish (serialised by a process-wide lock): rename existing
   dest → ``<dest>.ankerclient-old-<uuid>``, rename staged root → dest, then
   move the backup into the staging session and delete it with the session.
   If the second rename fails the backup is renamed back and ``InstallError``
   is raised. Directory renames retry for several seconds (antivirus scanners
   briefly lock freshly extracted files). A fresh destination name that was
   taken meanwhile (concurrent install) is never replaced: the next free name
   is used. Leftovers of a crash in between are repaired at the start of the
   next install into that root (backup restored when the game folder is
   missing, deleted otherwise); stale staging folders of earlier runs are
   purged then too. A destination that is (or contains) a library folder, lies
   inside an ``.ankerclient`` work folder or is a protected system/user folder
   is refused.
6. Shortcuts per settings when an executable is known (failures are logged, not raised).
Phase ``"installing"`` progress 0..1 for steps 2–6. Cancellation is honoured
until step 5 starts; after the swap the install completes (a progress callback
that raises from then on is logged, not propagated). A ``DownloadOption`` whose
``kind`` is a plain string is normalised to ``DownloadKind``.

PATCH / ADDON (overlay)
* ``request.existing_install_path`` must be an existing managed install
  (``InstallError("Install the base game first.")`` otherwise).
* Extract to staging, then align the archive with the install: the first
  level of the single-folder chain whose entries already exist in the install
  is used (so a patch containing ``Game/Binaries/Win64/x.exe`` lands in
  ``<install>\\Game\\Binaries\\Win64``); when nothing matches, one wrapper folder is
  stripped. Files are moved into the install one by one with progress; every
  replaced file is first moved to a backup inside staging, so a failure or
  cancellation (including a failed manifest update) rolls the install back to
  its previous state. Each step is journaled on disk first (``_overlay``): after
  a crash the next install into that root rolls the half-applied update back
  before it purges the stale staging folder.
* Update the manifest: append the option label to ``applied_options`` (once);
  for PATCH set ``version`` to ``option.to_version`` (or ``request.version``),
  ``source_updated_date`` (when given) and ``updated_at``; re-detect the
  executable when the configured one is missing; refresh ``has_redist``.

Always: clean up staging (also on failure/cancel); never leave a half-moved
destination; never delete anything outside the target root. Afterwards the
archive is deleted when ``settings.delete_archive_after_install`` and not
``keep_archive`` (imported archives); an emptied download job folder under
``.ankerclient\\downloads`` is removed with it.

Module helpers ``read_manifest``/``write_manifest``/``directory_size``/
``find_redist_dirs`` are shared with the library service.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from anker_client.constants import (
    JUNK_EXTENSIONS,
    JUNK_FILENAMES,
    LIBRARY_IGNORED_DIRNAMES,
    LIBRARY_WORK_DIRNAME,
    MANIFEST_FILENAME,
    REDIST_DIRNAMES,
)
from anker_client.core.errors import AnkerError, DiskSpaceError, ExtractionError, InstallError
from anker_client.core.models import (
    DownloadKind,
    DownloadOption,
    InstallManifest,
    InstallRequest,
    InstallResult,
    utc_now_iso,
)
from anker_client.core.paths import is_dangerous_delete_target, is_within, sanitize_windows_name
from anker_client.core.settings import SettingsStore, atomic_write_text
from anker_client.core.tasks import CancelToken
from anker_client.services.install import _fsutil as fs
from anker_client.services.install import _overlay, diskspace, executables
from anker_client.services.install.extractor import Extractor, detect_archive_type
from anker_client.services.install.shortcuts import ShortcutService

log = logging.getLogger(__name__)

BACKUP_INFIX = ".ankerclient-old-"
_BACKUP_RE = re.compile(r"^(?P<name>.+)" + re.escape(BACKUP_INFIX) + r"[0-9a-f]{32}$")
_STAGING_DIRNAME = "staging"
_DOWNLOADS_DIRNAME = "downloads"
_EXTRACT_SUBDIR = "x"
_OLD_SUBDIR = "old"
# Renaming a freshly extracted game folder often fails for a few seconds while an
# antivirus scanner still holds handles inside it, so directory swaps retry longer.
_DIR_RENAME_ATTEMPTS = 10
_DIR_RENAME_DELAY = 0.25

_OS_JUNK_FILES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
_JUNK_DIRNAMES = frozenset({"__macosx"})
_BINARY_SUBDIRS = frozenset({"bin", "bin32", "bin64", "binaries", "x64", "x86", "win64", "win32"})
_IGNORED_FOLDER_NAMES = frozenset(name.casefold() for name in LIBRARY_IGNORED_DIRNAMES)
_REDIST_MAX_DEPTH = 2
_EXE_DIR_MAX_DEPTH = 2
_MAX_ROOT_DESCENT = 64
_SPACE_SLACK = 64 * 1024 * 1024
_SIZE_CHECK_EVERY = 256

# Directory swaps and crash recovery must never interleave (one process: single-instance app).
_SWAP_LOCK = threading.RLock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_STAGING: set[str] = set()

PhaseCallback = Callable[[str, float], None]


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def _coerce_manifest_value(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        if not isinstance(value, list):
            return list(default)
        return [item for item in value if isinstance(item, str)]
    return value


def _manifest_from_data(data: dict[str, Any]) -> InstallManifest:
    defaults = InstallManifest()
    values = {
        f.name: _coerce_manifest_value(data.get(f.name, getattr(defaults, f.name)), getattr(defaults, f.name))
        for f in fields(InstallManifest)
    }
    return InstallManifest(**values)


def read_manifest(install_dir: str) -> InstallManifest | None:
    """Parse ``<install_dir>/.ankerclient.json``; ``None`` when absent or unreadable."""
    if not install_dir:
        return None
    path = os.path.join(install_dir, MANIFEST_FILENAME)
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Unreadable manifest %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Manifest %s is not a JSON object", path)
        return None
    return _manifest_from_data(data)


def write_manifest(install_dir: str, manifest: InstallManifest) -> None:
    """Atomically write the manifest and mark it hidden on Windows.

    Raises ``InstallError`` when the folder is missing or not writable.
    """
    if not install_dir or not os.path.isdir(install_dir):
        raise InstallError("The game folder no longer exists.", detail=str(install_dir))
    path = Path(install_dir) / MANIFEST_FILENAME
    text = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
    try:
        if path.exists():
            fs.clear_readonly(str(path))
        atomic_write_text(path, text)
    except OSError as exc:
        raise InstallError(f"The game information could not be saved in {install_dir}.", detail=str(exc)) from exc
    fs.set_hidden(str(path))


# ---------------------------------------------------------------------------
# folder inspection
# ---------------------------------------------------------------------------


def _is_link(entry: os.DirEntry[str]) -> bool:
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def _real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return not _is_link(entry) and entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _scandir(path: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as iterator:
            return list(iterator)
    except OSError:
        return []


def directory_size(path: str, *, token: CancelToken | None = None) -> int:
    """Total size of regular files under ``path`` (does not follow symlinks/junctions)."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    seen = 0
    stack = [fs.long_path(path)]
    while stack:
        for entry in _scandir(stack.pop()):
            seen += 1
            if token is not None and seen % _SIZE_CHECK_EVERY == 0:
                token.raise_if_cancelled()
            try:
                if _is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def find_redist_dirs(install_dir: str) -> list[str]:
    """Relative paths of prerequisite folders (``REDIST_DIRNAMES``) at depth ≤ 2.

    Depth 0 is a direct child of ``install_dir`` (``_CommonRedist``); depth 2 is
    e.g. ``Engine\\Extras\\Redist``. Found folders are not searched further.
    """
    if not install_dir or not os.path.isdir(install_dir):
        return []
    found: list[str] = []
    stack: list[tuple[str, tuple[str, ...]]] = [(fs.long_path(install_dir), ())]
    while stack:
        directory, parts = stack.pop()
        for entry in _scandir(directory):
            if not _real_dir(entry):
                continue
            relative = (*parts, entry.name)
            if entry.name.casefold() in REDIST_DIRNAMES:
                found.append(os.path.join(*relative))
            elif len(relative) <= _REDIST_MAX_DEPTH:
                stack.append((entry.path, relative))
    return sorted(found, key=str.casefold)


def _is_junk_file(name: str) -> bool:
    folded = name.casefold()
    return folded in JUNK_FILENAMES or folded in _OS_JUNK_FILES or os.path.splitext(folded)[1] in JUNK_EXTENSIONS


def _significant_entries(directory: str) -> tuple[list[str], list[str]]:
    """``(dirs, files)`` of ``directory`` ignoring junk files and junk folders."""
    dirs: list[str] = []
    files: list[str] = []
    for entry in _scandir(directory):
        if _real_dir(entry):
            if entry.name.casefold() not in _JUNK_DIRNAMES:
                dirs.append(entry.path)
        elif not _is_junk_file(entry.name):
            files.append(entry.path)
    return sorted(dirs, key=str.casefold), sorted(files, key=str.casefold)


def _contains_exe(directory: str, max_depth: int) -> bool:
    stack: list[tuple[str, int]] = [(directory, 0)]
    while stack:
        current, depth = stack.pop()
        for entry in _scandir(current):
            if _real_dir(entry):
                if depth < max_depth:
                    stack.append((entry.path, depth + 1))
            elif entry.name.casefold().endswith(".exe"):
                return True
    return False


def _is_redist_name(name: str) -> bool:
    folded = name.casefold()
    return folded in REDIST_DIRNAMES or "redist" in folded or "prereq" in folded


def _preferred_game_dir(dirs: list[str]) -> str | None:
    """The folder holding the game when every sibling is a prerequisite folder, else ``None``.

    Folders the game needs can sit next to the one with the ``.exe`` (old Unreal
    games ship ``System`` + ``Maps`` + ``Textures``), so a sibling that is not a
    prerequisite folder always keeps the current level as the game root.
    """
    main = [d for d in dirs if not _is_redist_name(os.path.basename(d))]
    if len(main) != 1:
        return None
    chosen = main[0]
    if os.path.basename(chosen).casefold() in _BINARY_SUBDIRS:
        return None  # flat archive: "bin/" + "_CommonRedist/" both belong to the game
    if not _contains_exe(chosen, _EXE_DIR_MAX_DEPTH):
        return None
    return chosen


def _adopt_redist_siblings(chosen: str, dirs: list[str]) -> None:
    for directory in dirs:
        if directory == chosen:
            continue
        name = os.path.basename(directory)
        target = os.path.join(chosen, name)
        if os.path.lexists(target):
            log.info("Prerequisite folder %r already exists inside the game folder; keeping that one", name)
            continue
        try:
            os.replace(directory, target)
        except OSError as exc:
            log.warning("Could not keep prerequisite folder %s: %s", name, exc)


def _locate_game_root(top: str) -> str:
    current = top
    for _ in range(_MAX_ROOT_DESCENT):
        dirs, files = _significant_entries(current)
        if files:
            return current
        if not dirs:
            raise InstallError("The archive does not contain any game files.", detail=top)
        if len(dirs) == 1:
            current = dirs[0]
            continue
        preferred = _preferred_game_dir(dirs)
        if preferred is None:
            return current
        _adopt_redist_siblings(preferred, dirs)
        current = preferred
    return current


def _remove_junk(root: str) -> None:
    for entry in _scandir(root):
        if _real_dir(entry):
            if entry.name.casefold() in _JUNK_DIRNAMES:
                fs.try_remove_tree(entry.path, within=root)
        elif _is_junk_file(entry.name):
            try:
                fs.remove_file(entry.path)
            except OSError as exc:
                log.debug("Could not delete junk file %s: %s", entry.path, exc)


def _overlay_source_root(top: str, install_dir: str) -> str:
    """Level of the archive's single-folder chain that lines up with ``install_dir`` (see module doc)."""
    existing = {entry.name.casefold() for entry in _scandir(install_dir)}
    chain = [top]
    current = top
    for _ in range(_MAX_ROOT_DESCENT):
        dirs, files = _significant_entries(current)
        if files or len(dirs) != 1:
            break
        current = dirs[0]
        chain.append(current)
    for level in chain:
        dirs, files = _significant_entries(level)
        if any(os.path.basename(path).casefold() in existing for path in (*dirs, *files)):
            return level
    dirs, files = _significant_entries(top)
    if not dirs and not files:
        raise InstallError("The archive does not contain any files.", detail=top)
    return dirs[0] if len(dirs) == 1 and not files else top


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    relative: str
    path: str
    is_dir: bool
    size: int


def _walk_tree(root: str) -> Iterator[_TreeEntry]:
    """Top-down walk (a folder before its contents), links are skipped."""
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        directory, relative = stack.pop()
        subdirs: list[tuple[str, str]] = []
        for entry in sorted(_scandir(directory), key=lambda e: e.name.casefold()):
            child = os.path.join(relative, entry.name) if relative else entry.name
            if _is_link(entry):
                log.info("Skipping link %s in update archive", child)
                continue
            if _real_dir(entry):
                yield _TreeEntry(child, entry.path, True, 0)
                subdirs.append((entry.path, child))
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            yield _TreeEntry(child, entry.path, False, size)
        stack.extend(reversed(subdirs))


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


class _PhaseProgress:
    """Clamped, per-phase monotonic progress; a broken callback never breaks an install."""

    def __init__(self, callback: PhaseCallback | None) -> None:
        self._callback = callback
        self._last: dict[str, float] = {}
        self._warned = False
        self._committed = False

    def commit(self) -> None:
        """From now on the install cannot be undone: a callback may no longer abort it."""
        self._committed = True

    def __call__(self, phase: str, fraction: float) -> None:
        if self._callback is None:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction <= self._last.get(phase, -1.0):
            return
        self._last[phase] = fraction
        try:
            self._callback(phase, fraction)
        except AnkerError:
            # Before the commit a callback may cancel (e.g. raise OperationCancelled);
            # afterwards the files are in place and the caller must get the result.
            if not self._committed:
                raise
            log.info("Ignoring a progress callback error after the install was committed", exc_info=True)
        except Exception:
            if not self._warned:
                self._warned = True
                log.exception("Install progress callback failed")


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------


def _option_of(request: InstallRequest) -> DownloadOption:
    option: Any = request.option
    try:
        if isinstance(option, dict):
            option = DownloadOption.from_dict(option)
        if isinstance(option, DownloadOption):
            # ``kind`` may be a plain str (built by hand / from JSON): ``is`` checks need the enum.
            if not isinstance(option.kind, DownloadKind):
                option = replace(option, kind=DownloadKind(option.kind))
            return option
    except (TypeError, ValueError) as exc:
        raise InstallError("The install request is incomplete.", detail=f"option={option!r}") from exc
    raise InstallError("The install request is incomplete.", detail=f"option={option!r}")


def _rename_dir(src: str, dst: str) -> None:
    fs.rename_with_retry(src, dst, attempts=_DIR_RENAME_ATTEMPTS, delay=_DIR_RENAME_DELAY)


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _safe_relative(relative: str) -> bool:
    if not relative or os.path.isabs(relative) or os.path.splitdrive(relative)[0]:
        return False
    normalized = os.path.normpath(relative)
    return not (normalized == ".." or normalized.startswith(".." + os.sep))


def _zip_uncompressed_size(archive: str) -> int | None:
    if detect_archive_type(archive) != "zip":
        return None
    try:
        with zipfile.ZipFile(archive) as zf:
            return sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return None  # the extractor reports the damaged archive properly


class Installer:
    def __init__(self, settings: SettingsStore, extractor: Extractor, shortcuts: ShortcutService) -> None:
        self._settings = settings
        self._extractor = extractor
        self._shortcuts = shortcuts

    def install(
        self,
        request: InstallRequest,
        *,
        token: CancelToken,
        on_progress: Callable[[str, float], None] | None = None,  # (phase "extracting"|"installing", 0..1)
        keep_archive: bool = False,
    ) -> InstallResult:
        option = _option_of(request)
        archive = os.path.abspath(request.archive_path) if request.archive_path else ""
        if not archive or not os.path.isfile(archive):
            raise InstallError("The downloaded archive is missing. Download the game again.", detail=archive)
        token.raise_if_cancelled()
        progress = _PhaseProgress(on_progress)
        log.info("Installing %s (%s, %s) from %s", request.title, request.slug, option.kind.value, archive)
        if option.kind is DownloadKind.FULL:
            result = self._install_full(request, option, archive, token=token, progress=progress)
        else:
            result = self._install_overlay(request, option, archive, token=token, progress=progress)
        self._dispose_archive(archive, keep_archive=keep_archive)
        log.info("Installed %s into %s (executable %r)", request.title, result.install_path, result.executable)
        return result

    # --- FULL ----------------------------------------------------------------------------
    def _install_full(
        self,
        request: InstallRequest,
        option: DownloadOption,
        archive: str,
        *,
        token: CancelToken,
        progress: _PhaseProgress,
    ) -> InstallResult:
        target_root = self._target_root(request)
        self._ensure_dir(target_root)
        self._recover_interrupted(target_root)
        session = self._create_staging(target_root)
        try:
            extract_dir = os.path.join(session, _EXTRACT_SUBDIR)
            self._check_space(archive, session)
            self._extract(archive, extract_dir, token=token, progress=progress)
            token.raise_if_cancelled()
            progress("installing", 0.0)

            game_root = _locate_game_root(extract_dir)
            _remove_junk(game_root)
            dest, previous = self._destination(request, target_root)
            if os.path.lexists(dest) and is_within(archive, dest):
                # Replacing the folder would delete the archive along with the old copy.
                raise InstallError(
                    "The archive is inside the game folder it would replace. Move it to another folder and "
                    "try again.",
                    detail=f"{archive} in {dest}",
                )
            token.raise_if_cancelled()
            progress("installing", 0.15)

            candidates = executables.executable_candidates(game_root, request.title)
            executable, launch_args, run_as_admin = self._executable_settings(game_root, candidates, previous)
            redist = find_redist_dirs(game_root)
            progress("installing", 0.3)
            size = directory_size(game_root, token=token)
            progress("installing", 0.5)

            manifest = self._full_manifest(
                request, option, previous, executable=executable, launch_args=launch_args,
                run_as_admin=run_as_admin, has_redist=bool(redist),
            )
            write_manifest(game_root, manifest)
            progress("installing", 0.6)

            token.raise_if_cancelled()  # last cancellation point: the swap commits the install
            with _SWAP_LOCK:
                if previous is None and not request.existing_install_path and os.path.lexists(dest):
                    # Another install took this folder name meanwhile: never replace a foreign folder.
                    dest, _ = self._destination(request, target_root, allow_replace=False)
                backup = self._swap_into_place(game_root, dest, target_root)
            progress.commit()
            if backup is not None:
                self._retire_backup(backup, os.path.join(session, _OLD_SUBDIR), target_root)
            progress("installing", 0.85)
        finally:
            self._discard_staging(session, target_root)

        self._create_shortcuts(request.title or manifest.title, dest, executable, launch_args)
        progress("installing", 1.0)
        return InstallResult(
            install_path=dest,
            executable=executable,
            executable_candidates=[path for path, _ in candidates],
            has_redist=bool(redist),
            size_bytes=size,
        )

    @staticmethod
    def _target_root(request: InstallRequest) -> str:
        if request.existing_install_path:
            dest = os.path.abspath(request.existing_install_path)
            parent = os.path.dirname(dest)
            if not parent or parent == dest:
                raise InstallError("The game cannot be installed into a drive root.", detail=dest)
            return parent
        if not request.library_root:
            raise InstallError("No game library folder is set. Choose one in Settings.")
        return os.path.abspath(request.library_root)

    @staticmethod
    def _ensure_dir(path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise InstallError(f"The folder {path} could not be created.", detail=str(exc)) from exc

    def _destination(
        self, request: InstallRequest, target_root: str, *, allow_replace: bool = True
    ) -> tuple[str, InstallManifest | None]:
        if request.existing_install_path:
            dest = os.path.abspath(request.existing_install_path)
            self._assert_replaceable(dest)
            previous = read_manifest(dest) if os.path.isdir(dest) else None
            return dest, previous
        base = sanitize_windows_name(request.title, fallback=None) or sanitize_windows_name(
            request.slug, fallback="Game"
        )
        for number in range(1, 1000):
            name = base if number == 1 else f"{base} ({number})"
            if name.casefold() in _IGNORED_FOLDER_NAMES:
                continue
            dest = os.path.join(target_root, name)
            if not os.path.lexists(dest):
                return dest, None
            if allow_replace and os.path.isdir(dest) and not os.path.islink(dest):
                previous = read_manifest(dest)
                if previous is not None and request.slug and previous.slug == request.slug:
                    self._assert_replaceable(dest)
                    return dest, previous
        raise InstallError("No free folder name was found for the game.", detail=base)

    def _assert_replaceable(self, dest: str) -> None:
        if is_dangerous_delete_target(dest):
            raise InstallError("AnkerClient will not install a game over a system or user folder.", detail=dest)
        dest_key = _norm_key(dest)
        dest_prefix = dest_key.rstrip("\\/") + os.sep
        for library in self._settings.get().library_dirs:
            library_key = _norm_key(library)
            # Replacing the folder also deletes everything inside it, libraries included.
            if library_key == dest_key or library_key.startswith(dest_prefix):
                raise InstallError("A library folder cannot be replaced by a game.", detail=dest)
        if LIBRARY_WORK_DIRNAME.casefold() in (part.casefold() for part in Path(dest).parts):
            raise InstallError("AnkerClient's work folder cannot be replaced by a game.", detail=dest)
        if os.path.lexists(dest) and not os.path.isdir(dest):
            raise InstallError("A file with the game's name is in the way.", detail=dest)

    @staticmethod
    def _executable_settings(
        game_root: str,
        candidates: list[tuple[str, int]],
        previous: InstallManifest | None,
    ) -> tuple[str, str, bool]:
        if (
            previous is not None
            and _safe_relative(previous.executable)
            and os.path.isfile(os.path.join(game_root, previous.executable))
        ):
            return previous.executable, previous.launch_args, previous.run_as_admin
        return executables._pick_best(candidates), "", False

    @staticmethod
    def _full_manifest(
        request: InstallRequest,
        option: DownloadOption,
        previous: InstallManifest | None,
        *,
        executable: str,
        launch_args: str,
        run_as_admin: bool,
        has_redist: bool,
    ) -> InstallManifest:
        now = utc_now_iso()
        slug = request.slug or (previous.slug if previous else "")
        same_game = previous is not None and bool(previous.slug) and previous.slug == slug
        return InstallManifest(
            slug=slug,
            title=request.title or (previous.title if previous else ""),
            version=request.version or option.to_version,
            source_updated_date=request.source_updated_date,
            installed_at=now,
            updated_at=now,
            executable=executable,
            launch_args=launch_args,
            run_as_admin=run_as_admin,
            applied_options=[option.label] if option.label else [],
            has_redist=has_redist,
            redist_installed=has_redist and same_game and bool(previous and previous.redist_installed),
            cover_url=request.cover_url or (previous.cover_url if previous else ""),
            genres=list(request.genres) or (list(previous.genres) if previous else []),
        )

    def _swap_into_place(self, source: str, dest: str, target_root: str) -> str | None:
        """Move ``source`` to ``dest``; returns the backup of the replaced folder (``None`` when fresh).

        The caller disposes of the backup (:meth:`_retire_backup`).
        """
        backup: str | None = None
        with _SWAP_LOCK:
            if os.path.lexists(dest):
                backup = f"{dest}{BACKUP_INFIX}{uuid.uuid4().hex}"
                try:
                    _rename_dir(dest, backup)
                except OSError as exc:
                    raise InstallError(
                        "The existing installation could not be replaced. Close the game and any window "
                        "showing its folder, then try again.",
                        detail=f"{dest}: {exc}",
                    ) from exc
            try:
                _rename_dir(source, dest)
            except BaseException as exc:
                if backup is not None:
                    self._restore_backup(backup, dest)
                if isinstance(exc, OSError):
                    raise InstallError(
                        "The game could not be moved into the library folder.",
                        detail=f"{source} -> {dest}: {exc}",
                    ) from exc
                raise
        return backup

    @staticmethod
    def _retire_backup(backup: str, trash: str, target_root: str) -> None:
        """Delete a replaced installation that is no longer needed.

        It is first moved to ``trash`` inside the staging area (deleted now, or purged
        by a later install if a locked file stops the delete), so a delete that stops
        half-way never leaves a ``*.ankerclient-old-*`` folder that crash recovery
        would restore as the game after an uninstall.
        """
        try:
            os.makedirs(os.path.dirname(trash), exist_ok=True)
            fs.rename_with_retry(backup, trash)
        except OSError as exc:
            log.info("Could not move %s into staging (%s); deleting it in place", backup, exc)
            fs.try_remove_tree(backup, within=target_root)
            return
        fs.try_remove_tree(trash, within=target_root)

    @staticmethod
    def _restore_backup(backup: str, dest: str) -> None:
        try:
            _rename_dir(backup, dest)
            log.info("Restored previous installation %s", dest)
        except OSError:
            log.critical("Could not restore %s from %s; it will be restored on the next install", dest, backup,
                         exc_info=True)

    @staticmethod
    def _recover_interrupted(target_root: str) -> None:
        with _SWAP_LOCK:
            for entry in _scandir(target_root):
                match = _BACKUP_RE.match(entry.name)
                if not match or not _real_dir(entry):
                    continue
                original = os.path.join(target_root, match.group("name"))
                if os.path.lexists(original):
                    log.info("Deleting leftover backup %s", entry.path)
                    staging = os.path.join(target_root, LIBRARY_WORK_DIRNAME, _STAGING_DIRNAME)
                    Installer._retire_backup(entry.path, os.path.join(staging, uuid.uuid4().hex), target_root)
                    continue
                try:
                    _rename_dir(entry.path, original)
                    log.warning("Restored %s after an interrupted install", original)
                except OSError as exc:
                    log.warning("Could not restore %s from %s: %s", original, entry.path, exc)

    def _create_shortcuts(self, title: str, dest: str, executable: str, launch_args: str) -> None:
        if not executable:
            return
        settings = self._settings.get()
        if not (settings.create_desktop_shortcut or settings.create_start_menu_shortcut):
            return
        try:
            self._shortcuts.create(
                title,
                os.path.join(dest, executable),
                arguments=launch_args,
                desktop=settings.create_desktop_shortcut,
                start_menu=settings.create_start_menu_shortcut,
            )
        except Exception as exc:
            log.warning("Shortcuts for %s could not be created: %s", title, exc)

    # --- PATCH / ADDON ----------------------------------------------------------------------
    def _install_overlay(
        self,
        request: InstallRequest,
        option: DownloadOption,
        archive: str,
        *,
        token: CancelToken,
        progress: _PhaseProgress,
    ) -> InstallResult:
        target = os.path.abspath(request.existing_install_path) if request.existing_install_path else ""
        if target and os.path.isdir(os.path.dirname(target)):
            self._recover_interrupted(os.path.dirname(target))  # the base game may sit in a backup
        if not target or read_manifest(target) is None:
            raise InstallError("Install the base game first.", detail=target or "(no install path)")
        target_root = os.path.dirname(target)
        # Creating the session also rolls back an update of this root that a crash interrupted.
        session = self._create_staging(target_root)
        try:
            manifest = read_manifest(target)
            if manifest is None:
                raise InstallError("Install the base game first.", detail=target)
            extract_dir = os.path.join(session, _EXTRACT_SUBDIR)
            self._check_space(archive, session)
            self._extract(archive, extract_dir, token=token, progress=progress)
            token.raise_if_cancelled()
            progress("installing", 0.0)
            source_root = _overlay_source_root(extract_dir, target)
            _remove_junk(source_root)
            base_manifest = manifest

            def commit() -> InstallManifest:
                updated = read_manifest(target) or base_manifest
                self._update_overlay_manifest(updated, request, option, target)
                write_manifest(target, updated)
                return updated

            manifest = self._overlay_files(
                source_root, target, session, token=token, progress=progress, commit=commit
            )
            progress.commit()
            progress("installing", 0.9)
        finally:
            self._discard_staging(session, target_root)
        candidates = executables.executable_candidates(target, manifest.title or request.title)
        size = directory_size(target)
        progress("installing", 1.0)
        return InstallResult(
            install_path=target,
            executable=manifest.executable,
            executable_candidates=[path for path, _ in candidates],
            has_redist=manifest.has_redist,
            size_bytes=size,
        )

    def _overlay_files(
        self,
        source_root: str,
        target: str,
        session: str,
        *,
        token: CancelToken,
        progress: _PhaseProgress,
        commit: Callable[[], InstallManifest],
    ) -> InstallManifest:
        """Move every file of ``source_root`` into ``target``, then run ``commit`` (the manifest update).

        A failure or cancellation anywhere, ``commit`` included, puts every replaced
        file and the manifest back (``_overlay``), so the manifest never disagrees
        with the files; a crash is rolled back by the next install. Returns ``commit()``.
        """
        # A manifest shipped inside an update archive must never replace the install's own.
        entries = [
            entry for entry in _walk_tree(fs.long_path(source_root))
            if entry.relative.casefold() != MANIFEST_FILENAME.casefold()
        ]
        total = sum(max(entry.size, 1) for entry in entries if not entry.is_dir) or 1
        try:
            transaction = _overlay.OverlayTransaction(session, target)
        except OSError as exc:
            raise InstallError("The game files could not be updated.", detail=str(exc)) from exc
        done = 0
        try:
            for entry in entries:
                token.raise_if_cancelled()
                if entry.is_dir:
                    transaction.add_dir(entry.relative)
                    continue
                transaction.add_file(entry.path, entry.relative)
                done += max(entry.size, 1)
                progress("installing", 0.8 * done / total)
            token.raise_if_cancelled()
            transaction.protect_manifest()
            manifest = commit()
            transaction.complete()
            return manifest
        except BaseException as exc:
            transaction.rollback()
            if isinstance(exc, OSError):
                raise InstallError(
                    "The game files could not be updated. Close the game and try again.", detail=str(exc)
                ) from exc
            raise
        finally:
            transaction.close()

    @staticmethod
    def _update_overlay_manifest(
        manifest: InstallManifest,
        request: InstallRequest,
        option: DownloadOption,
        target: str,
    ) -> None:
        if option.label and option.label not in manifest.applied_options:
            manifest.applied_options.append(option.label)
        if option.kind is DownloadKind.PATCH:
            new_version = option.to_version or request.version
            if new_version:
                manifest.version = new_version
            if request.source_updated_date:
                manifest.source_updated_date = request.source_updated_date
            manifest.updated_at = utc_now_iso()
        if not (_safe_relative(manifest.executable) and os.path.isfile(os.path.join(target, manifest.executable))):
            detected = executables.find_executable(target, manifest.title or request.title)
            if detected or manifest.executable:
                log.info("Executable of %s is now %r", target, detected)
            manifest.executable = detected
        manifest.has_redist = bool(find_redist_dirs(target))
        if not manifest.cover_url and request.cover_url:
            manifest.cover_url = request.cover_url

    # --- shared steps -------------------------------------------------------------------------
    def _create_staging(self, target_root: str) -> str:
        work_dir = os.path.join(target_root, LIBRARY_WORK_DIRNAME)
        base = os.path.join(work_dir, _STAGING_DIRNAME)
        self._ensure_dir(base)
        fs.set_hidden(work_dir)
        self._purge_stale_staging(base, target_root)
        session = os.path.join(base, uuid.uuid4().hex)
        with _ACTIVE_LOCK:
            _ACTIVE_STAGING.add(_norm_key(session))
        try:
            os.makedirs(session)
        except OSError as exc:
            with _ACTIVE_LOCK:
                _ACTIVE_STAGING.discard(_norm_key(session))
            raise InstallError(f"The folder {base} could not be prepared.", detail=str(exc)) from exc
        return session

    @staticmethod
    def _purge_stale_staging(base: str, target_root: str) -> None:
        for entry in _scandir(base):
            # Check the live registry per entry (not a snapshot taken before the scan): a
            # concurrent install registers its session before creating the folder.
            with _ACTIVE_LOCK:
                active = _norm_key(entry.path) in _ACTIVE_STAGING
            if active:
                continue
            if not _overlay.recover(entry.path, within=target_root):
                log.warning("Keeping %s: an interrupted update could not be fully rolled back yet", entry.path)
                continue
            log.info("Deleting leftover staging folder %s", entry.path)
            fs.try_remove_tree(entry.path, within=target_root)

    @staticmethod
    def _discard_staging(session: str, target_root: str) -> None:
        try:
            # A journal left behind means an update rollback did not finish: retry it, and keep
            # the session (it holds the backups of the original files) until it succeeds.
            if _overlay.recover(session, within=target_root):
                fs.try_remove_tree(session, within=target_root)
            else:
                log.warning("Keeping %s: an update could not be fully rolled back yet", session)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_STAGING.discard(_norm_key(session))

    @staticmethod
    def _check_space(archive: str, staging: str) -> None:
        needed = _zip_uncompressed_size(archive)
        if needed is None:
            return  # 7-Zip reports a full disk itself
        required = needed + _SPACE_SLACK
        available = diskspace.free_bytes(staging)
        if available < required:
            raise DiskSpaceError(required, available, diskspace._volume_root(staging))

    def _extract(self, archive: str, dest: str, *, token: CancelToken, progress: _PhaseProgress) -> None:
        progress("extracting", 0.0)
        try:
            self._extractor.extract(archive, dest, token=token, on_progress=lambda f: progress("extracting", f))
        except AnkerError:
            raise
        except OSError as exc:
            raise ExtractionError(detail=f"{archive}: {exc}") from exc
        progress("extracting", 1.0)

    def _dispose_archive(self, archive: str, *, keep_archive: bool) -> None:
        if keep_archive or not self._settings.get().delete_archive_after_install:
            return
        try:
            fs.remove_file(archive)
        except OSError as exc:
            log.warning("Could not delete the archive %s: %s", archive, exc)
            return
        log.info("Deleted archive %s", archive)
        job_dir = os.path.dirname(archive)
        downloads_dir = os.path.dirname(job_dir)
        work_dir = os.path.dirname(downloads_dir)
        if (
            os.path.basename(downloads_dir).casefold() == _DOWNLOADS_DIRNAME
            and os.path.basename(work_dir).casefold() == LIBRARY_WORK_DIRNAME
        ):
            try:
                os.rmdir(job_dir)  # only when empty
            except OSError:
                pass
