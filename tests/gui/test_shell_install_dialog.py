"""InstallDialog tests (disk-space functions are patched; the real module is built in parallel)."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QWidget

from anker_client.core.models import DownloadKind, DownloadOption, GameDetails
from anker_client.core.tasks import CancelToken
from anker_client.services.install import diskspace
from anker_client.ui.dialogs.install_dialog import (
    InstallDialog,
    SpaceCheck,
    compute_free_by_folder,
    download_dir_for,
    option_size,
    option_size_text,
    option_title,
)
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from tests.fakes import sample_details, screenshot

GB = 1024**3


@pytest.fixture
def theme(qapp: QApplication) -> ThemeManager:
    manager = ThemeManager(QApplication.instance())
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")
    return manager


class Disk:
    """Controllable stand-in for ``services.install.diskspace``."""

    def __init__(self) -> None:
        self.free = 500 * GB
        self.fail: Exception | None = None
        self.required_calls: list[tuple[int | None, str, str]] = []

    def free_bytes(self, path: str) -> int:
        if self.fail is not None:
            raise self.fail
        return self.free

    def required_bytes(self, archive_size: int | None, *, download_dir: str, library_root: str) -> dict[str, int]:
        self.required_calls.append((archive_size, download_dir, library_root))
        if self.fail is not None:
            raise self.fail
        if archive_size is None:
            return {}
        return {"C:\\": int(archive_size * 2.1)}


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> Disk:
    fake = Disk()
    monkeypatch.setattr(diskspace, "free_bytes", fake.free_bytes)
    monkeypatch.setattr(diskspace, "required_bytes", fake.required_bytes)
    return fake


def _dispose(widget: QWidget) -> None:
    widget.hide()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


@pytest.fixture
def make_dialog(qtbot: Any, fake_ctx: Any, theme: ThemeManager, disk: Disk):
    created: list[InstallDialog] = []

    def make(details: GameDetails, option: DownloadOption | None = None, **kwargs: Any) -> InstallDialog:
        dialog = InstallDialog(fake_ctx, details, option, **kwargs)
        created.append(dialog)
        return dialog

    yield make
    for dialog in created:
        _dispose(dialog)


def _settle(qtbot: Any, dialog: InstallDialog) -> None:
    qtbot.waitUntil(lambda: dialog.space_check is not None and dialog.space_text != "Checking free space…",
                    timeout=3000)


def _not_installed(fake_ctx: Any) -> GameDetails:
    return sample_details(fake_ctx.client.games[20])  # Celeste: FULL + ADDON + PATCH, not installed


def _installed(fake_ctx: Any) -> GameDetails:
    return sample_details(fake_ctx.client.games[0])  # Hollow Knight: installed v1.1.0, FULL + ADDON + PATCH


# --- pure helpers -----------------------------------------------------------------------------------------


def test_option_title_strips_size_suffix() -> None:
    assert option_title(DownloadOption(1, "Language Pack (1.2 GB)", DownloadKind.ADDON)) == "Language Pack"
    assert option_title(DownloadOption(1, "Launcher (Steam fix)", DownloadKind.ADDON)) == "Launcher (Steam fix)"
    assert option_title(DownloadOption(1, "Direct")) == "Direct"


def test_option_size_sources() -> None:
    details = GameDetails(slug="g", title="G", size_text="10 GB", size_bytes=10 * GB)
    assert option_size(details, DownloadOption(1, "Direct")) == 10 * GB
    assert option_size(details, DownloadOption(2, "Patch (124 MB)", DownloadKind.PATCH)) == 124 * 1024**2
    assert option_size(details, DownloadOption(3, "Pack", DownloadKind.ADDON, size_text="1 GB")) == GB
    assert option_size(details, DownloadOption(4, "Launcher", DownloadKind.ADDON)) is None
    assert option_size_text(details, DownloadOption(4, "Launcher", DownloadKind.ADDON)) == "Size unknown"
    assert option_size_text(details, DownloadOption(1, "Direct")) == "10.0 GB"


def test_download_dir_defaults_inside_library() -> None:
    assert download_dir_for("", r"D:\Games").replace("\\", "/") == "D:/Games/.ankerclient/downloads"
    assert download_dir_for(r"E:\Downloads", r"D:\Games") == r"E:\Downloads"


def test_space_check_shortfalls() -> None:
    check = SpaceCheck(required={"C:\\": 10, "D:\\": 5}, free={"C:\\": 20, "D:\\": 1})
    assert check.shortfalls() == [("D:\\", 5, 1)]
    assert not check.sufficient and check.known
    assert SpaceCheck().sufficient and not SpaceCheck().known


def test_compute_free_by_folder_isolates_failures(disk: Disk, monkeypatch: pytest.MonkeyPatch) -> None:
    def free(path: str) -> int:
        if path == "X:/broken":
            raise OSError("drive gone")
        return 7

    monkeypatch.setattr(diskspace, "free_bytes", free)
    assert compute_free_by_folder(["C:/Games", "X:/broken"], token=CancelToken()) == {"C:/Games": 7,
                                                                                     "X:/broken": None}


# --- dialog -----------------------------------------------------------------------------------------------


def test_lists_options_and_preselects_primary(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    dialog = make_dialog(details)
    assert [c.option.kind for c in dialog._cards] == [DownloadKind.FULL, DownloadKind.ADDON, DownloadKind.PATCH]
    assert [c.kind_badge.text() for c in dialog._cards] == ["Full game", "Add-on", "Update"]
    assert dialog._cards[1].title_label.text() == "Language Pack"
    assert dialog.selected_option() == details.primary_option
    assert dialog.selected_library() == fake_ctx.settings.get().default_library
    assert dialog.install_button.text() == "Install"
    assert dialog.notice.isHidden()
    assert not dialog.desktop_check.isHidden()
    _settle(qtbot, dialog)
    assert dialog.install_button.isEnabled()
    assert "needs about" in dialog.space_text


def test_explicit_option_is_preselected(make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    addon = next(o for o in details.download_options if o.kind is DownloadKind.ADDON)
    dialog = make_dialog(details, addon)
    assert dialog.selected_option() == addon


def test_clicking_a_card_selects_it(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    from PyQt6.QtCore import Qt

    dialog = make_dialog(_installed(fake_ctx))
    card = dialog._cards[1]
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert dialog.selected_option() == card.option
    assert card.property("selected") == "true"
    assert dialog._cards[0].property("selected") == "false"


def test_free_space_shown_per_library_folder(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk) -> None:
    disk.free = 123 * GB
    dialog = make_dialog(_not_installed(fake_ctx))
    qtbot.waitUntil(lambda: "123 GB free" in dialog.library_combo.itemText(0), timeout=3000)
    assert dialog.library_combo.itemData(0) == fake_ctx.settings.get().library_dirs[0]


def test_long_library_paths_keep_free_space_visible(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk,
                                                    tmp_path: Any) -> None:
    from PyQt6.QtCore import Qt

    deep = str(tmp_path / ("very-long-folder-name-" * 6) / "Games")
    fake_ctx.settings.update(library_dirs=[deep], default_library=deep)
    disk.free = 77 * GB
    dialog = make_dialog(_not_installed(fake_ctx))
    qtbot.waitUntil(lambda: "77.0 GB free" in dialog.library_combo.itemText(0), timeout=3000)
    text = dialog.library_combo.itemText(0)
    assert "…" in text and text.endswith("77.0 GB free")  # shortened in the middle, suffix intact
    assert dialog.library_combo.itemData(0) == deep
    assert dialog.library_combo.itemData(0, Qt.ItemDataRole.ToolTipRole) == deep
    assert dialog.selected_library() == deep


def test_long_option_labels_wrap_instead_of_widening(make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    details.download_options = [DownloadOption(1, "Direct V 1.0.20240102.1200 (Build 15938221, includes all DLC "
                                                   "and the original soundtrack in lossless quality)")]
    dialog = make_dialog(details)
    assert dialog._cards[0].title_label.wordWrap()
    assert dialog.minimumSizeHint().width() <= dialog.maximumWidth()


def test_insufficient_space_blocks_until_override(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk) -> None:
    disk.free = 1 * GB
    dialog = make_dialog(_not_installed(fake_ctx))
    _settle(qtbot, dialog)
    assert not dialog.install_button.isEnabled()
    assert dialog.space_text.startswith("Not enough space on C:\\")
    assert not dialog.override_button.isHidden()
    dialog.accept()  # disabled: must not accept
    assert dialog.result() == 0 and dialog.isHidden()

    dialog.override_button.click()
    assert dialog.install_button.isEnabled()
    assert "Installing anyway" in dialog.space_text
    assert dialog.override_button.isHidden()


def test_disk_check_failure_never_blocks(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk) -> None:
    disk.fail = NotImplementedError()
    dialog = make_dialog(_not_installed(fake_ctx))
    _settle(qtbot, dialog)
    assert dialog.install_button.isEnabled()
    assert "Couldn't check free space" in dialog.space_text


def test_unknown_size_never_blocks(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    details.download_options = [DownloadOption(9, "Launcher", DownloadKind.ADDON)]
    details.size_bytes = None
    dialog = make_dialog(details)
    assert "size unknown" in dialog.space_text


def test_download_dir_and_library_passed_to_space_check(qtbot: Any, make_dialog: Any, fake_ctx: Any,
                                                        disk: Disk) -> None:
    fake_ctx.settings.update(download_dir="E:/Downloads")
    details = _not_installed(fake_ctx)
    dialog = make_dialog(details)
    _settle(qtbot, dialog)
    size, download_dir, library_root = disk.required_calls[-1]
    assert size == details.size_bytes
    assert download_dir == "E:/Downloads"
    assert library_root == fake_ctx.settings.get().default_library


def test_switching_library_rechecks_space(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk,
                                          tmp_path: Any) -> None:
    second = str(tmp_path / "Second")
    fake_ctx.settings.update(library_dirs=[*fake_ctx.settings.get().library_dirs, second])
    dialog = make_dialog(_not_installed(fake_ctx))
    _settle(qtbot, dialog)
    dialog.library_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: disk.required_calls[-1][2] == second, timeout=3000)
    assert dialog.selected_library() == second


def test_stale_space_results_are_ignored(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    dialog = make_dialog(_not_installed(fake_ctx))
    _settle(qtbot, dialog)
    current = dialog.space_check
    dialog._apply_space(dialog._space_generation - 1, SpaceCheck(required={"C:\\": 10**15}, free={"C:\\": 1}))
    assert dialog.space_check is current
    assert dialog.install_button.isEnabled()


def test_patch_without_base_game_is_blocked(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    patch = next(o for o in details.download_options if o.kind is DownloadKind.PATCH)
    dialog = make_dialog(details, patch)
    assert dialog.notice.tone == "error"
    assert "isn't installed" in dialog.notice.text_label.text()
    assert not dialog.install_button.isEnabled()
    assert dialog.install_button.text() == "Install update"
    assert dialog.desktop_check.isHidden()
    dialog.notice.action_button.click()  # "Choose the full game"
    assert dialog.selected_option().kind is DownloadKind.FULL
    assert dialog.install_button.isEnabled()


def test_patch_for_installed_game_targets_its_library(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    details = _installed(fake_ctx)
    fake_ctx.library.set_update_state("hollow-knight", latest_version="v1.1.0", available=False)
    fake_ctx.library._games["hollow-knight"].version = "v1.0.0"
    patch = next(o for o in details.download_options if o.kind is DownloadKind.PATCH)
    dialog = make_dialog(details, patch)
    assert dialog.notice.tone == "info"
    assert "from v1.0.0 to v1.1.0" in dialog.notice.text_label.text()
    assert not dialog.library_combo.isEnabled()
    assert dialog.selected_library() == fake_ctx.library.get("hollow-knight").library_root
    assert not dialog.target_caption.isHidden()
    assert dialog.install_button.isEnabled()


def test_patch_version_mismatch_warns(make_dialog: Any, fake_ctx: Any) -> None:
    details = _installed(fake_ctx)  # installed v1.1.0, patch is 1.0.0 → 1.1.0
    patch = next(o for o in details.download_options if o.kind is DownloadKind.PATCH)
    dialog = make_dialog(details, patch)
    assert dialog.notice.tone == "warning"
    assert "is for v1.0.0" in dialog.notice.text_label.text()
    assert dialog.install_button.isEnabled()


def test_addon_for_installed_game(make_dialog: Any, fake_ctx: Any) -> None:
    details = _installed(fake_ctx)
    addon = next(o for o in details.download_options if o.kind is DownloadKind.ADDON)
    dialog = make_dialog(details, addon)
    assert dialog.notice.tone == "info"
    assert dialog.install_button.text() == "Install add-on"
    assert dialog.target_heading.text() == "Applies to"


def test_reinstall_warns_and_locks_folder(make_dialog: Any, fake_ctx: Any) -> None:
    dialog = make_dialog(_installed(fake_ctx))
    assert dialog.notice.tone == "warning"
    assert "already installed" in dialog.notice.text_label.text()
    assert dialog.install_button.text() == "Reinstall"
    assert not dialog.library_combo.isEnabled()
    assert dialog.target_caption.isHidden()


def test_shortcut_preferences_saved_on_accept_only(qtbot: Any, make_dialog: Any, fake_ctx: Any) -> None:
    dialog = make_dialog(_not_installed(fake_ctx))
    dialog.desktop_check.setChecked(False)
    dialog.reject()
    assert fake_ctx.settings.get().create_desktop_shortcut is True

    dialog = make_dialog(_not_installed(fake_ctx))
    _settle(qtbot, dialog)
    dialog.desktop_check.setChecked(False)
    dialog.accept()
    assert dialog.result() == 1
    assert fake_ctx.settings.get().create_desktop_shortcut is False
    assert fake_ctx.settings.get().create_start_menu_shortcut is True


def test_no_options_disables_install(make_dialog: Any, fake_ctx: Any) -> None:
    details = _not_installed(fake_ctx)
    details.download_options = []
    dialog = make_dialog(details)
    assert not dialog.install_button.isEnabled()
    with pytest.raises(ValueError):
        dialog.selected_option()


def test_library_lookup_failure_is_tolerated(make_dialog: Any, fake_ctx: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_slug: str) -> None:
        raise RuntimeError("library not ready")

    monkeypatch.setattr(fake_ctx.library, "find_by_slug", broken)
    dialog = make_dialog(_installed(fake_ctx))
    assert dialog.installed is None
    assert dialog.install_button.text() == "Install"


# --- screenshots ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["midnight", "daylight"])
def test_screenshot_install_dialog(qtbot: Any, make_dialog: Any, fake_ctx: Any, disk: Disk, theme: ThemeManager,
                                   theme_key: str) -> None:
    theme.apply(theme_key)
    try:
        loader = ImageLoader(fake_ctx.images, fake_ctx.runner)
        disk.free = 20 * GB  # Celeste needs ~55 GB: shows the "Not enough space" state + "Install anyway"
        dialog = make_dialog(_not_installed(fake_ctx), loader=loader)
        _settle(qtbot, dialog)
        assert not dialog.install_button.isEnabled()
        assert screenshot(dialog, f"shell_install_dialog_{theme_key}", (600, dialog.sizeHint().height())).exists()
    finally:
        theme.apply("midnight")
