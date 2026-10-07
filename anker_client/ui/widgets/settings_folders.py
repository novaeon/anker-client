"""Library-folder validation and the folder list editor (Settings → Library, first-run wizard).

``check_library_folder`` / ``check_download_folder`` do blocking filesystem
checks — call them through ``run_async``. A library root must be a dedicated
folder: every direct sub-folder is treated as a game and uninstall deletes
inside it, so drive roots, profile/system folders, Program Files and folders
nested inside (or containing) another library root are refused.

Writability is tested by creating (and immediately deleting) a temporary file
in the folder or its nearest existing parent: on Windows ``os.access`` only
looks at the read-only attribute and ignores ACLs.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QEvent, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from anker_client.core.formatting import format_bytes
from anker_client.core.paths import is_dangerous_delete_target, is_within
from anker_client.ui.widgets.common import Badge, Divider, button, icon_button
from anker_client.ui.widgets.settings_controls import IconBinder, StatusLine

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FolderCheck:
    path: str  # normalised path ("" when the input was empty)
    ok: bool
    message: str  # error, or "312 GB free on D:" style info
    free_bytes: int | None = None
    exists: bool = False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _protected_roots() -> list[str]:
    roots = []
    for env in ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA"):
        value = os.environ.get(env)
        if value:
            roots.append(value)
    return roots


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _is_writable(directory: Path) -> bool:
    """Create and delete a probe file in ``directory`` (the only reliable test on Windows).

    One ``O_EXCL`` attempt with a random name: ``tempfile`` cannot be used because on Windows
    it retries forever on ``PermissionError`` in a folder ``os.access`` calls writable.
    """
    probe = directory / f".ankerclient-write-test-{uuid.uuid4().hex}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_TEMPORARY", 0)  # deleted on close
    try:
        fd = os.open(probe, flags, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        probe.unlink(missing_ok=True)
    except OSError:
        log.debug("Could not remove the write probe %s", probe, exc_info=True)
    return True


def overlapping_folder(path: str, others: Iterable[str]) -> str | None:
    """The first of ``others`` that is ``path``, contains it or lies inside it (None when none does)."""
    for other in others:
        if other and (is_within(path, other) or is_within(other, path)):
            return other
    return None


def _absolute(path: str, example: str) -> tuple[str, FolderCheck | None]:
    """``(normalised path, None)`` or ``("", failed check)`` for empty/relative input."""
    text = (path or "").strip().strip('"').strip()
    if not text:
        return "", FolderCheck("", False, "Choose a folder.")
    candidate = Path(text)
    if not candidate.is_absolute() or (os.name == "nt" and not candidate.drive):
        return "", FolderCheck(text, False, f"Enter a full path, for example {example}.")
    return os.path.normpath(text), None


def _location_problem(candidate: Path) -> tuple[Path | None, str]:
    """``(nearest existing folder, "")`` or ``(None, reason)`` when nothing can be written there."""
    if any(is_within(candidate, root) for root in _protected_roots()):
        return None, "Folders inside Windows or Program Files need administrator rights. Choose another folder."
    anchor = _nearest_existing(candidate)
    if anchor is None:
        return None, f"The drive {candidate.drive or candidate.anchor} is not available."
    if candidate.exists() and not candidate.is_dir():
        return None, "That path is a file, not a folder."
    if not anchor.is_dir() or not _is_writable(anchor):
        return None, "AnkerClient cannot write to this location."
    return anchor, ""


def _free_space_info(candidate: Path, anchor: Path) -> tuple[str, int | None]:
    try:
        free: int | None = shutil.disk_usage(anchor).free
    except OSError:
        free = None
    drive = candidate.drive or candidate.anchor
    info = f"{format_bytes(free)} free on {drive}" if free is not None else "Free space unknown"
    if not candidate.exists():
        info += " · the folder will be created"
    return info, free


def check_library_folder(path: str, *, existing: Iterable[str] = ()) -> FolderCheck:
    """Validate ``path`` as a new library root (blocking: touches the disk).

    ``existing`` are the other library roots: the same folder, one inside them or one
    containing them is refused (nested roots would make one library's games look like
    sub-folders of another's).
    """
    normalized, failed = _absolute(path, "D:\\Games")
    if failed is not None:
        return failed if failed.path else FolderCheck("", False, "Choose a folder for your games.")
    candidate = Path(normalized)
    others = [e for e in existing if e]
    if _norm(normalized) in {_norm(e) for e in others}:
        return FolderCheck(normalized, False, "This folder is already one of your library folders.")
    other = overlapping_folder(normalized, others)
    if other is not None:
        return FolderCheck(normalized, False, f"This folder overlaps another library folder ({other}).")
    if candidate.parent == candidate or is_dangerous_delete_target(candidate):
        return FolderCheck(normalized, False, "Choose a dedicated folder for games, such as D:\\Games, "
                                              "not a drive or a personal/system folder.")
    anchor, problem = _location_problem(candidate)
    if anchor is None:
        return FolderCheck(normalized, False, problem)
    info, free = _free_space_info(candidate, anchor)
    return FolderCheck(normalized, True, info, free, candidate.exists())


def check_download_folder(path: str) -> FolderCheck:
    """Validate ``path`` as the download folder (blocking: touches the disk).

    Less strict than a library root (``Downloads`` is a fine choice): it must be a full
    path outside Windows/Program Files where AnkerClient can write.
    """
    normalized, failed = _absolute(path, "D:\\Downloads")
    if failed is not None:
        return failed
    candidate = Path(normalized)
    anchor, problem = _location_problem(candidate)
    if anchor is None:
        return FolderCheck(normalized, False, problem)
    info, free = _free_space_info(candidate, anchor)
    return FolderCheck(normalized, True, info, free, candidate.exists())


def folder_free_space(path: str) -> int | None:
    """Free bytes on the volume holding ``path`` (or its nearest existing parent)."""
    anchor = _nearest_existing(Path(path))
    if anchor is None:
        return None
    try:
        return shutil.disk_usage(anchor).free
    except OSError:
        return None


class ElidedPathLabel(QLabel):
    """Shows a path elided in the middle to fit (full path in the tooltip).

    A plain label's minimum width is its whole text, so one long path widened the
    section beyond the window and pushed the row's buttons out of view.
    """

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = path
        self.setToolTip(path)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(80, super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: QEvent | None) -> None:  # noqa: N802
        super().changeEvent(event)
        if event is not None and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        width = max(0, self.width())
        text = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, width) if width else self._full
        if text != self.text():
            super().setText(text)


class _FolderRow(QWidget):
    def __init__(self, path: str, is_default: bool, removable: bool, binder: IconBinder,
                 editor: LibraryFoldersEditor) -> None:
        super().__init__()
        self.setProperty("role", "transparent")
        self.path = path
        icon = QLabel()
        icon.setFixedSize(20, 20)
        binder.bind(icon, "folder", "accent" if is_default else "text_muted", 20)
        self.path_label = ElidedPathLabel(path)
        self.info = StatusLine("Checking free space…", "plain")
        text = QVBoxLayout()
        text.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.path_label)
        if is_default:
            title_row.addWidget(Badge("Default", "accent"))
        title_row.addStretch(1)
        text.addLayout(title_row)
        text.addWidget(self.info)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 10, 12, 10)
        row.setSpacing(12)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)
        self.default_button = button("Make default", variant="ghost", size="sm",
                                     on_click=lambda: editor.default_requested.emit(path))
        self.default_button.setVisible(not is_default)
        self.open_button = icon_button("external", "Open in Explorer",
                                       on_click=lambda: editor.open_requested.emit(path))
        binder.bind(self.open_button, "external", "text_muted", 16)
        self.remove_button = icon_button("trash", "Remove from library",
                                         on_click=lambda: editor.remove_requested.emit(path))
        binder.bind(self.remove_button, "trash", "text_muted", 16)
        self.remove_button.setEnabled(removable)
        if not removable:
            self.remove_button.setToolTip("Your library needs at least one folder")
        row.addWidget(self.default_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)


class LibraryFoldersEditor(QWidget):
    """List of library folders with default/open/remove actions and an "Add folder…" button.

    The editor only emits requests; the owner validates, persists and calls :meth:`set_folders`.
    """

    add_requested = pyqtSignal()
    remove_requested = pyqtSignal(str)
    default_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        self._binder = IconBinder()
        self._rows: dict[str, _FolderRow] = {}
        self._list = QVBoxLayout()
        self._list.setSpacing(0)
        self._list.setContentsMargins(0, 0, 0, 0)
        self.add_button = button("Add folder…", "plus", size="sm", on_click=self.add_requested.emit)
        self.add_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._binder.bind(self.add_button, "plus", "text", 16)
        self.message = StatusLine("", "error")
        self.message.hide()
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 10, 18, 12)
        footer.setSpacing(12)
        footer.addWidget(self.add_button)
        footer.addWidget(self.message, 1)
        footer.addStretch(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self._list)
        layout.addLayout(footer)

    def folders(self) -> list[str]:
        return list(self._rows)

    def row(self, path: str) -> _FolderRow | None:
        return self._rows.get(path)

    def set_folders(self, folders: list[str], default: str) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        removable = len(folders) > 1
        for index, path in enumerate(folders):
            if index:
                holder = QWidget()
                holder.setProperty("role", "transparent")
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(18, 0, 18, 0)
                hl.addWidget(Divider())
                self._list.addWidget(holder)
            row = _FolderRow(path, _norm(path) == _norm(default), removable, self._binder, self)
            self._rows[path] = row
            self._list.addWidget(row)

    def set_folder_info(self, path: str, text: str, kind: str = "plain") -> None:
        row = self._rows.get(path)
        if row is not None:
            row.info.set_status(text, kind)

    def show_message(self, text: str, kind: str = "error") -> None:
        self.message.set_status(text, kind)
        self.message.setVisible(bool(text))

    def retint(self) -> None:
        self._binder.retint()
        self.message.retint()
        for row in self._rows.values():
            row.info.retint()
