"""Library dialogs against FakeContext: they load asynchronously and save through the services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from anker_client.core.errors import InstallError
from anker_client.core.models import DownloadKind, GameSummary
from anker_client.ui.dialogs.game_dialogs import (
    ExecutablePickerDialog,
    GamePropertiesDialog,
    ImportArchiveDialog,
    ImportFoldersDialog,
)
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.library_common import confirm

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module", autouse=True)
def _theme(qapp):
    ThemeManager(qapp).apply("midnight")


@pytest.fixture
def show(qtbot, fake_ctx):
    dialogs: list[QDialog] = []

    def _show(dialog: QDialog) -> QDialog:
        qtbot.addWidget(dialog)
        dialog.show()
        dialogs.append(dialog)
        return dialog

    yield _show
    for dialog in dialogs:
        dialog.close()
    fake_ctx.runner.shutdown(wait=True)  # no worker may still deliver into a dialog being destroyed
    QApplication.processEvents()


def game_path(ctx, install_id: str) -> Path:
    return Path(ctx.library.get(install_id).path)


# --- executable picker ------------------------------------------------------------------------------


def test_executable_picker_lists_candidates_and_saves(qtbot, fake_ctx, show):
    exe = game_path(fake_ctx, "hollow-knight") / "Launcher.exe"
    exe.write_bytes(b"MZ" + b"\0" * 2046)
    dialog = show(ExecutablePickerDialog(fake_ctx, "hollow-knight"))
    qtbot.waitUntil(lambda: dialog.list.count() == 3, timeout=3000)
    assert dialog._paths == ["Hollow Knight.exe", "Launcher.exe", r"bin\x64\Game-Win64-Shipping.exe"]
    assert dialog.selected_path() == "Hollow Knight.exe"  # the current executable is preselected
    launcher_row = dialog.list.itemWidget(dialog.list.item(1))
    assert launcher_row.size.text() == "2.00 KB"
    assert launcher_row.folder.text() == "Game folder"
    dialog.list.setCurrentRow(1)
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.save_button.click()
    assert fake_ctx.library.get("hollow-knight").executable == "Launcher.exe"


def test_executable_picker_recommends_first_when_unset(qtbot, fake_ctx, show):
    dialog = show(ExecutablePickerDialog(fake_ctx, "hades-ii"))
    qtbot.waitUntil(lambda: dialog.list.count() == 3, timeout=3000)
    assert dialog.selected_path() == "Hades II.exe"
    first = dialog.list.itemWidget(dialog.list.item(0))
    assert any(child.text() == "Recommended" for child in first.findChildren(type(first.name)))


def test_executable_picker_browse_restricted_to_install_dir(qtbot, fake_ctx, show, tmp_path):
    root = game_path(fake_ctx, "hades-ii")
    (root / "tools").mkdir()
    inside = root / "tools" / "Start.exe"
    inside.write_bytes(b"MZ")
    outside = tmp_path / "elsewhere.exe"
    outside.write_bytes(b"MZ")
    dialog = show(ExecutablePickerDialog(fake_ctx, "hades-ii"))
    qtbot.waitUntil(dialog.browse_button.isEnabled, timeout=3000)
    dialog._ask_file = lambda start: str(outside)
    dialog.browse_button.click()
    assert dialog._error.isVisibleTo(dialog) and "inside the game folder" in dialog._error.text()
    assert dialog.list.count() == 3
    dialog._ask_file = lambda start: str(inside)
    dialog.browse_button.click()
    assert not dialog._error.isVisibleTo(dialog)
    assert dialog.selected_path() == str(Path("tools") / "Start.exe")
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.save_button.click()
    assert fake_ctx.library.get("hades-ii").executable == str(Path("tools") / "Start.exe")


def test_executable_picker_unknown_game(qtbot, fake_ctx, show):
    dialog = show(ExecutablePickerDialog(fake_ctx, "nope"))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 2, timeout=3000)
    assert not dialog.save_button.isEnabled()


# --- properties ---------------------------------------------------------------------------------------


def test_properties_loads_facts_and_saves_changes(qtbot, fake_ctx, show):
    dialog = show(GamePropertiesDialog(fake_ctx, "minecraft"))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 1, timeout=3000)
    assert dialog.title_edit.text() == "Minecraft"
    assert dialog.exe_combo.currentData() == "Minecraft.exe"
    assert dialog.fact_version.text() == "v1.1.0"
    assert dialog.fact_installed.text() == "01 Sep 2026"
    assert dialog.fact_playtime.text().startswith("300.0 hours")
    assert dialog.fact_store.text() == "ankergames.net/game/minecraft"
    assert dialog.fact_size.text() == "87.8 GB"
    assert not dialog.save_button.isEnabled()  # nothing changed yet

    dialog.title_edit.setText("Minecraft (modded)")
    dialog.args_edit.setText("-windowed")
    dialog.admin_check.setChecked(True)
    dialog.exe_combo.setCurrentIndex(dialog.exe_combo.findData("Launcher.exe"))
    assert dialog.save_button.isEnabled()
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.save_button.click()
    game = fake_ctx.library.get("minecraft")
    assert (game.title, game.launch_args, game.run_as_admin, game.executable) == (
        "Minecraft (modded)", "-windowed", True, "Launcher.exe")


def test_properties_blank_title_cannot_be_saved(qtbot, fake_ctx, show):
    dialog = show(GamePropertiesDialog(fake_ctx, "minecraft"))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 1, timeout=3000)
    dialog.title_edit.setText("   ")
    assert not dialog.save_button.isEnabled()


def test_properties_shortcut_buttons(qtbot, fake_ctx, show):
    created: list[tuple[Any, ...]] = []

    class Shortcuts:
        def exists(self, title: str) -> dict[str, bool]:
            return {"desktop": True, "start_menu": False}

        def create(self, title, target_exe, *, arguments="", desktop=True, start_menu=True):
            created.append((title, target_exe, desktop, start_menu))
            return ["x.lnk"]

    fake_ctx.shortcuts = Shortcuts()
    dialog = show(GamePropertiesDialog(fake_ctx, "hollow-knight"))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 1, timeout=3000)
    assert dialog.shortcut_status.text() == "Shortcut exists on the desktop."
    dialog.start_menu_button.click()
    qtbot.waitUntil(lambda: bool(created), timeout=2000)
    title, target, desktop, start_menu = created[0]
    assert title == "Hollow Knight" and target.endswith("Hollow Knight.exe") and not desktop and start_menu
    qtbot.waitUntil(lambda: "Start menu" in dialog.shortcut_status.text(), timeout=2000)


def test_properties_shortcuts_need_an_executable(qtbot, fake_ctx, show):
    dialog = show(GamePropertiesDialog(fake_ctx, "hades-ii"))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 1, timeout=3000)
    assert dialog.exe_combo.currentData() == ""
    assert not dialog.desktop_button.isEnabled()
    dialog.exe_combo.setCurrentIndex(1)
    assert dialog.desktop_button.isEnabled()


def test_properties_computes_unknown_size(qtbot, fake_ctx, show, monkeypatch):
    fake_ctx.library._games["minecraft"].size_bytes = None
    monkeypatch.setattr(fake_ctx.library, "compute_size", lambda install_id, *, token=None: 1024**3)
    dialog = show(GamePropertiesDialog(fake_ctx, "minecraft"))
    qtbot.waitUntil(lambda: dialog.fact_size.text() == "1.00 GB", timeout=3000)


# --- import archive -------------------------------------------------------------------------------------


@pytest.fixture
def archive(tmp_path) -> Path:
    path = tmp_path / "Hollow-Knight_v1.5.78.zip"
    path.write_bytes(b"PK" + b"\0" * 4094)
    return path


def test_import_archive_prefilled_from_job(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, slug="hollow-knight", title="Hollow Knight",
                                      archive_path=str(archive)))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    summary, title = dialog.picker.selection()
    assert summary is not None and summary.slug == "hollow-knight" and title == "Hollow Knight"
    assert "4.00 KB" in dialog.archive_info.text()
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.import_button.click()
    job = dialog.job
    assert job is not None and job.imported_archive
    assert (job.slug, job.title, job.option.kind, job.archive_path) == (
        "hollow-knight", "Hollow Knight", DownloadKind.FULL, str(archive))
    assert job.library_root == fake_ctx.settings.get().default_library


def test_import_archive_browse_guesses_title_and_searches(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx))
    assert not dialog.import_button.isEnabled()
    dialog._ask_archive = lambda: str(archive)
    dialog.archive_browse.click()
    assert dialog.picker.search.text() == "Hollow Knight"
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    assert dialog.picker.results.count() == 3  # two catalog matches + "use as title"


def test_import_archive_custom_title(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(archive)))
    dialog.picker.set_query("My Homebrew Game")
    qtbot.waitUntil(lambda: dialog.picker.results.count() == 1, timeout=3000)
    dialog.picker.results.setCurrentRow(0)
    assert dialog.picker.selection() == (None, "My Homebrew Game")
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.import_button.click()
    assert dialog.job.slug == "" and dialog.job.title == "My Homebrew Game"


def test_import_archive_rejects_wrong_file(qtbot, fake_ctx, show, tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("hi")
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(text), slug="hollow-knight", title="Hollow Knight"))
    assert dialog.archive_info.property("role") == "error"
    missing = ImportArchiveDialog(fake_ctx, archive_path=str(tmp_path / "missing.zip"))
    show(missing)
    qtbot.waitUntil(lambda: "doesn't exist" in missing.archive_info.text(), timeout=3000)
    assert not missing.import_button.isEnabled()


def test_import_archive_patch_requires_installed_base(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(archive), slug="celeste", title="Celeste"))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(DownloadKind.PATCH))
    qtbot.waitUntil(lambda: "isn't installed" in dialog.notice_text.text(), timeout=3000)
    assert not dialog.import_button.isEnabled()
    assert not dialog.library_combo.isEnabled()
    dialog.picker.set_query("Hollow Knight", prefer_slug="hollow-knight")
    qtbot.waitUntil(lambda: "Extracted over your installation" in dialog.notice_text.text(), timeout=3000)
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.import_button.click()
    assert dialog.job.option.kind is DownloadKind.PATCH


def test_import_archive_failure_is_shown(qtbot, fake_ctx, show, archive, monkeypatch):
    def fail(*_a, **_k):
        raise InstallError("7-Zip is busy")

    monkeypatch.setattr(fake_ctx.downloads, "import_archive", fail)
    dialog = show(ImportArchiveDialog(fake_ctx, slug="hollow-knight", title="Hollow Knight",
                                      archive_path=str(archive)))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    dialog.import_button.click()
    qtbot.waitUntil(lambda: "7-Zip is busy" in dialog._error.text(), timeout=3000)
    assert dialog.isVisible() and dialog.import_button.isEnabled()


# --- import folders --------------------------------------------------------------------------------------


def test_import_folders_lists_unmanaged_with_matches_and_adopts(qtbot, fake_ctx, show):
    dialog = show(ImportFoldersDialog(fake_ctx))
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=3000)
    assert dialog.table.item(0, dialog.COL_FOLDER).text() == "Roadhouse Simulator"
    assert dialog.table.item(0, dialog.COL_MATCH).text().startswith("Roadhouse Simulator")
    assert dialog.checked_ids() == ["local:roadhouse simulator"]
    assert dialog.import_button.text() == "Import 1 game"
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.import_button.click()
    assert dialog.imported == 1
    adopted = fake_ctx.library.get("roadhouse-simulator")
    assert adopted is not None and adopted.managed and adopted.slug == "roadhouse-simulator"


def test_import_folders_change_match(qtbot, fake_ctx, show):
    dialog = show(ImportFoldersDialog(fake_ctx))
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=3000)
    dialog._change_match(0)
    popup = dialog._match_dialog
    assert popup is not None
    qtbot.waitUntil(lambda: popup.picker.results.count() >= 1, timeout=3000)
    popup.picker.set_query("Celeste")
    qtbot.waitUntil(lambda: popup.use_button.isEnabled() and popup.picker.selection()[1] == "Celeste", timeout=3000)
    popup.use_button.click()
    assert dialog.table.item(0, dialog.COL_MATCH).text().startswith("Celeste")
    # "Don't link" keeps the folder name
    dialog._change_match(0)
    dialog._match_dialog.unlink_button.click()
    assert dialog.table.item(0, dialog.COL_MATCH).text().startswith("No match")
    dialog.select_all.click()
    assert dialog.checked_ids() == []
    assert not dialog.import_button.isEnabled()


def test_import_folders_failure_keeps_row(qtbot, fake_ctx, show, monkeypatch):
    def fail(install_id, *, slug="", title=""):
        raise InstallError("Folder is read-only")

    monkeypatch.setattr(fake_ctx.library, "adopt", fail)
    dialog = show(ImportFoldersDialog(fake_ctx))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    dialog.import_button.click()
    qtbot.waitUntil(lambda: "couldn't be imported" in dialog._error.text(), timeout=3000)
    assert dialog.isVisible() and dialog.imported == 0
    assert dialog.table.item(0, dialog.COL_MATCH).text() == "Folder is read-only"


def test_import_folders_empty(qtbot, fake_ctx, show):
    fake_ctx.library.adopt("local:roadhouse simulator", slug="roadhouse-simulator", title="Roadhouse Simulator")
    dialog = show(ImportFoldersDialog(fake_ctx))
    qtbot.waitUntil(lambda: dialog._stack.currentIndex() == 1 and not dialog._loading, timeout=3000)
    assert not dialog.import_button.isEnabled()


# --- shared confirm helper ---------------------------------------------------------------------------------


def test_confirm_returns_true_only_for_the_destructive_button(qtbot, monkeypatch):
    def click_role(role):
        def fake_exec(box: QMessageBox) -> int:
            for btn in box.buttons():
                if box.buttonRole(btn) == role:
                    btn.click()
            return 0
        return fake_exec

    monkeypatch.setattr(QMessageBox, "exec", click_role(QMessageBox.ButtonRole.DestructiveRole))
    assert confirm(None, title="t", text="Delete X?", confirm_text="Delete")
    monkeypatch.setattr(QMessageBox, "exec", click_role(QMessageBox.ButtonRole.RejectRole))
    assert not confirm(None, title="t", text="Delete X?", confirm_text="Delete")


def test_catalog_summary_caption():
    from anker_client.ui.widgets.library_catalog_picker import summary_caption

    game = GameSummary(slug="a", title="A", primary_genre="RPG", year=2020, size_text="1 GB")
    assert summary_caption(game) == "RPG · 2020 · 1 GB"


def test_import_archive_typed_path_with_quotes(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, slug="hollow-knight", title="Hollow Knight"))
    dialog.archive_edit.setText(f'"{archive}"')
    dialog.archive_edit.textEdited.emit(dialog.archive_edit.text())
    assert not dialog.import_button.isEnabled()
    dialog.archive_edit.editingFinished.emit()
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    assert dialog.archive_path() == str(archive)


# --- review regressions ------------------------------------------------------------------------------


def test_import_archive_kind_can_be_preset(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(archive), slug="hollow-knight",
                                      title="Hollow Knight", kind=DownloadKind.PATCH))
    assert dialog.selected_kind() is DownloadKind.PATCH
    assert not dialog.library_combo.isEnabled()
    qtbot.waitUntil(lambda: "Extracted over your installation" in dialog.notice_text.text(), timeout=3000)
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.import_button.click()
    assert dialog.job.option.kind is DownloadKind.PATCH and dialog.job.library_root  # the fake picks a root


def test_import_archive_full_warns_that_it_replaces_an_install(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(archive), slug="hollow-knight",
                                      title="Hollow Knight"))
    qtbot.waitUntil(lambda: "replaces that installation" in dialog.notice_text.text(), timeout=3000)
    assert dialog.notice.isVisibleTo(dialog) and dialog.notice.property("tone") == "warning"
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)  # a warning, not a block
    dialog.picker.set_query("Celeste", prefer_slug="celeste")  # not installed: nothing to warn about
    qtbot.waitUntil(lambda: dialog.picker.selection()[1] == "Celeste", timeout=3000)
    assert not dialog.notice.isVisibleTo(dialog)


def test_import_archive_single_library_folder_is_not_selectable(qtbot, fake_ctx, show):
    dialog = show(ImportArchiveDialog(fake_ctx))
    assert dialog.library_combo.count() == 1
    assert not dialog.library_combo.isEnabled()


def test_import_archive_focus_loss_keeps_import_enabled(qtbot, fake_ctx, show, archive):
    dialog = show(ImportArchiveDialog(fake_ctx, archive_path=str(archive), slug="hollow-knight",
                                      title="Hollow Knight"))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)
    dialog.archive_edit.editingFinished.emit()  # focus moved to the Import button being clicked
    assert dialog.import_button.isEnabled()


def test_executable_picker_survives_a_failing_program_search(qtbot, fake_ctx, show, monkeypatch):
    def broken(install_id):
        raise InstallError("The game folder can't be read.")

    monkeypatch.setattr(fake_ctx.library, "executable_candidates", broken)
    dialog = show(ExecutablePickerDialog(fake_ctx, "hollow-knight"))
    qtbot.waitUntil(lambda: dialog.list.count() == 1, timeout=3000)
    assert dialog.selected_path() == "Hollow Knight.exe"  # the current program is still listed
    assert dialog.browse_button.isEnabled()


def test_enter_in_the_catalog_search_searches_instead_of_importing(qtbot, fake_ctx, show, archive, monkeypatch):
    imports: list[str] = []
    monkeypatch.setattr(fake_ctx.downloads, "import_archive", lambda path, **kw: imports.append(path))
    dialog = show(ImportArchiveDialog(fake_ctx, slug="hollow-knight", title="Hollow Knight",
                                      archive_path=str(archive)))
    qtbot.waitUntil(dialog.import_button.isEnabled, timeout=3000)  # "Import" is the default button
    dialog.picker.search.setFocus()
    dialog.picker.search.setText("Celeste")
    qtbot.keyClick(dialog.picker.search, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: dialog.picker.selection()[1] == "Celeste", timeout=3000)
    assert imports == [] and dialog.isVisible()
    qtbot.keyClick(dialog.picker.search, Qt.Key.Key_Down)
    assert dialog.picker.results.hasFocus()
