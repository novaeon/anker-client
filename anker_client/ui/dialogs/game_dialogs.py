"""Library dialogs: executable picker, game properties, import archive/folder.

All four read and write through services on worker threads (``run_async``)
and show loading / error states inline; none of them blocks the GUI thread.

* :class:`ExecutablePickerDialog` — ``library.executable_candidates`` (best
  first) with relative paths and file sizes, "Recommended" on the first entry
  and "Current" on the configured one; "Browse…" opens a file dialog rooted
  at the install folder and refuses files outside it; Save →
  ``library.set_executable(install_id, relative_path)``.
* :class:`GamePropertiesDialog` — display name (``library.rename``), program
  (candidates + Browse…, ``library.set_executable``), launch arguments and
  run-as-administrator (``library.set_launch_options``); shortcut buttons
  (``shortcuts.create`` for the desktop / Start menu); read-only facts:
  location (+ Open folder via ``launcher.open_folder``), size (computed when
  unknown), version, install date, playtime, store link. Only changed values
  are written on Save.
* :class:`ImportArchiveDialog` — local .zip/.7z/.rar (+ .001 split volumes),
  the catalog game it belongs to (live search; or a custom title when the
  game is not in the catalog), kind FULL/PATCH/ADDON (preset with ``kind=``,
  e.g. from a failed patch download; PATCH/ADDON need the base game installed
  and ignore the library folder; a FULL import of an installed game warns that
  it replaces that installation) and library folder →
  ``downloads.import_archive``. The created job is kept in ``job``.
* :class:`ImportFoldersDialog` — runs ``library.scan`` and lists unmanaged
  folders with a suggested catalog match (the scan's own match, else
  ``catalog.match_title(folder)``), editable through a search popup; checked
  rows (matched ones are pre-checked) → ``library.adopt``. Failures stay in
  the list with their error; ``imported`` counts successes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.errors import OperationCancelled
from anker_client.core.formatting import format_bytes, format_playtime, format_relative_time, pluralize
from anker_client.core.models import DownloadJob, DownloadKind, GameSummary, InstalledGame
from anker_client.core.paths import is_within
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Badge, Divider, EmptyState, LoadingOverlay, button, label, repolish
from anker_client.ui.widgets.library_catalog_picker import CatalogPicker, summary_caption
from anker_client.ui.widgets.library_common import IconTinter, format_date, tokenless

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".001")
ARCHIVE_FILTER = "Archives (*.zip *.7z *.rar *.001);;All files (*)"
PROGRAM_FILTER = "Programs (*.exe *.bat *.cmd);;All files (*)"
#: "v1.5.78", "1.0.2", "v2" — stripped from archive names before guessing the game title.
_VERSION_TOKEN = re.compile(r"(?i)\bv?\d+(?:\.\d+)+\b|\bv\d+\b")


# --- shared pieces ---------------------------------------------------------------------------


def _dialog_layout(dialog: QDialog, title: str, subtitle: str = "") -> tuple[QVBoxLayout, QLabel, QLabel]:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(14)
    heading = label(title, "title", wrap=True)
    sub = label(subtitle, "muted", wrap=True)
    sub.setVisible(bool(subtitle))
    header = QVBoxLayout()
    header.setSpacing(4)
    header.addWidget(heading)
    header.addWidget(sub)
    layout.addLayout(header)
    return layout, heading, sub


def _error_label() -> QLabel:
    lbl = label("", "error", wrap=True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.hide()
    return lbl


def _show_error(lbl: QLabel, text: str) -> None:
    lbl.setText(text)
    lbl.setVisible(bool(text))


def _section(title: str) -> QLabel:
    return label(title.upper(), "caption")


def _relative_inside(root: str, path: str) -> str | None:
    """``path`` relative to ``root`` when it lies strictly inside it, else None."""
    if not root or not path:
        return None
    if not is_within(path, root):
        return None
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:  # different drives on Windows
        return None
    if rel in (".", "") or rel.startswith(".."):
        return None
    return rel


def _clean_path(text: str) -> str:
    """A path typed or pasted by the user: trimmed and without surrounding quotes."""
    return text.strip().strip('"').strip()


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# --- executable picker -------------------------------------------------------------------------


class _ExeRow(QWidget):
    """List row: file name, folder, size and an optional badge."""

    def __init__(self, relative: str, size: int | None, badge: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        folder, name = os.path.split(relative)
        self.name = label(name or relative)
        font = self.name.font()
        font.setBold(True)
        self.name.setFont(font)
        self.folder = label(folder + os.sep if folder else "Game folder", "caption")
        self.size = label(format_bytes(size, unknown="—"), "muted")
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(self.name)
        text.addWidget(self.folder)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 12, 6)
        row.setSpacing(10)
        row.addLayout(text, 1)
        if badge:
            row.addWidget(Badge(badge, "accent" if badge == "Recommended" else ""), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.size, 0, Qt.AlignmentFlag.AlignVCenter)


class ExecutablePickerDialog(QDialog):
    """Lists ``library.executable_candidates`` (best first) + "Browse…"; saves via ``library.set_executable``."""

    def __init__(self, ctx: AppContext, install_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._install_id = install_id
        self._game: InstalledGame | None = None
        self._paths: list[str] = []
        self._saving = False
        self.setWindowTitle("Choose executable")
        self.setMinimumSize(580, 480)

        layout, self._heading, self._subtitle = _dialog_layout(
            self, "Choose the program to start", "Looking for programs in the game folder…")
        self._stack = QStackedWidget()
        self._stack.addWidget(LoadingOverlay("Looking for programs…"))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.list.itemDoubleClicked.connect(lambda _i: self._save())
        self._stack.addWidget(self.list)
        self._empty = EmptyState("search", "No programs found",
                                 "AnkerClient couldn't find a program in this game's folder. "
                                 "Use Browse… to pick the file that starts the game.")
        self._stack.addWidget(self._empty)
        layout.addWidget(self._stack, 1)
        self._error = _error_label()
        layout.addWidget(self._error)

        self.browse_button = button("Browse…", on_click=self._browse)
        self.browse_button.setEnabled(False)
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.save_button = button("Save", variant="primary", on_click=self._save)
        self.save_button.setEnabled(False)
        self.save_button.setDefault(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.browse_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self._load()

    # --- loading ----------------------------------------------------------------------------
    def _load(self) -> None:
        ctx, install_id = self._ctx, self._install_id

        def work(*, token: CancelToken) -> tuple[InstalledGame | None, list[tuple[str, int | None]]]:
            game = ctx.library.get(install_id)
            if game is None:
                return None, []
            try:
                candidates = list(ctx.library.executable_candidates(install_id))
            except OperationCancelled:
                raise
            except Exception as exc:  # Browse… still works without suggestions
                log.info("Executable candidates unavailable for %s: %s", install_id, exc)
                candidates = []
            if game.executable and game.executable not in candidates:
                candidates.append(game.executable)
            sized = []
            for rel in candidates:
                token.raise_if_cancelled()
                sized.append((rel, _file_size(os.path.join(game.path, rel))))
            return game, sized

        run_async(self, ctx.runner, work, on_result=self._on_loaded, on_error=self._on_load_failed)

    def _on_loaded(self, result: tuple[InstalledGame | None, list[tuple[str, int | None]]]) -> None:
        game, candidates = result
        if game is None:
            self._on_load_failed(LookupError("This game is no longer in your library."))
            return
        self._game = game
        self._heading.setText(f"Choose the program that starts {game.title}")
        self._subtitle.setText("The best match is listed first. Pick another one if the game doesn't start "
                               "or opens a launcher you don't want.")
        self.browse_button.setEnabled(True)
        for index, (rel, size) in enumerate(candidates):
            self._add_row(rel, size, recommended=index == 0)
        current = self._paths.index(game.executable) if game.executable in self._paths else 0
        if self._paths:
            self.list.setCurrentRow(current)
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(2)
        self._update_buttons()

    def _on_load_failed(self, exc: BaseException) -> None:
        self._stack.setCurrentIndex(2)
        self._empty.set_content("error", "Couldn't list the game's programs", error_text(exc))
        self.browse_button.setEnabled(self._game is not None)

    def _add_row(self, relative: str, size: int | None, *, recommended: bool = False) -> int:
        if relative in self._paths:
            return self._paths.index(relative)
        badge = "Recommended" if recommended else ""
        if self._game is not None and relative == self._game.executable:
            badge = "Current"
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, relative)
        item.setToolTip(relative)
        row = _ExeRow(relative, size, badge)
        item.setSizeHint(QSize(0, max(52, row.sizeHint().height())))
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self._paths.append(relative)
        return len(self._paths) - 1

    # --- interaction ------------------------------------------------------------------------
    def selected_path(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _update_buttons(self) -> None:
        self.save_button.setEnabled(bool(self.selected_path()) and not self._saving)

    def _ask_file(self, start_dir: str) -> str:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose the game's program", start_dir, PROGRAM_FILTER)
        return path

    def _browse(self) -> None:
        if self._game is None:
            return
        path = self._ask_file(self._game.path)
        if not path:
            return
        rel = _relative_inside(self._game.path, path)
        if rel is None:
            _show_error(self._error, f"Choose a program inside the game folder ({self._game.path}).")
            return
        _show_error(self._error, "")
        row = self._add_row(rel, _file_size(path))
        self._stack.setCurrentIndex(1)
        self.list.setCurrentRow(row)
        self._update_buttons()

    def _save(self) -> None:
        relative = self.selected_path()
        if not relative or self._saving:
            return
        self._saving = True
        self.save_button.setText("Saving…")
        self._update_buttons()
        _show_error(self._error, "")
        run_async(self, self._ctx.runner, tokenless(self._ctx.library.set_executable, self._install_id, relative),
                  on_result=lambda _r: self.accept(), on_error=self._on_save_failed)

    def _on_save_failed(self, exc: BaseException) -> None:
        self._saving = False
        self.save_button.setText("Save")
        self._update_buttons()
        _show_error(self._error, f"Couldn't save: {error_text(exc)}")


# --- game properties ----------------------------------------------------------------------------


@dataclass(slots=True)
class _PropsData:
    game: InstalledGame
    candidates: list[str]
    shortcuts: dict[str, bool] | None


class GamePropertiesDialog(QDialog):
    """Title, executable, launch arguments, run-as-admin, shortcuts, folder, size, version, playtime."""

    def __init__(self, ctx: AppContext, install_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._install_id = install_id
        self._game: InstalledGame | None = None
        self._saving = False
        self._size_handle: TaskHandle[Any] | None = None
        self._tint = IconTinter(self)
        self.setWindowTitle("Properties")
        self.setMinimumWidth(620)

        layout, self._heading, self._subtitle = _dialog_layout(self, "Properties", "Loading…")
        self._stack = QStackedWidget()
        self._stack.addWidget(LoadingOverlay("Loading game details…"))
        self._stack.addWidget(self._build_form())
        self._failure = EmptyState("error", "Couldn't load this game", "")
        self._stack.addWidget(self._failure)
        layout.addWidget(self._stack, 1)
        self._error = _error_label()
        layout.addWidget(self._error)

        self.cancel_button = button("Cancel", on_click=self.reject)
        self.save_button = button("Save", variant="primary", on_click=self._save)
        self.save_button.setDefault(True)
        self.save_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self._load()

    def _build_form(self) -> QWidget:
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Name shown in your library")
        self.title_edit.textChanged.connect(lambda _t: self._update_buttons())
        form.addRow("Name", self.title_edit)

        self.exe_combo = QComboBox()
        self.exe_combo.setMinimumContentsLength(28)
        self.exe_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.exe_combo.currentIndexChanged.connect(lambda _i: self._update_buttons())
        self.exe_browse = button("Browse…", on_click=self._browse_exe)
        exe_row = QHBoxLayout()
        exe_row.setSpacing(8)
        exe_row.addWidget(self.exe_combo, 1)
        exe_row.addWidget(self.exe_browse)
        form.addRow("Program", exe_row)

        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText("Optional, e.g. -windowed -skipintro")
        self.args_edit.textChanged.connect(lambda _t: self._update_buttons())
        form.addRow("Launch options", self.args_edit)
        self.admin_check = QCheckBox("Run as administrator")
        self.admin_check.toggled.connect(lambda _c: self._update_buttons())
        form.addRow("", self.admin_check)
        col.addLayout(form)

        col.addWidget(Divider())
        col.addWidget(_section("Shortcuts"))
        self.desktop_button = button("Create desktop shortcut", on_click=lambda: self._create_shortcut(desktop=True))
        self._tint.set(self.desktop_button, "shortcut", size=16)
        self.start_menu_button = button("Add to Start menu", on_click=lambda: self._create_shortcut(desktop=False))
        self._tint.set(self.start_menu_button, "plus", size=16)
        self.shortcut_status = label("", "caption", wrap=True)
        shortcut_row = QHBoxLayout()
        shortcut_row.setSpacing(8)
        shortcut_row.addWidget(self.desktop_button)
        shortcut_row.addWidget(self.start_menu_button)
        shortcut_row.addStretch(1)
        col.addLayout(shortcut_row)
        col.addWidget(self.shortcut_status)

        col.addWidget(Divider())
        col.addWidget(_section("Details"))
        facts = QFormLayout()
        facts.setHorizontalSpacing(16)
        facts.setVerticalSpacing(6)
        facts.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.fact_location = label("", selectable=True)
        self.fact_location.setWordWrap(True)
        self.open_folder_button = button("Open folder", variant="link", on_click=self._open_folder)
        location_box = QVBoxLayout()
        location_box.setSpacing(2)
        location_box.addWidget(self.fact_location)
        location_box.addWidget(self.open_folder_button, 0, Qt.AlignmentFlag.AlignLeft)
        facts.addRow(self._fact_label("Location"), location_box)
        self.fact_size = label("—")
        self.fact_version = label("—")
        self.fact_installed = label("—")
        self.fact_playtime = label("—")
        self.fact_store = label("—", selectable=True)
        for name, widget in (("Size", self.fact_size), ("Version", self.fact_version),
                             ("Installed", self.fact_installed), ("Playtime", self.fact_playtime),
                             ("Store page", self.fact_store)):
            facts.addRow(self._fact_label(name), widget)
        col.addLayout(facts)
        col.addStretch(1)
        return body

    @staticmethod
    def _fact_label(text: str) -> QLabel:
        return label(text, "muted")

    # --- loading -------------------------------------------------------------------------------
    def _load(self) -> None:
        ctx, install_id = self._ctx, self._install_id

        def work(*, token: CancelToken) -> _PropsData | None:
            game = ctx.library.get(install_id)
            if game is None:
                return None
            try:
                candidates = list(ctx.library.executable_candidates(install_id))
            except OperationCancelled:
                raise
            except Exception as exc:  # the form still works with the current program only
                log.info("Executable candidates unavailable for %s: %s", install_id, exc)
                candidates = []
            token.raise_if_cancelled()
            shortcut_state = None
            try:
                state = ctx.shortcuts.exists(game.title)
                shortcut_state = dict(state) if isinstance(state, dict) else None
            except Exception as exc:
                log.debug("Shortcut state unavailable: %s", exc)
            return _PropsData(game, candidates, shortcut_state)

        run_async(self, ctx.runner, work, on_result=self._on_loaded, on_error=self._on_load_failed)

    def _on_loaded(self, data: _PropsData | None) -> None:
        if data is None:
            self._on_load_failed(LookupError("This game is no longer in your library."))
            return
        game = data.game
        self._game = game
        self.setWindowTitle(f"Properties — {game.title}")
        self._heading.setText(game.title)
        self._subtitle.setText("Managed by AnkerClient" if game.managed else
                               "Not managed by AnkerClient yet — saving creates its manifest")
        self.title_edit.setText(game.title)
        self.exe_combo.blockSignals(True)
        self.exe_combo.clear()
        if not game.executable:
            self.exe_combo.addItem("Not chosen yet", "")
        paths = list(data.candidates)
        if game.executable and game.executable not in paths:
            paths.insert(0, game.executable)
        for rel in paths:
            self.exe_combo.addItem(rel, rel)
        self.exe_combo.setCurrentIndex(max(0, self.exe_combo.findData(game.executable)))
        self.exe_combo.blockSignals(False)
        self.args_edit.setText(game.launch_args)
        self.admin_check.setChecked(game.run_as_admin)

        self.fact_location.setText(game.path)
        self.fact_version.setText(game.version or "Unknown")
        self.fact_installed.setText(format_date(game.installed_at, unknown="Unknown"))
        played = format_playtime(game.playtime_seconds)
        if game.last_played:
            played += f" · last played {format_relative_time(game.last_played)}"
        self.fact_playtime.setText(played)
        self.fact_store.setText((game.slug and f"ankergames.net/game/{game.slug}") or "Not linked to a store page")
        self._set_size(game.size_bytes)
        if game.size_bytes is None:
            self._compute_size()
        self._render_shortcut_state(data.shortcuts)
        self._stack.setCurrentIndex(1)
        self._update_buttons()

    def _on_load_failed(self, exc: BaseException) -> None:
        self._failure.set_content("error", "Couldn't load this game", error_text(exc))
        self._stack.setCurrentIndex(2)
        self._subtitle.setText("")
        self.save_button.setEnabled(False)

    def _render_shortcut_state(self, state: dict[str, bool] | None) -> None:
        if not state:
            self.shortcut_status.setText("Shortcuts start the game directly, without opening AnkerClient.")
            return
        places = [name for key, name in (("desktop", "the desktop"), ("start_menu", "the Start menu"))
                  if state.get(key)]
        self.shortcut_status.setText(f"Shortcut exists on {' and '.join(places)}." if places
                                     else "No shortcuts yet.")

    def _set_size(self, size: int | None) -> None:
        self.fact_size.setText(format_bytes(size, unknown="Unknown"))

    def _compute_size(self) -> None:
        self.fact_size.setText("Calculating…")
        self._size_handle = run_async(self, self._ctx.runner, self._ctx.library.compute_size, self._install_id,
                                      on_result=self._set_size, on_error=lambda _e: self._set_size(None))

    # --- editing -------------------------------------------------------------------------------
    def _selected_exe(self) -> str:
        return str(self.exe_combo.currentData() or "")

    def _changes(self) -> dict[str, Any]:
        game = self._game
        if game is None:
            return {}
        changes: dict[str, Any] = {}
        title = self.title_edit.text().strip()
        if title and title != game.title:
            changes["title"] = title
        exe = self._selected_exe()
        if exe and exe != game.executable:
            changes["executable"] = exe
        args = self.args_edit.text().strip()
        admin = self.admin_check.isChecked()
        if args != game.launch_args or admin != game.run_as_admin:
            changes["launch"] = (args, admin)
        return changes

    def _update_buttons(self) -> None:
        loaded = self._game is not None
        self.save_button.setEnabled(loaded and not self._saving and bool(self._changes())
                                    and bool(self.title_edit.text().strip()))
        has_exe = bool(self._selected_exe())
        for btn in (self.desktop_button, self.start_menu_button):
            btn.setEnabled(loaded and has_exe)
            btn.setToolTip("" if has_exe else "Choose the program first.")

    def _ask_file(self, start_dir: str) -> str:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose the game's program", start_dir, PROGRAM_FILTER)
        return path

    def _browse_exe(self) -> None:
        if self._game is None:
            return
        path = self._ask_file(self._game.path)
        if not path:
            return
        rel = _relative_inside(self._game.path, path)
        if rel is None:
            _show_error(self._error, f"Choose a program inside the game folder ({self._game.path}).")
            return
        _show_error(self._error, "")
        index = self.exe_combo.findData(rel)
        if index < 0:
            self.exe_combo.addItem(rel, rel)
            index = self.exe_combo.count() - 1
        self.exe_combo.setCurrentIndex(index)

    def _create_shortcut(self, *, desktop: bool) -> None:
        game = self._game
        exe = self._selected_exe()
        if game is None or not exe:
            return
        title = self.title_edit.text().strip() or game.title
        target = os.path.normpath(os.path.join(game.path, exe))
        create = tokenless(self._ctx.shortcuts.create, title, target, arguments=self.args_edit.text().strip(),
                           desktop=desktop, start_menu=not desktop)
        btn = self.desktop_button if desktop else self.start_menu_button
        btn.setEnabled(False)
        where = "on the desktop" if desktop else "in the Start menu"

        def done(_paths: object) -> None:
            self.shortcut_status.setText(f"Shortcut created {where}.")
            self.shortcut_status.setProperty("role", "success")
            repolish(self.shortcut_status)

        def failed(exc: BaseException) -> None:
            self.shortcut_status.setText(f"Couldn't create the shortcut: {error_text(exc)}")
            self.shortcut_status.setProperty("role", "error")
            repolish(self.shortcut_status)

        run_async(self, self._ctx.runner, create, on_result=done, on_error=failed, on_finished=self._update_buttons)

    def _open_folder(self) -> None:
        run_async(self, self._ctx.runner, tokenless(self._ctx.launcher.open_folder, self._install_id),
                  on_error=lambda exc: _show_error(self._error, f"Couldn't open the folder: {error_text(exc)}"))

    def _save(self) -> None:
        changes = self._changes()
        if not changes or self._saving:
            return
        self._saving = True
        self.save_button.setText("Saving…")
        self._update_buttons()
        _show_error(self._error, "")
        library, install_id = self._ctx.library, self._install_id

        def work(*, token: CancelToken) -> None:
            if "executable" in changes:
                library.set_executable(install_id, changes["executable"])
            token.raise_if_cancelled()
            if "launch" in changes:
                args, admin = changes["launch"]
                library.set_launch_options(install_id, args=args, run_as_admin=admin)
            token.raise_if_cancelled()
            if "title" in changes:
                library.rename(install_id, changes["title"])

        run_async(self, self._ctx.runner, work, on_result=lambda _r: self.accept(), on_error=self._on_save_failed)

    def _on_save_failed(self, exc: BaseException) -> None:
        self._saving = False
        self.save_button.setText("Save")
        _show_error(self._error, f"Couldn't save: {error_text(exc)}")
        self._update_buttons()

    def done(self, result: int) -> None:
        if self._size_handle is not None:
            self._size_handle.cancel()
        super().done(result)


# --- import archive -------------------------------------------------------------------------------

_KIND_CHOICES = (
    (DownloadKind.FULL, "Full game"),
    (DownloadKind.PATCH, "Update / patch"),
    (DownloadKind.ADDON, "Add-on (DLC, language pack…)"),
)


class ImportArchiveDialog(QDialog):
    """Pick a local archive + the catalog game it belongs to (search) → ``downloads.import_archive``."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *, slug: str = "", title: str = "",
                 archive_path: str = "", kind: DownloadKind = DownloadKind.FULL) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._archive_ok = False
        self._validated_path = ""
        self._archive_seq = 0
        self._base_seq = 0
        self._base_installed: bool | None = None
        self._importing = False
        self.job: DownloadJob | None = None
        self.setWindowTitle("Import archive")
        self.setMinimumSize(600, 640)

        layout, _heading, _sub = _dialog_layout(
            self, "Import an archive",
            "Install a game from an archive you downloaded yourself, for example from an external file host. "
            "AnkerClient extracts it like a normal download and never deletes your file.")

        layout.addWidget(_section("Archive"))
        self.archive_edit = QLineEdit()
        self.archive_edit.setPlaceholderText("Choose a .zip, .7z or .rar file")
        self.archive_edit.editingFinished.connect(lambda: self._inspect_archive(self.archive_edit.text()))
        self.archive_edit.textEdited.connect(self._on_archive_edited)
        self.archive_browse = button("Browse…", on_click=self._browse_archive)
        archive_row = QHBoxLayout()
        archive_row.setSpacing(8)
        archive_row.addWidget(self.archive_edit, 1)
        archive_row.addWidget(self.archive_browse)
        layout.addLayout(archive_row)
        self.archive_info = label("", "caption", wrap=True)
        self.archive_info.hide()
        layout.addWidget(self.archive_info)

        layout.addWidget(_section("Game"))
        self.picker = CatalogPicker(ctx, self)
        self.picker.selection_changed.connect(self._on_game_changed)
        layout.addWidget(self.picker, 1)

        options = QHBoxLayout()
        options.setSpacing(16)
        kind_box = QVBoxLayout()
        kind_box.setSpacing(6)
        kind_box.addWidget(_section("Contents"))
        self.kind_combo = QComboBox()
        for choice, text in _KIND_CHOICES:
            self.kind_combo.addItem(text, choice)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(kind)))
        self.kind_combo.currentIndexChanged.connect(lambda _i: self._on_game_changed())
        kind_box.addWidget(self.kind_combo)
        library_box = QVBoxLayout()
        library_box.setSpacing(6)
        library_box.addWidget(_section("Install to"))
        self.library_combo = QComboBox()
        settings = ctx.settings.get()
        for folder in settings.library_dirs:
            self.library_combo.addItem(folder, folder)
        self.library_combo.setCurrentIndex(max(0, self.library_combo.findData(settings.default_library)))
        library_box.addWidget(self.library_combo)
        options.addLayout(kind_box, 1)
        options.addLayout(library_box, 1)
        layout.addLayout(options)

        self.notice = QFrame()
        self.notice.setProperty("role", "notice")
        self.notice.setProperty("tone", "info")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(12, 8, 12, 8)
        self.notice_text = label("", wrap=True)
        notice_layout.addWidget(self.notice_text)
        self.notice.hide()
        layout.addWidget(self.notice)

        self._error = _error_label()
        layout.addWidget(self._error)
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.import_button = button("Import", variant="primary", on_click=self._import)
        self.import_button.setDefault(True)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)

        if archive_path:
            self.archive_edit.setText(archive_path)
            self._inspect_archive(archive_path)
        if title or slug:
            self.picker.set_query(title or slug.replace("-", " "), prefer_slug=slug)
        self._on_game_changed()

    # --- archive ---------------------------------------------------------------------------------
    def _ask_archive(self) -> str:
        start = os.path.dirname(self.archive_path()) or os.path.expanduser("~")
        path, _filter = QFileDialog.getOpenFileName(self, "Choose an archive", start, ARCHIVE_FILTER)
        return path

    def _browse_archive(self) -> None:
        path = self._ask_archive()
        if path:
            self.archive_edit.setText(os.path.normpath(path))
            self._inspect_archive(path)
            if not self.picker.search.text().strip():
                self.picker.set_query(_guess_title(path))

    def _on_archive_edited(self, _text: str) -> None:
        # typed paths are re-validated when editing finishes; until then Import stays disabled
        self._archive_seq += 1
        self._archive_ok = False
        self._update_buttons()

    def archive_path(self) -> str:
        return _clean_path(self.archive_edit.text())

    def _inspect_archive(self, path: str) -> None:
        path = _clean_path(path)
        if self._archive_ok and path == self._validated_path:
            return  # e.g. focus left the unchanged field: keep Import enabled (the click must not be lost)
        self._archive_seq += 1
        seq = self._archive_seq
        self._archive_ok = False
        self._validated_path = ""
        self._set_archive_info("", "caption")
        self._update_buttons()
        if not path:
            return
        if not path.casefold().endswith(ARCHIVE_SUFFIXES):
            self._set_archive_info("Choose a .zip, .7z or .rar archive.", "error")
            return

        def work(*, token: CancelToken) -> int:
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            return os.path.getsize(path)

        def ok(size: int) -> None:
            if seq != self._archive_seq:
                return
            self._archive_ok = True
            self._validated_path = path
            self._set_archive_info(f"{os.path.basename(path)} · {format_bytes(size)}", "caption")
            self._update_buttons()

        def failed(_exc: BaseException) -> None:
            if seq == self._archive_seq:
                self._set_archive_info("That file doesn't exist or can't be read.", "error")

        run_async(self, self._ctx.runner, work, on_result=ok, on_error=failed)

    def _set_archive_info(self, text: str, role: str) -> None:
        self.archive_info.setText(text)
        self.archive_info.setVisible(bool(text))
        if self.archive_info.property("role") != role:
            self.archive_info.setProperty("role", role)
            repolish(self.archive_info)

    # --- game / kind ------------------------------------------------------------------------------
    def selected_kind(self) -> DownloadKind:
        kind = self.kind_combo.currentData()
        return kind if isinstance(kind, DownloadKind) else DownloadKind.FULL

    def _on_game_changed(self) -> None:
        summary, title = self.picker.selection()
        overlay = self.selected_kind() is not DownloadKind.FULL
        self.library_combo.setEnabled(not overlay and self.library_combo.count() > 1)
        self._base_installed = None
        self._base_seq += 1
        if not overlay:
            self.notice.hide()
            if summary is not None:
                self._check_existing_install(summary, overlay=False)
        elif summary is None:
            self._show_notice("info", "Updates and add-ons are extracted over an existing installation. "
                                      "Pick the game from the catalog so AnkerClient can find it."
                              if not title else
                              f"“{title}” isn't linked to the store, so AnkerClient can't check that it is "
                              "installed. Make sure the full game is installed first.")
        else:
            self._check_existing_install(summary, overlay=True)
        self._update_buttons()

    def _check_existing_install(self, summary: GameSummary, *, overlay: bool) -> None:
        """PATCH/ADDON: the base game must be installed. FULL: warn that an installed copy is replaced."""
        seq = self._base_seq
        if overlay:
            self._show_notice("info", f"Checking that {summary.title} is installed…")

        def done(game: InstalledGame | None) -> None:
            if seq != self._base_seq:
                return
            if not overlay:
                # The download manager re-installs a managed game of the same slug in place.
                if game is not None and game.managed:
                    self._show_notice("warning", f"{game.title} is already installed in {game.path}. Importing a "
                                                 "full game replaces that installation; your playtime is kept.")
                return
            self._base_installed = game is not None
            if game is not None:
                self._show_notice("info", f"Extracted over your installation in {game.path}.")
            else:
                self._show_notice("warning", f"{summary.title} isn't installed. Install or import the full game "
                                             "first, then add updates and add-ons.")
            self._update_buttons()

        run_async(self, self._ctx.runner, tokenless(self._ctx.library.find_by_slug, summary.slug),
                  on_result=done, on_error=lambda _e: done(None))

    def _show_notice(self, tone: str, text: str) -> None:
        self.notice_text.setText(text)
        if self.notice.property("tone") != tone:
            self.notice.setProperty("tone", tone)
            repolish(self.notice)
        self.notice.show()

    def _update_buttons(self) -> None:
        _summary, title = self.picker.selection()
        overlay_blocked = self.selected_kind() is not DownloadKind.FULL and self._base_installed is False
        self.import_button.setEnabled(self._archive_ok and bool(title) and not overlay_blocked
                                      and not self._importing)

    # --- import -------------------------------------------------------------------------------------
    def _import(self) -> None:
        summary, title = self.picker.selection()
        path = self.archive_path()
        if not (self._archive_ok and title and path) or self._importing:
            return
        self._importing = True
        self.import_button.setText("Importing…")
        self._update_buttons()
        _show_error(self._error, "")
        kind = self.selected_kind()
        library_root = self.library_combo.currentData() if kind is DownloadKind.FULL else None
        call = tokenless(self._ctx.downloads.import_archive, path, slug=summary.slug if summary else "",
                         title=title, kind=kind, library_root=library_root or None,
                         cover_url=summary.cover_url if summary else "")
        run_async(self, self._ctx.runner, call, on_result=self._on_imported, on_error=self._on_import_failed)

    def _on_imported(self, job: DownloadJob) -> None:
        self.job = job
        self.accept()

    def _on_import_failed(self, exc: BaseException) -> None:
        self._importing = False
        self.import_button.setText("Import")
        _show_error(self._error, f"Couldn't start the import: {error_text(exc)}")
        self._update_buttons()

    def done(self, result: int) -> None:
        self.picker.shutdown()
        super().done(result)


