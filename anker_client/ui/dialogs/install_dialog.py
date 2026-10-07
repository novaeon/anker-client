"""Install options dialog shown by ``MainWindow.request_install``.

What the user picks
* Download option — radio cards (label, kind badge "Full game" / "Update" /
  "Add-on", size). The option passed in (or ``details.primary_option``) is
  preselected.
* Library folder — combo of ``settings.library_dirs`` (default
  ``settings.default_library``) with the free space of each folder's volume.
  PATCH/ADDON options and reinstalls go to the existing install's library, so
  the combo is locked there.
* "Create a desktop shortcut" / "Add to the Start menu" — initialised from the
  settings and written back to them when the dialog is accepted (full-game
  installs only; overlays never create shortcuts).

What the dialog checks
* PATCH/ADDON need the base game installed: otherwise a notice explains it and
  Install stays disabled (with "Choose the full game" when one exists). A patch
  whose ``from_version`` differs from the installed version gets a warning.
* FULL over an existing install: overwrite warning naming the folder; the
  button reads "Reinstall".
* Disk space (off the GUI thread): ``install.diskspace.required_bytes`` per
  volume vs ``free_bytes``. When a volume is short Install is disabled and an
  "Install anyway" link overrides it. Unknown sizes or a failing check never
  block the install (shown as a caption).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anker_client.constants import LIBRARY_WORK_DIRNAME
from anker_client.core.formatting import format_bytes, normalize_version, parse_size
from anker_client.core.models import DownloadKind, DownloadOption, GameDetails, InstalledGame
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import Badge, button, label, repolish
from anker_client.ui.widgets.toast import breakable

log = logging.getLogger(__name__)

KIND_LABELS = {DownloadKind.FULL: "Full game", DownloadKind.PATCH: "Update", DownloadKind.ADDON: "Add-on"}
KIND_BADGES = {DownloadKind.FULL: "accent", DownloadKind.PATCH: "warning", DownloadKind.ADDON: ""}
_SIZE_SUFFIX_RE = re.compile(r"\s*\(([^()]*\d[^()]*)\)\s*$")
_FOLDER_TEXT_WIDTH = 330  # px for the path in the library combo (the dialog is at least 580 px wide)


# --- pure helpers ----------------------------------------------------------------------------------


def option_title(option: DownloadOption) -> str:
    """The option label without a trailing "(124 MB)" size suffix (shown separately)."""
    match = _SIZE_SUFFIX_RE.search(option.label)
    if match and parse_size(match.group(1)) is not None:
        return option.label[: match.start()].strip() or option.label
    return option.label


def option_size(details: GameDetails, option: DownloadOption) -> int | None:
    """Best known archive size for ``option`` in bytes."""
    size = parse_size(option.size_text)
    if size is None:
        match = _SIZE_SUFFIX_RE.search(option.label)
        size = parse_size(match.group(1)) if match else None
    if size is not None:
        return size
    if option.kind is DownloadKind.FULL:
        return details.size_bytes or parse_size(details.size_text)
    return None


def option_size_text(details: GameDetails, option: DownloadOption) -> str:
    size = option_size(details, option)
    if size is not None:
        return format_bytes(size)
    return details.size_text if option.kind is DownloadKind.FULL and details.size_text else "Size unknown"


def download_dir_for(configured: str, library_root: str) -> str:
    return configured or os.path.join(library_root, LIBRARY_WORK_DIRNAME, "downloads")


@dataclass(slots=True)
class SpaceCheck:
    required: dict[str, int] = field(default_factory=dict)
    free: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def known(self) -> bool:
        return bool(self.required) and not self.error

    def shortfalls(self) -> list[tuple[str, int, int]]:
        """``(volume, required, free)`` for every volume without enough space."""
        out = []
        for volume, needed in self.required.items():
            free = self.free.get(volume)
            if free is not None and free < needed:
                out.append((volume, needed, free))
        return out

    @property
    def sufficient(self) -> bool:
        return not self.shortfalls()


def compute_space(size: int | None, download_dir: str, library_root: str, *, token: CancelToken) -> SpaceCheck:
    """Worker-thread disk check (``install.diskspace`` is looked up at call time so tests can patch it)."""
    from anker_client.services.install import diskspace

    required = diskspace.required_bytes(size, download_dir=download_dir, library_root=library_root)
    token.raise_if_cancelled()
    free = {volume: diskspace.free_bytes(volume) for volume in required}
    return SpaceCheck(required=dict(required), free=free)


def compute_free_by_folder(folders: list[str], *, token: CancelToken) -> dict[str, int | None]:
    from anker_client.services.install import diskspace

    result: dict[str, int | None] = {}
    for folder in folders:
        token.raise_if_cancelled()
        try:
            result[folder] = diskspace.free_bytes(folder)
        except Exception as exc:  # one unreachable drive must not hide the others
            log.debug("Free space for %s unavailable: %s", folder, exc)
            result[folder] = None
    return result


# --- widgets -------------------------------------------------------------------------------------------


class OptionCard(QFrame):
    """Selectable card for one download option."""

    selected = pyqtSignal(object)  # DownloadOption

    def __init__(self, option: DownloadOption, size_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.option = option
        self.setProperty("role", "option")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 14, 10)
        row.setSpacing(10)
        self.radio = QRadioButton()
        self.radio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.radio.toggled.connect(self._on_toggled)
        row.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignVCenter)
        # Wrapped: labels like "Update Only From V 1.0.20231215.1530 To V …" must be readable in full
        # and must not widen the dialog past its maximum width (they would be clipped).
        self.title_label = label(option_title(option), wrap=True)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setToolTip(option.label)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.title_label, 1)
        self.kind_badge = Badge(KIND_LABELS[option.kind], KIND_BADGES[option.kind])
        row.addWidget(self.kind_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.size_label = label(size_text, "muted")
        self.size_label.setMinimumWidth(72)
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.radio.setChecked(True)
        super().mouseReleaseEvent(event)

    def _on_toggled(self, checked: bool) -> None:
        self.setProperty("selected", "true" if checked else "false")
        repolish(self)
        if checked:
            self.selected.emit(self.option)


class Notice(QFrame):
    """Tinted message box (role=notice) with a level icon."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "notice")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(6)
        self.text_label = label("", wrap=True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.text_label)
        self.action_button = button("", variant="link")
        self.action_button.setVisible(False)
        column.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignLeft)
        row.addLayout(column, 1)
        self.tone = ""
        self.setVisible(False)

    def show_message(self, tone: str, text: str, action_text: str = "") -> None:
        pal = palette.current()
        colors = {"info": pal.info, "success": pal.success, "warning": pal.warning, "error": pal.danger}
        names = {"info": "info", "success": "check_circle", "warning": "warning", "error": "error"}
        self.tone = tone
        self.setProperty("tone", tone)
        repolish(self)
        self._icon.setPixmap(icons.pixmap(names.get(tone, "info"), 18, colors.get(tone, pal.info)))
        self.text_label.setText(text)
        self.action_button.setText(action_text)
        self.action_button.setVisible(bool(action_text))
        self.setVisible(True)

    def clear(self) -> None:
        self.tone = ""
        self.setVisible(False)


