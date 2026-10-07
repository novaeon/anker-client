"""First-run wizard: gated steps, async folder validation, live theme preview, final settings write."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QDialog, QWizard

# QtWebEngine must load before the QApplication exists (wizard → login → browser).
import anker_client.ui.dialogs.browser  # noqa: F401
from anker_client.services.install import sevenzip
from anker_client.ui.dialogs import first_run
from anker_client.ui.dialogs.first_run import FirstRunWizard
from anker_client.ui.dialogs.login import LoginDialog
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from tests.fakes import screenshot

pytestmark = pytest.mark.gui


def destroy(widget: Any) -> None:
    if widget is not None and not sip.isdeleted(widget):
        widget.close()
        sip.delete(widget)


@pytest.fixture
def seven_zip(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"path": r"C:\Program Files\7-Zip\7z.exe"}
    monkeypatch.setattr(sevenzip.SevenZip, "locate", staticmethod(lambda configured=None: configured or state["path"]))
    monkeypatch.setattr(sevenzip.SevenZip, "__init__", lambda self, exe: None)
    monkeypatch.setattr(sevenzip.SevenZip, "version", lambda self: "7-Zip 24.09 (x64)")
    return state


@pytest.fixture
def theme(qapp) -> Iterator[ThemeManager]:
    manager = ThemeManager(qapp)
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")  # re-polishing every live widget is slow; skip when already active
    yield manager
    if palette.current().key != "midnight":
        manager.apply("midnight")


@pytest.fixture
def wizard(qtbot, fake_ctx, theme, seven_zip, tmp_path: Path) -> Iterator[FirstRunWizard]:
    fake_ctx.settings.update(first_run_completed=False)
    wiz = FirstRunWizard(fake_ctx, theme)
    wiz.show()
    yield wiz
    destroy(wiz)


def next_enabled(wiz: QWizard) -> bool:
    return wiz.button(QWizard.WizardButton.NextButton).isEnabled()


def go_to_library(wiz: FirstRunWizard, qtbot, path: Path) -> None:
    wiz.welcome_page.ack.setChecked(True)
    wiz.next()
    assert wiz.currentPage() is wiz.library_page
    wiz.library_page.path_edit.setText(str(path))
    qtbot.waitUntil(lambda: next_enabled(wiz), timeout=3000)


def test_disclaimer_must_be_acknowledged(wizard: FirstRunWizard) -> None:
    assert wizard.currentPage() is wizard.welcome_page
    assert not next_enabled(wizard)
    assert "not affiliated" in first_run.DISCLAIMER
    wizard.welcome_page.ack.setChecked(True)
    assert next_enabled(wizard)


def test_library_folder_is_validated_off_thread(wizard: FirstRunWizard, qtbot, tmp_path: Path) -> None:
    go_to_library(wizard, qtbot, tmp_path / "Games")
    assert wizard.library_page.status.kind in ("success", "warning")  # warning = low free space
    assert "free on" in wizard.library_page.status.text()
    wizard.library_page.path_edit.setText("relative\\folder")
    assert not next_enabled(wizard)  # pending re-check blocks Continue at once
    qtbot.waitUntil(lambda: wizard.library_page.status.kind == "error", timeout=3000)
    assert not next_enabled(wizard)
    assert "full path" in wizard.library_page.status.text()


def test_complete_setup_writes_settings(wizard: FirstRunWizard, fake_ctx, theme, qtbot, tmp_path: Path) -> None:
    library = tmp_path / "MyGames"
    go_to_library(wizard, qtbot, library)
    wizard.next()
    assert wizard.currentPage() is wizard.seven_zip_page
    qtbot.waitUntil(lambda: wizard.seven_zip_page.panel.status.kind == "success", timeout=3000)
    wizard.next()
    prefs = wizard.preferences_page
    prefs.picker.cards()["daylight"].click()
    assert theme.current_key == "daylight"  # live preview
    assert fake_ctx.settings.get().theme == "midnight"  # but nothing persisted yet
    prefs.desktop_toggle.click()
    prefs.close_combo.setCurrentIndex(prefs.close_combo.findData("tray"))
    wizard.next()
    assert wizard.currentPage() is wizard.account_page
    assert "Not signed in" in wizard.account_page.status.text()
    wizard.next()
    assert wizard.currentPage() is wizard.finish_page
    assert wizard.finish_page.lines["library"].text() == str(library)
    assert wizard.finish_page.lines["sevenzip"].text() == "7-Zip 24.09 (x64)"
    assert wizard.finish_page.lines["theme"].text() == "Daylight"
    wizard.button(QWizard.WizardButton.FinishButton).click()

    assert wizard.result() == QDialog.DialogCode.Accepted
    s = fake_ctx.settings.get()
    assert s.first_run_completed is True
    assert s.library_dirs[0] == str(library) and s.default_library == str(library)
    assert s.theme == "daylight" and theme.current_key == "daylight"
    assert s.create_desktop_shortcut is False and s.close_behavior == "tray"
    assert s.seven_zip_path == ""  # auto-detected, nothing custom to remember
    qtbot.waitUntil(library.is_dir, timeout=3000)  # created in the background


def test_existing_library_folders_are_kept(wizard: FirstRunWizard, fake_ctx, qtbot, tmp_path: Path) -> None:
    old = fake_ctx.settings.get().library_dirs[0]
    assert Path(old).is_dir()
    go_to_library(wizard, qtbot, tmp_path / "New")
    assert wizard.settings_changes()["library_dirs"] == [str(tmp_path / "New"), old]


def test_library_folder_may_not_nest_with_a_kept_folder(wizard: FirstRunWizard, fake_ctx, qtbot) -> None:
    old = Path(fake_ctx.settings.get().library_dirs[0])
    go_to_library(wizard, qtbot, old)  # the existing folder itself is fine
    page = wizard.library_page
    for nested in (old / "Sub", old.parent):
        page.path_edit.setText(str(nested))
        qtbot.waitUntil(lambda: page.status.kind == "error", timeout=3000)
        assert "overlaps" in page.status.text() and str(old) in page.status.text()
        assert not next_enabled(wizard)
        page.path_edit.setText(str(old))
        qtbot.waitUntil(lambda: next_enabled(wizard), timeout=3000)


def test_running_setup_again_keeps_folders_on_missing_drives(qtbot, fake_ctx, theme, seven_zip, tmp_path) -> None:
    old = fake_ctx.settings.get().library_dirs[0]
    unplugged = str(tmp_path / "unplugged-drive" / "Games")
    fake_ctx.settings.update(first_run_completed=True, library_dirs=[old, unplugged])
    wiz = FirstRunWizard(fake_ctx, theme)
    wiz.show()
    try:
        go_to_library(wiz, qtbot, tmp_path / "New")
        assert wiz.settings_changes()["library_dirs"] == [str(tmp_path / "New"), old, unplugged]
    finally:
        destroy(wiz)


def test_first_run_drops_the_missing_builtin_default(qtbot, fake_ctx, theme, seven_zip, tmp_path) -> None:
    old = fake_ctx.settings.get().library_dirs[0]
    missing = str(tmp_path / "never-created")
    fake_ctx.settings.update(first_run_completed=False, library_dirs=[old, missing])
    wiz = FirstRunWizard(fake_ctx, theme)
    wiz.show()
    try:
        go_to_library(wiz, qtbot, tmp_path / "New")
        assert wiz.settings_changes()["library_dirs"] == [str(tmp_path / "New"), old]
    finally:
        destroy(wiz)


def test_cancel_restores_theme_and_saves_nothing(wizard: FirstRunWizard, fake_ctx, theme, qtbot, tmp_path) -> None:
    go_to_library(wizard, qtbot, tmp_path / "X")
    wizard.next()
    wizard.next()
    wizard.preferences_page.picker.cards()["terminal"].click()
    assert theme.current_key == "terminal"
    wizard.reject()
    assert theme.current_key == "midnight"
    s = fake_ctx.settings.get()
    assert s.first_run_completed is False and s.theme == "midnight"


def test_missing_seven_zip_does_not_block(wizard: FirstRunWizard, seven_zip, qtbot, tmp_path) -> None:
    seven_zip["path"] = None
    go_to_library(wizard, qtbot, tmp_path / "Y")
    wizard.next()
    panel = wizard.seven_zip_page.panel
    qtbot.waitUntil(lambda: panel.status.kind == "warning", timeout=3000)
    assert not panel.get_button.isHidden()
    assert next_enabled(wizard)


def test_account_step_signs_in_with_the_login_dialog(wizard, fake_ctx, qtbot, tmp_path) -> None:
    go_to_library(wizard, qtbot, tmp_path / "Z")
    wizard.next()
    wizard.next()
    wizard.next()
    page = wizard.account_page
    page.sign_in_button.click()
    dialog = page.login_dialog()
    assert isinstance(dialog, LoginDialog) and dialog.isVisible()
    dialog.email_edit.setText("player@example.com")
    dialog.password_edit.setText("password")
    dialog.submit()
    qtbot.waitUntil(lambda: page.login_dialog() is None, timeout=3000)
    assert page.status.text() == "Signed in as player"
    assert not page.sign_in_button.isEnabled()
    qtbot.waitUntil(lambda: sip.isdeleted(dialog), timeout=3000)  # not kept alive by the wizard


def test_account_step_names_users_without_a_display_name(wizard, fake_ctx, qtbot, tmp_path) -> None:
    from anker_client.core.models import UserInfo

    fake_ctx.auth._user = UserInfo(display_name="", email="nova@example.com")
    wizard.account_page.refresh()
    assert wizard.account_page.status.text() == "Signed in as nova@example.com"


@pytest.mark.parametrize("theme_key", ["midnight"])  # both themes: see the manual QA script
def test_screenshots(qtbot, fake_ctx, theme, seven_zip, tmp_path: Path, theme_key: str) -> None:
    theme.apply(theme_key)
    fake_ctx.settings.update(first_run_completed=False)
    wiz = FirstRunWizard(fake_ctx, theme)
    wiz.show()
    try:
        wiz.welcome_page.ack.setChecked(True)
        wiz.next()
        wiz.library_page.path_edit.setText(str(tmp_path / "Games"))
        qtbot.waitUntil(lambda: next_enabled(wiz), timeout=3000)
        wiz.next()
        wiz.next()
        assert screenshot(wiz, f"settings_ui_wizard_preferences_{theme_key}", (960, 700)).exists()
    finally:
        destroy(wiz)