def _guess_title(path: str) -> str:
    """``Hollow-Knight_v1.5.zip`` → ``Hollow Knight`` (a starting point for the catalog search)."""
    stem = os.path.basename(path)
    for suffix in (".001", ".zip", ".7z", ".rar"):
        if stem.casefold().endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = stem.replace("_", " ").replace("-", " ")
    stem = _VERSION_TOKEN.sub(" ", stem)
    words = stem.replace(".", " ").split()
    return " ".join(words[:6])


# --- import folders --------------------------------------------------------------------------------


@dataclass(slots=True)
class _FolderRow:
    game: InstalledGame
    match: GameSummary | None
    error: str = ""


class _MatchDialog(QDialog):
    """Search popup to link one folder to a catalog game (or to none)."""

    def __init__(self, ctx: AppContext, folder: str, current: GameSummary | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.choice: GameSummary | None = current
        self.setWindowTitle("Link to a store game")
        self.setMinimumSize(480, 440)
        layout, _h, _s = _dialog_layout(self, f"Which game is “{folder}”?",
                                        "Linking a folder lets AnkerClient show artwork and check for updates.")
        self.picker = CatalogPicker(ctx, self, allow_custom=False)
        layout.addWidget(self.picker, 1)
        self.unlink_button = button("Don't link", variant="ghost", on_click=self._unlink)
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.use_button = button("Use this game", variant="primary", on_click=self._use)
        self.use_button.setEnabled(False)
        self.picker.selection_changed.connect(lambda: self.use_button.setEnabled(self.picker.has_selection()))
        row = QHBoxLayout()
        row.addWidget(self.unlink_button)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.use_button)
        layout.addLayout(row)
        self.picker.set_query(current.title if current else folder, prefer_slug=current.slug if current else "")

    def _use(self) -> None:
        summary, _title = self.picker.selection()
        if summary is not None:
            self.choice = summary
            self.accept()

    def _unlink(self) -> None:
        self.choice = None
        self.accept()

    def done(self, result: int) -> None:
        self.picker.shutdown()
        super().done(result)