# --- dialog ----------------------------------------------------------------------------------------------


class InstallDialog(QDialog):
    """Choose download option (when several), library folder (when several), shortcut toggles;
    shows required vs. free space per volume and warns before overwriting an existing install."""

    def __init__(self, ctx: AppContext, details: GameDetails, option: DownloadOption | None = None,
                 parent: QWidget | None = None, *, loader: Any = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._details = details
        self._settings = ctx.settings.get()
        self._installed = self._find_installed()
        self._options = list(details.download_options)
        self._cards: list[OptionCard] = []
        self._selected: DownloadOption | None = None
        self._space: SpaceCheck | None = None
        self._space_pending = False
        self._space_override = False
        self._space_generation = 0
        self._space_handle: TaskHandle[Any] | None = None
        self._free_by_folder: dict[str, int | None] = {}

        self.setWindowTitle(f"Install {details.title}")
        self.setModal(True)
        self.setMinimumWidth(580)
        self.setMaximumWidth(760)
        self._build(loader)
        initial = self._initial_option(option)
        if initial is not None:
            self._card_for(initial).radio.setChecked(True)
        else:
            self._update_state()
        self._load_folder_free_space()

    # --- public API -----------------------------------------------------------------------------------
    def selected_option(self) -> DownloadOption:
        if self._selected is None:
            raise ValueError("No download option available")
        return self._selected

    def selected_library(self) -> str:
        if self._is_locked_target() and self._installed is not None:
            return self._installed.library_root
        data = self.library_combo.currentData()
        return str(data) if data else self._settings.default_library

    @property
    def installed(self) -> InstalledGame | None:
        return self._installed

    @property
    def space_check(self) -> SpaceCheck | None:
        return self._space

    def accept(self) -> None:
        if not self.install_button.isEnabled():
            return
        self._save_shortcut_preferences()
        super().accept()

    def done(self, result: int) -> None:
        if self._space_handle is not None:
            self._space_handle.cancel()
        super().done(result)

    # --- construction ---------------------------------------------------------------------------------
    def _find_installed(self) -> InstalledGame | None:
        try:
            return self._ctx.library.find_by_slug(self._details.slug)
        except Exception:
            log.warning("Library lookup for %s failed", self._details.slug, exc_info=True)
            return None

    def _initial_option(self, option: DownloadOption | None) -> DownloadOption | None:
        if not self._options:
            return None
        if option is not None:
            for candidate in self._options:
                if candidate.download_id == option.download_id:
                    return candidate
        return self._details.primary_option or self._options[0]

    def _card_for(self, option: DownloadOption) -> OptionCard:
        return next(card for card in self._cards if card.option.download_id == option.download_id)

    def _build(self, loader: Any) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        layout.addLayout(self._build_header(loader))
        layout.addLayout(self._build_options())
        self.notice = Notice()
        self.notice.action_button.clicked.connect(self._choose_full_game)
        layout.addWidget(self.notice)
        layout.addLayout(self._build_target())
        layout.addLayout(self._build_shortcuts())
        layout.addStretch(1)
        layout.addLayout(self._build_buttons())

    def _build_header(self, loader: Any) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)
        if loader is not None:
            from anker_client.ui.widgets.image_label import AsyncImage

            cover = AsyncImage(loader, radius=6)
            cover.setFixedSize(QSize(54, 80))
            cover.set_image(self._details.cover_url, self._details.title)
            row.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(4)
        self.title_label = label(self._details.title, "title", wrap=True)
        column.addWidget(self.title_label)
        size = format_bytes(self._details.size_bytes) if self._details.size_bytes else self._details.size_text
        facts = [part for part in (self._details.version, size) if part]  # same size format as the cards
        if self._installed is not None:
            installed = self._installed.version or "unknown version"
            facts.append(f"Installed: {installed}")
        self.facts_label = label("  ·  ".join(facts) if facts else "Choose what to download", "muted", wrap=True)
        column.addWidget(self.facts_label)
        column.addStretch(1)
        row.addLayout(column, 1)
        return row

    def _build_options(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(6)
        column.addWidget(label("Download", "heading"))
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for option in self._options:
            card = OptionCard(option, option_size_text(self._details, option))
            card.selected.connect(self._on_option_selected)
            self._group.addButton(card.radio)
            self._cards.append(card)
            column.addWidget(card)
        if not self._options:
            column.addWidget(label("This game has no downloads available right now.", "error", wrap=True))
        return column

    def _build_target(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(6)
        self.target_heading = label("Install to", "heading")
        column.addWidget(self.target_heading)
        self.library_combo = QComboBox()
        # Long paths must not widen the dialog: the combo elides them instead.
        self.library_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.library_combo.setMinimumContentsLength(28)
        self.library_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        folders = list(self._settings.library_dirs)
        if self._installed is not None and os.path.normcase(self._installed.library_root) not in {
            os.path.normcase(f) for f in folders
        }:
            folders.append(self._installed.library_root)
        for folder in folders:
            self.library_combo.addItem(icons.icon("folder"), self._folder_text(folder, None), folder)
            self.library_combo.setItemData(self.library_combo.count() - 1, folder, Qt.ItemDataRole.ToolTipRole)
        default = self._settings.default_library
        index = next((i for i, f in enumerate(folders) if os.path.normcase(f) == os.path.normcase(default)), 0)
        self.library_combo.setCurrentIndex(index)
        self.library_combo.currentIndexChanged.connect(lambda _i: self._refresh_space())
        column.addWidget(self.library_combo)
        self.target_caption = label("", "caption", wrap=True)
        self.target_caption.setVisible(False)
        column.addWidget(self.target_caption)
        space_row = QHBoxLayout()
        space_row.setSpacing(8)
        self.space_icon = QLabel()
        self.space_icon.setFixedSize(16, 16)
        space_row.addWidget(self.space_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.space_label = label("", "muted", wrap=True)
        space_row.addWidget(self.space_label, 1)
        self.override_button = button("Install anyway", variant="link", on_click=self._override_space)
        self.override_button.setVisible(False)
        space_row.addWidget(self.override_button, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(space_row)
        return column

    def _build_shortcuts(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(6)
        self.desktop_check = QCheckBox("Create a desktop shortcut")
        self.desktop_check.setChecked(self._settings.create_desktop_shortcut)
        self.start_menu_check = QCheckBox("Add to the Start menu")
        self.start_menu_check.setChecked(self._settings.create_start_menu_shortcut)
        column.addWidget(self.desktop_check)
        column.addWidget(self.start_menu_check)
        return column

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.cancel_button = button("Cancel", variant="ghost", on_click=self.reject)
        row.addWidget(self.cancel_button)
        self.install_button = button("Install", "download", variant="primary", on_click=self.accept)
        self.install_button.setDefault(True)
        row.addWidget(self.install_button)
        return row

    # --- state ------------------------------------------------------------------------------------------
    def _on_option_selected(self, option: DownloadOption) -> None:
        self._selected = option
        self._space_override = False
        self._update_state()
        self._refresh_space()

    def _is_overlay(self) -> bool:
        return self._selected is not None and self._selected.kind is not DownloadKind.FULL

    def _is_locked_target(self) -> bool:
        return self._installed is not None and self._selected is not None

    def _base_missing(self) -> bool:
        return self._is_overlay() and self._installed is None

    def _update_state(self) -> None:
        self._update_notice()
        self._update_target()
        overlay = self._is_overlay()
        self.desktop_check.setVisible(not overlay)
        self.start_menu_check.setVisible(not overlay)
        self._update_install_button()

    def _update_notice(self) -> None:
        option, installed, title = self._selected, self._installed, self._details.title
        if option is None:
            self.notice.clear()
            return
        if option.kind is DownloadKind.FULL:
            if installed is not None:
                self.notice.show_message(
                    "warning",
                    f"{title} is already installed. Reinstalling downloads the full game again and replaces "
                    f"the files in its folder:\n{installed.path}")
            else:
                self.notice.clear()
            return
        what = "update" if option.kind is DownloadKind.PATCH else "add-on"
        if installed is None:
            has_full = any(o.kind is DownloadKind.FULL for o in self._options)
            self.notice.show_message(
                "error",
                f"{title} isn't installed. Install the full game first, then add this {what}.",
                "Choose the full game" if has_full else "")
            return
        if option.kind is DownloadKind.PATCH:
            span = ""
            if option.from_version and option.to_version:
                span = f" from v{normalize_version(option.from_version)} to v{normalize_version(option.to_version)}"
            mismatch = (option.from_version and installed.version
                        and normalize_version(option.from_version) != normalize_version(installed.version))
            if mismatch:
                self.notice.show_message(
                    "warning",
                    f"This update is for v{normalize_version(option.from_version)}, but "
                    f"v{normalize_version(installed.version)} is installed. It may not apply correctly — "
                    "the full game is the safer choice.",
                    "Choose the full game" if any(o.kind is DownloadKind.FULL for o in self._options) else "")
            else:
                self.notice.show_message("info", f"Updates your installed copy{span}. Only the changed files "
                                                 "are downloaded.")
            return
        self.notice.show_message("info", "Adds extra content (language pack, launcher, DLC) to your installed copy.")

    def _update_target(self) -> None:
        locked = self._is_locked_target()
        if locked and self._installed is not None:
            index = self.library_combo.findData(self._installed.library_root)
            if index < 0:
                index = next((i for i in range(self.library_combo.count())
                              if os.path.normcase(str(self.library_combo.itemData(i)))
                              == os.path.normcase(self._installed.library_root)), -1)
            if index >= 0 and index != self.library_combo.currentIndex():
                self.library_combo.blockSignals(True)
                self.library_combo.setCurrentIndex(index)
                self.library_combo.blockSignals(False)
            self.target_caption.setText(breakable(f"Goes into the existing folder: {self._installed.path}"))
            self.target_caption.setVisible(self._is_overlay())  # reinstalls already name it in the notice
        else:
            self.target_caption.setVisible(False)
        self.library_combo.setEnabled(not locked)
        self.target_heading.setText("Applies to" if self._is_overlay() else "Install to")

    def _update_install_button(self) -> None:
        option = self._selected
        if option is None:
            text = "Install"
        elif option.kind is DownloadKind.PATCH:
            text = "Install update"
        elif option.kind is DownloadKind.ADDON:
            text = "Install add-on"
        else:
            text = "Reinstall" if self._installed is not None else "Install"
        self.install_button.setText(text)
        enabled = option is not None and not self._base_missing() and self.library_combo.count() > 0
        if enabled and self._space is not None and self._space.known and not self._space.sufficient:
            enabled = self._space_override
        self.install_button.setEnabled(enabled)

    def _choose_full_game(self) -> None:
        full = next((o for o in self._options if o.kind is DownloadKind.FULL), None)
        if full is not None:
            self._card_for(full).radio.setChecked(True)

    # --- disk space ----------------------------------------------------------------------------------------
    def _load_folder_free_space(self) -> None:
        folders = [str(self.library_combo.itemData(i)) for i in range(self.library_combo.count())]
        if not folders:
            return
        run_async(self, self._ctx.runner, compute_free_by_folder, folders,
                  on_result=self._apply_folder_free_space,
                  on_error=lambda exc: log.debug("Free space lookup failed: %s", exc))

    def _apply_folder_free_space(self, free: dict[str, int | None]) -> None:
        self._free_by_folder = dict(free)
        for i in range(self.library_combo.count()):
            folder = str(self.library_combo.itemData(i))
            self.library_combo.setItemText(i, self._folder_text(folder, free.get(folder)))

    def _folder_text(self, folder: str, free: int | None) -> str:
        """``folder · 123 GB free``; long paths are shortened in the middle so the free space stays visible."""
        metrics = QFontMetrics(self.library_combo.font())
        path = metrics.elidedText(folder, Qt.TextElideMode.ElideMiddle, _FOLDER_TEXT_WIDTH)
        return path if free is None else f"{path}   ·   {format_bytes(free)} free"

    def _refresh_space(self) -> None:
        if self._space_handle is not None:
            self._space_handle.cancel()
            self._space_handle = None
        self._space_generation += 1
        generation = self._space_generation
        self._space = None
        option = self._selected
        if option is None:
            self._show_space_text("", "")
            self._update_install_button()
            return
        size = option_size(self._details, option)
        if size is None:
            self._space = SpaceCheck()
            self._show_space_text("info", "Download size unknown — make sure the drive has enough free space.")
            self._update_install_button()
            return
        library_root = self.selected_library()
        download_dir = download_dir_for(self._settings.download_dir, library_root)
        self._space_pending = True
        self._show_space_text("pending", "Checking free space…")
        self._update_install_button()
        self._space_handle = run_async(
            self, self._ctx.runner, compute_space, size, download_dir, library_root,
            on_result=lambda check, g=generation: self._apply_space(g, check),
            on_error=lambda exc, g=generation: self._apply_space(g, SpaceCheck(error=error_text(exc))),
        )

    def _apply_space(self, generation: int, check: SpaceCheck) -> None:
        if generation != self._space_generation:
            return  # superseded by a newer option/library choice
        self._space_handle = None
        self._space_pending = False
        self._space = check
        if check.error:
            log.info("Disk space check unavailable: %s", check.error)
            self._show_space_text("info", "Couldn't check free space — make sure the drive has enough room.")
        elif not check.required:
            self._show_space_text("info", "Download size unknown — make sure the drive has enough free space.")
        elif check.sufficient:
            parts = []
            for volume, needed in check.required.items():
                free = check.free.get(volume)
                free_text = f" · {format_bytes(free)} free" if free is not None else ""
                parts.append(f"{volume} needs about {format_bytes(needed)}{free_text}")
            self._show_space_text("ok", "\n".join(parts))
        else:
            parts = [f"Not enough space on {volume}: about {format_bytes(needed)} needed, {format_bytes(free)} free."
                     for volume, needed, free in check.shortfalls()]
            if self._space_override:
                parts.append("Installing anyway — the download stops if the drive fills up.")
                self._show_space_text("warning", "\n".join(parts))
            else:
                self._show_space_text("error", "\n".join(parts))
        self.override_button.setVisible(check.known and not check.sufficient and not self._space_override)
        self._update_install_button()

    def _show_space_text(self, tone: str, text: str) -> None:
        pal = palette.current()
        roles = {"ok": "muted", "info": "caption", "pending": "caption", "warning": "warning", "error": "error"}
        icon_colors = {"ok": pal.text_muted, "info": pal.text_faint, "pending": pal.text_faint,
                       "warning": pal.warning, "error": pal.danger}
        self.space_label.setProperty("role", roles.get(tone, "muted"))
        repolish(self.space_label)
        self.space_label.setText(text)
        self.space_label.setVisible(bool(text))
        self.space_icon.setVisible(bool(text))
        if text:
            self.space_icon.setPixmap(icons.pixmap("hdd", 16, icon_colors.get(tone, pal.text_muted)))
        if tone != "error":
            self.override_button.setVisible(False)

    def _override_space(self) -> None:
        self._space_override = True
        if self._space is not None:
            self._apply_space(self._space_generation, self._space)

    @property
    def space_text(self) -> str:
        return self.space_label.text()

    # --- settings -------------------------------------------------------------------------------------------
    def _save_shortcut_preferences(self) -> None:
        if self._is_overlay():
            return
        changes = {}
        if self.desktop_check.isChecked() != self._settings.create_desktop_shortcut:
            changes["create_desktop_shortcut"] = self.desktop_check.isChecked()
        if self.start_menu_check.isChecked() != self._settings.create_start_menu_shortcut:
            changes["create_start_menu_shortcut"] = self.start_menu_check.isChecked()
        if changes:
            try:
                self._ctx.settings.update(**changes)
            except Exception:
                log.exception("Could not save shortcut preferences")