class ImportFoldersDialog(QDialog):
    """Lists unmanaged folders in the libraries with suggested catalog matches → ``library.adopt``."""

    COL_CHECK, COL_FOLDER, COL_MATCH, COL_CHANGE = range(4)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: list[_FolderRow] = []
        self._loading = False
        self._importing = False
        self._load_handle: TaskHandle[Any] | None = None
        self._match_dialog: _MatchDialog | None = None
        self._checks: list[QCheckBox] = []
        self.imported = 0
        self.setWindowTitle("Import existing games")
        self.setMinimumSize(760, 540)

        layout, _h, _s = _dialog_layout(
            self, "Import existing games",
            "These folders are in your library folders but aren't managed by AnkerClient yet. Importing links "
            "each one to its store page so playtime and updates are tracked. Nothing is moved or deleted; "
            "AnkerClient only adds a small hidden file to each folder.")
        self._stack = QStackedWidget()
        self._stack.addWidget(LoadingOverlay("Looking for game folders…"))
        self._empty = EmptyState("folder", "No new game folders found", self._empty_message())
        self._stack.addWidget(self._empty)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Folder", "Store match", ""])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.COL_CHECK, 40)
        header.setSectionResizeMode(self.COL_FOLDER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_MATCH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_CHANGE, QHeaderView.ResizeMode.Fixed)
        header.setHighlightSections(False)
        self.table.setColumnWidth(self.COL_CHANGE, 110)
        self._stack.addWidget(self.table)
        layout.addWidget(self._stack, 1)

        self._error = _error_label()
        layout.addWidget(self._error)
        self.summary = label("", "muted")
        self.select_all = QCheckBox("Select all")
        self.select_all.clicked.connect(self._toggle_all)
        self.rescan_button = button("Rescan", variant="ghost", on_click=self._load)
        self.cancel_button = button("Cancel", on_click=self.reject)
        self.import_button = button("Import", variant="primary", on_click=self._import)
        self.import_button.setDefault(True)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.select_all)
        row.addWidget(self.summary)
        row.addStretch(1)
        row.addWidget(self.rescan_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.import_button)
        layout.addLayout(row)
        self._load()

    def _empty_message(self) -> str:
        folders = ", ".join(self._ctx.settings.get().library_dirs)
        return (f"Copy game folders into a library folder ({folders}) and they will show up here. "
                "Folders already managed by AnkerClient are not listed.")

    # --- loading ---------------------------------------------------------------------------------
    def _load(self) -> None:
        if self._importing:
            return
        if self._load_handle is not None:
            self._load_handle.cancel()
        self._loading = True
        self._stack.setCurrentIndex(0)
        _show_error(self._error, "")
        self._update_buttons()
        ctx = self._ctx

        def work(*, token: CancelToken) -> list[_FolderRow]:
            games = ctx.library.scan(token=token)
            rows = []
            for game in sorted((g for g in games if not g.managed), key=lambda g: g.folder_name.casefold()):
                token.raise_if_cancelled()
                match = None
                try:
                    match = ctx.catalog.get(game.slug) if game.slug else None
                    match = match or ctx.catalog.match_title(game.folder_name)
                except Exception as exc:  # a missing catalog only means "no suggestion"
                    log.debug("No catalog match for %s: %s", game.folder_name, exc)
                rows.append(_FolderRow(game, match))
            return rows

        self._load_handle = run_async(self, ctx.runner, work, on_result=self._on_loaded,
                                      on_error=self._on_load_failed)

    def _on_loaded(self, rows: list[_FolderRow]) -> None:
        self._loading = False
        self._rows = list(rows)
        self._populate(checked={r.game.install_id for r in rows if r.match is not None})
        self._update_buttons()

    def _on_load_failed(self, exc: BaseException) -> None:
        self._loading = False
        self._empty.set_content("error", "Couldn't scan your library folders", error_text(exc))
        self._stack.setCurrentIndex(1)
        self._update_buttons()

    def _populate(self, *, checked: set[str]) -> None:
        self.table.setRowCount(0)
        self._checks = []
        for row_index, row in enumerate(self._rows):
            self.table.insertRow(row_index)
            check = QCheckBox()
            check.setChecked(row.game.install_id in checked)
            check.setToolTip(f"Import {row.game.folder_name}")
            check.toggled.connect(lambda _c: self._update_buttons())
            check_holder = QWidget()
            check_layout = QHBoxLayout(check_holder)
            check_layout.setContentsMargins(12, 0, 0, 0)
            check_layout.addWidget(check)
            self._checks.append(check)
            self.table.setCellWidget(row_index, self.COL_CHECK, check_holder)
            folder = QTableWidgetItem(row.game.folder_name)
            folder.setFlags(Qt.ItemFlag.ItemIsEnabled)
            folder.setToolTip(row.game.path)
            folder.setData(Qt.ItemDataRole.UserRole, row.game.install_id)
            self.table.setItem(row_index, self.COL_FOLDER, folder)
            self.table.setItem(row_index, self.COL_MATCH, self._match_item(row))
            change = button("Change…", size="sm", on_click=lambda r=row_index: self._change_match(r))
            change.setObjectName(f"change_{row_index}")
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(4, 0, 8, 0)
            holder_layout.addWidget(change)
            self.table.setCellWidget(row_index, self.COL_CHANGE, holder)
        self._stack.setCurrentIndex(2 if self._rows else 1)
        if not self._rows:
            self._empty.set_content("folder", "No new game folders found", self._empty_message())

    def _match_item(self, row: _FolderRow) -> QTableWidgetItem:
        pal = palette.current()
        if row.error:
            item = QTableWidgetItem(row.error)
            item.setForeground(QColor(pal.danger))
            item.setToolTip(row.error)
        elif row.match is not None:
            caption = summary_caption(row.match)
            item = QTableWidgetItem(f"{row.match.title}" + (f"  ·  {caption}" if caption else ""))
            item.setToolTip(row.match.page_url)
        else:
            item = QTableWidgetItem("No match — keeps the folder name")
            item.setForeground(QColor(pal.text_muted))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    # --- editing ------------------------------------------------------------------------------------
    def checked_ids(self) -> list[str]:
        return [row.game.install_id for row, check in zip(self._rows, self._checks, strict=False)
                if check.isChecked()]

    def set_checked(self, row_index: int, checked: bool) -> None:
        if 0 <= row_index < len(self._checks):
            self._checks[row_index].setChecked(checked)

    def set_match(self, row_index: int, match: GameSummary | None) -> None:
        if not 0 <= row_index < len(self._rows):
            return
        row = self._rows[row_index]
        row.match = match
        row.error = ""
        self.table.setItem(row_index, self.COL_MATCH, self._match_item(row))
        if match is not None:
            self.set_checked(row_index, True)
        self._update_buttons()

    def _change_match(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._rows) or self._importing:
            return
        row = self._rows[row_index]
        dialog = _MatchDialog(self._ctx, row.game.folder_name, row.match, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.accepted.connect(lambda d=dialog, r=row_index: self.set_match(r, d.choice))
        self._match_dialog = dialog
        dialog.open()

    def _toggle_all(self, checked: bool) -> None:
        for check in self._checks:
            check.blockSignals(True)
            check.setChecked(checked)
            check.blockSignals(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        count = len(self.checked_ids())
        total = self.table.rowCount()
        busy = self._loading or self._importing
        self.import_button.setEnabled(count > 0 and not busy)
        if not self._importing:
            self.import_button.setText(f"Import {pluralize(count, 'game')}" if count else "Import")
        self.rescan_button.setEnabled(not busy)
        self.select_all.setEnabled(total > 0 and not busy)
        self.select_all.blockSignals(True)
        self.select_all.setChecked(total > 0 and count == total)
        self.select_all.blockSignals(False)
        self.summary.setText(f"{count} of {pluralize(total, 'folder')} selected" if total else "")
        self.table.setEnabled(not busy)

    # --- import ---------------------------------------------------------------------------------------
    def _import(self) -> None:
        ids = set(self.checked_ids())
        selected = [r for r in self._rows if r.game.install_id in ids]
        if not selected or self._importing:
            return
        self._importing = True
        self.import_button.setText("Importing…")
        _show_error(self._error, "")
        self._update_buttons()
        library = self._ctx.library
        jobs = [(r.game.install_id, r.match.slug if r.match else "", r.match.title if r.match else r.game.title)
                for r in selected]

        def work(*, token: CancelToken) -> dict[str, str]:
            results: dict[str, str] = {}
            for install_id, slug, title in jobs:
                token.raise_if_cancelled()
                try:
                    library.adopt(install_id, slug=slug, title=title)
                    results[install_id] = ""
                except Exception as exc:
                    log.warning("Importing %s failed: %s", install_id, exc)
                    results[install_id] = error_text(exc)
            return results

        run_async(self, self._ctx.runner, work, on_result=self._on_imported, on_error=self._on_import_failed)

    def _on_imported(self, results: dict[str, str]) -> None:
        self._importing = False
        succeeded = {i for i, err in results.items() if not err}
        self.imported += len(succeeded)
        failed = {i: err for i, err in results.items() if err}
        if not failed:
            self.accept()
            return
        remaining = [r for r in self._rows if r.game.install_id not in succeeded]
        for row in remaining:
            row.error = failed.get(row.game.install_id, "")
        self._rows = remaining
        self._populate(checked=set(failed))
        _show_error(self._error, f"{pluralize(len(failed), 'folder')} couldn't be imported. "
                                 "Fix the problem and try again.")
        self._update_buttons()

    def _on_import_failed(self, exc: BaseException) -> None:
        self._importing = False
        _show_error(self._error, f"Import failed: {error_text(exc)}")
        self._update_buttons()

    def done(self, result: int) -> None:
        if self._load_handle is not None and not self._importing:
            self._load_handle.cancel()
        super().done(result)


__all__ = ["ExecutablePickerDialog", "GamePropertiesDialog", "ImportArchiveDialog", "ImportFoldersDialog"]

