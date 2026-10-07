"""Settings page: every control writes ``ctx.settings``, live updates, async sections, visuals."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QSpinBox

# QtWebEngine must be imported before the QApplication exists (the wizard → login → browser chain).
import anker_client.ui.dialogs.browser  # noqa: F401
from anker_client import __version__
from anker_client.core import events as ev
from anker_client.core.models import AppRelease, UserInfo
from anker_client.services.install import sevenzip
from anker_client.services.system import autostart
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.pages import settings as settings_page
from anker_client.ui.pages.settings import SECTIONS, SettingsPage
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets import settings_controls, settings_sevenzip
from anker_client.ui.widgets.settings_controls import ToggleSwitch
from tests.fakes import screenshot

pytestmark = pytest.mark.gui


class RecordingNav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.toasts: list[tuple[str, str]] = []

    def toast(self, message: str, level: str = "info") -> None:
        self.toasts.append((message, level))

    def request_login(self) -> None:
        self.calls.append(("request_login", (), {}))

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


class SevenZipStub:
    path: str | None = r"C:\Program Files\7-Zip\7z.exe"
    version = "7-Zip 24.09 (x64)"
    error: BaseException | None = None
    located_with: list[str | None] = []


class AutostartStub:
    enabled = False
    set_result: bool | BaseException = True
    calls: list[bool] = []


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> tuple[type[SevenZipStub], type[AutostartStub]]:
    SevenZipStub.path = r"C:\Program Files\7-Zip\7z.exe"
    SevenZipStub.version = "7-Zip 24.09 (x64)"
    SevenZipStub.error = None
    SevenZipStub.located_with = []
    AutostartStub.enabled = False
    AutostartStub.set_result = True
    AutostartStub.calls = []

    def locate(configured: str | None = None) -> str | None:
        SevenZipStub.located_with.append(configured)
        if SevenZipStub.error is not None:
            raise SevenZipStub.error
        return configured or SevenZipStub.path

    def set_enabled(enabled: bool) -> bool:
        AutostartStub.calls.append(enabled)
        if isinstance(AutostartStub.set_result, BaseException):
            raise AutostartStub.set_result
        return AutostartStub.set_result

    monkeypatch.setattr(sevenzip.SevenZip, "locate", staticmethod(locate))
    monkeypatch.setattr(sevenzip.SevenZip, "__init__", lambda self, exe: setattr(self, "_exe", exe))
    monkeypatch.setattr(sevenzip.SevenZip, "version", lambda self: SevenZipStub.version)
    monkeypatch.setattr(autostart, "is_enabled", lambda: AutostartStub.enabled)
    monkeypatch.setattr(autostart, "set_enabled", set_enabled)
    return SevenZipStub, AutostartStub


def destroy(widget: Any) -> None:
    """Delete a top-level widget now. pytest-qt only calls deleteLater(), which never runs in tests
    that do not spin an event loop, so old pages would pile up and be re-polished on every theme change."""
    if widget is not None and not sip.isdeleted(widget):
        widget.close()
        sip.delete(widget)


@pytest.fixture
def theme(qapp) -> ThemeManager:
    manager = ThemeManager(qapp)
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")  # re-polishing every live widget is slow; skip when already active
    yield manager
    if palette.current().key != "midnight":
        manager.apply("midnight")


@pytest.fixture
def bridge(fake_ctx) -> QtEventBridge:
    b = QtEventBridge(fake_ctx.events)
    yield b
    b.close()


@pytest.fixture
def nav() -> RecordingNav:
    return RecordingNav()


@pytest.fixture
def page(qtbot, fake_ctx, bridge, theme, nav, stubs) -> SettingsPage:
    p = SettingsPage(fake_ctx, bridge, nav, theme)  # type: ignore[arg-type]
    p.resize(1100, 760)
    p.show()
    p.on_activated()
    yield p
    p.shutdown()
    destroy(p)


def build(page: SettingsPage, *sections: str) -> None:
    """Sections are built lazily on first show; tests that poke at widgets build them first."""
    for section in sections or SECTIONS:
        page.ensure_section(section)


def toggle(page: SettingsPage, key: str) -> ToggleSwitch:
    widget = page.findChild(ToggleSwitch, f"setting_{key}")
    assert widget is not None, key
    return widget


def choose(combo: QComboBox, value: Any) -> None:
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------


def test_every_section_has_a_nav_entry_and_can_be_shown(page: SettingsPage, qtbot) -> None:
    assert list(page.nav_buttons) == list(SECTIONS)
    for key in SECTIONS:
        qtbot.mouseClick(page.nav_buttons[key], Qt.MouseButton.LeftButton)
        assert page.current_section() == key
        assert page.stack.currentWidget() is page.sections[key]
        assert page.nav_buttons[key].isChecked()
    page.show_section("not-a-section")
    assert page.current_section() == "about"  # unknown keys keep the current section
    page.show_section("downloads")
    assert page.current_section() == "downloads"


# ---------------------------------------------------------------------------
# simple controls
# ---------------------------------------------------------------------------

TOGGLE_KEYS = [
    "start_minimized", "notifications_enabled", "check_app_updates", "create_desktop_shortcut",
    "create_start_menu_shortcut", "minimize_on_game_launch", "show_hidden_games", "check_game_updates",
    "auto_install", "delete_archive_after_install", "verify_archive_before_install", "auto_resume_downloads",
    "remember_login",
]


def test_toggles_write_settings_immediately(page: SettingsPage, fake_ctx, qtbot) -> None:
    build(page)
    for key in TOGGLE_KEYS:
        before = getattr(fake_ctx.settings.get(), key)
        switch = toggle(page, key)
        page.show_section(next(k for k, s in page.sections.items() if s.isAncestorOf(switch)))
        assert switch.isChecked() == before, key
        qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)
        assert getattr(fake_ctx.settings.get(), key) is (not before), key
        assert switch.isChecked() is (not before), key


def test_clicking_row_text_toggles_the_switch(page: SettingsPage, fake_ctx, qtbot) -> None:
    switch = toggle(page, "start_minimized")
    row = switch.parentWidget()
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton, pos=row.rect().topLeft() + row.rect().center() * 0.2)
    assert fake_ctx.settings.get().start_minimized is True


def test_combo_spin_and_interval_controls(page: SettingsPage, fake_ctx) -> None:
    build(page, "library", "downloads")
    choose(page.findChild(QComboBox, "setting_close_behavior"), "quit")
    assert fake_ctx.settings.get().close_behavior == "quit"
    choose(page.findChild(QComboBox, "setting_game_update_interval_hours"), 24)
    assert fake_ctx.settings.get().game_update_interval_hours == 24
    choose(page.findChild(QComboBox, "setting_verification_timeout_seconds"), 300)
    assert fake_ctx.settings.get().verification_timeout_seconds == 300
    page.findChild(QSpinBox, "setting_max_concurrent_downloads").setValue(3)
    page.findChild(QSpinBox, "setting_connections_per_download").setValue(12)
    s = fake_ctx.settings.get()
    assert (s.max_concurrent_downloads, s.connections_per_download) == (3, 12)


def test_update_interval_follows_the_update_check_switch(page: SettingsPage, qtbot) -> None:
    page.show_section("library")
    combo = page.findChild(QComboBox, "setting_game_update_interval_hours")
    assert combo.isEnabled()
    qtbot.mouseClick(toggle(page, "check_game_updates"), Qt.MouseButton.LeftButton)
    assert not combo.isEnabled()


def test_external_changes_refresh_controls_live(page: SettingsPage, fake_ctx, qtbot) -> None:
    build(page, "library", "downloads")
    fake_ctx.settings.update(notifications_enabled=False, max_concurrent_downloads=4, close_behavior="tray",
                             game_update_interval_hours=5, speed_limit_kbps=25 * 1024)
    qtbot.waitUntil(lambda: not toggle(page, "notifications_enabled").isChecked(), timeout=2000)
    assert page.findChild(QSpinBox, "setting_max_concurrent_downloads").value() == 4
    assert page.findChild(QComboBox, "setting_close_behavior").currentData() == "tray"
    interval = page.findChild(QComboBox, "setting_game_update_interval_hours")
    assert interval.currentData() == 5 and interval.currentText() == "Every 5 hours"
    assert page.speed_combo.currentText() == "25 MB/s"


def test_speed_limit_presets_and_custom_value(page: SettingsPage, fake_ctx) -> None:
    page.show_section("downloads")
    choose(page.speed_combo, 5 * 1024)
    assert fake_ctx.settings.get().speed_limit_kbps == 5120
    assert page.speed_spin.isHidden()
    choose(page.speed_combo, settings_page.CUSTOM_SPEED)
    assert not page.speed_spin.isHidden()
    assert page.speed_combo.currentData() == settings_page.CUSTOM_SPEED  # stays on Custom
    page.findChild(QDoubleSpinBox, "custom_speed").setValue(7.5)
    assert fake_ctx.settings.get().speed_limit_kbps == 7680
    choose(page.speed_combo, 0)
    assert fake_ctx.settings.get().speed_limit_kbps == 0
    assert page.speed_spin.isHidden()


def test_download_folder_change_and_reset(page: SettingsPage, fake_ctx, monkeypatch, qtbot, tmp_path) -> None:
    target = tmp_path / "dl"
    monkeypatch.setattr(settings_page.QFileDialog, "getExistingDirectory", lambda *a, **k: target.as_posix())
    page.show_section("downloads")
    page.download_dir_button.click()
    qtbot.waitUntil(lambda: fake_ctx.settings.get().download_dir == str(target), timeout=3000)  # normalised
    assert page.download_dir_label.text() == str(target)
    assert not page.download_dir_reset.isHidden() and page.download_dir_button.isEnabled()
    page.download_dir_reset.click()
    assert fake_ctx.settings.get().download_dir == ""
    assert page.download_dir_label.text().startswith("Default")


def test_download_folder_inside_windows_is_refused(page, fake_ctx, nav, monkeypatch, qtbot, tmp_path) -> None:
    windows = tmp_path / "FakeWindows"
    windows.mkdir()
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    monkeypatch.setattr(settings_page.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: str(windows / "Temp"))
    page.show_section("downloads")
    page.download_dir_button.click()
    qtbot.waitUntil(lambda: bool(nav.toasts), timeout=3000)
    assert nav.toasts[-1][1] == "error" and "administrator" in nav.toasts[-1][0]
    assert fake_ctx.settings.get().download_dir == ""
    assert page.download_dir_button.isEnabled()


def test_save_failure_shows_a_toast_and_reverts(page: SettingsPage, fake_ctx, nav, monkeypatch, qtbot) -> None:
    def broken(**_changes: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fake_ctx.settings, "update", broken)
    switch = toggle(page, "start_minimized")
    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert not switch.isChecked()
    assert nav.toasts and nav.toasts[-1][1] == "error" and "disk full" in nav.toasts[-1][0]


# ---------------------------------------------------------------------------
# launch with Windows
# ---------------------------------------------------------------------------


def test_autostart_enable_uses_the_service(page: SettingsPage, fake_ctx, nav, stubs, qtbot) -> None:
    _, auto = stubs
    qtbot.mouseClick(page.autostart_toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: fake_ctx.settings.get().launch_on_startup, timeout=3000)
    assert auto.calls == [True]
    assert nav.toasts[-1][1] == "success"


@pytest.mark.parametrize("failure", [False, NotImplementedError(), PermissionError("denied")])
def test_autostart_failure_reverts_the_switch(page: SettingsPage, fake_ctx, nav, stubs, qtbot, failure) -> None:
    _, auto = stubs
    auto.set_result = failure
    qtbot.mouseClick(page.autostart_toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(nav.toasts), timeout=3000)
    assert nav.toasts[-1][1] == "error"
    assert not page.autostart_toggle.isChecked()
    assert page.autostart_toggle.isEnabled()
    assert fake_ctx.settings.get().launch_on_startup is False


def test_stale_autostart_state_does_not_undo_the_users_change(qtbot, fake_ctx, bridge, theme, nav, stubs,
                                                              monkeypatch) -> None:
    _, auto = stubs
    release = threading.Event()

    def slow_is_enabled() -> bool:
        release.wait(5)
        return False  # what Windows said before the user's change

    monkeypatch.setattr(autostart, "is_enabled", slow_is_enabled)
    p = SettingsPage(fake_ctx, bridge, nav, theme)  # type: ignore[arg-type] -- starts the state check
    try:
        p.show()
        qtbot.mouseClick(p.autostart_toggle, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: fake_ctx.settings.get().launch_on_startup, timeout=3000)
        release.set()
        qtbot.wait(300)  # the stale answer would arrive now
        assert fake_ctx.settings.get().launch_on_startup is True
        assert p.autostart_toggle.isChecked() and auto.calls == [True]
    finally:
        release.set()
        p.shutdown()
        destroy(p)


def test_autostart_state_is_reconciled_with_windows(qtbot, fake_ctx, bridge, theme, nav, stubs) -> None:
    _, auto = stubs
    auto.enabled = True
    p = SettingsPage(fake_ctx, bridge, nav, theme)  # type: ignore[arg-type]
    try:
        p.on_activated()
        qtbot.waitUntil(lambda: fake_ctx.settings.get().launch_on_startup, timeout=3000)
        assert p.autostart_toggle.isChecked()
    finally:
        p.shutdown()
        destroy(p)


# ---------------------------------------------------------------------------
# library folders
# ---------------------------------------------------------------------------


def test_add_library_folder_validates_saves_and_rescans(page, fake_ctx, nav, monkeypatch, qtbot, tmp_path):
    scans: list[bool] = []
    original_scan = fake_ctx.library.scan
    monkeypatch.setattr(fake_ctx.library, "scan", lambda **kw: scans.append(True) or original_scan(**kw))
    new_dir = tmp_path / "MoreGames"
    monkeypatch.setattr(settings_page.QFileDialog, "getExistingDirectory", lambda *a, **k: str(new_dir))
    page.show_section("library")
    page.folders_editor.add_button.click()
    qtbot.waitUntil(lambda: str(new_dir) in fake_ctx.settings.get().library_dirs, timeout=3000)
    qtbot.waitUntil(lambda: bool(scans), timeout=3000)
    assert str(new_dir) in page.folders_editor.folders()
    assert nav.toasts[-1][1] == "success"


def test_add_library_folder_rejects_bad_choices(page, fake_ctx, monkeypatch, qtbot) -> None:
    page.show_section("library")
    existing = fake_ctx.settings.get().library_dirs[0]
    for bad in (existing, os.path.splitdrive(existing)[0] + "\\"):
        monkeypatch.setattr(settings_page.QFileDialog, "getExistingDirectory", lambda *a, b=bad, **k: b)
        page.folders_editor.add_button.click()
        qtbot.waitUntil(lambda: page.folders_editor.message.kind == "error", timeout=3000)
        assert fake_ctx.settings.get().library_dirs == [existing]
        page.folders_editor.show_message("")


def test_remove_and_default_library_folder(page, fake_ctx, monkeypatch, qtbot, tmp_path) -> None:
    first = fake_ctx.settings.get().library_dirs[0]
    second = str(tmp_path / "Second")
    page.show_section("library")
    fake_ctx.settings.update(library_dirs=[first, second])
    qtbot.waitUntil(lambda: page.folders_editor.folders() == [first, second], timeout=2000)

    page.folders_editor.default_requested.emit(second)
    assert fake_ctx.settings.get().default_library == second

    monkeypatch.setattr(settings_page, "confirm", lambda *a, **k: False)
    page.folders_editor.remove_requested.emit(second)
    assert fake_ctx.settings.get().library_dirs == [first, second]

    asked: list[str] = []
    monkeypatch.setattr(settings_page, "confirm", lambda *a, **k: asked.append(k["text"]) or True)
    page.folders_editor.remove_requested.emit(second)
    s = fake_ctx.settings.get()
    assert s.library_dirs == [first] and s.default_library == first
    assert second in asked[0]
    assert not page.folders_editor.row(first).remove_button.isEnabled()  # last folder cannot be removed


def test_long_library_paths_do_not_push_the_row_buttons_out_of_view(page, fake_ctx, qtbot) -> None:
    first = fake_ctx.settings.get().library_dirs[0]
    long_dir = "D:\\Games\\" + "\\".join(["a rather long folder name"] * 6)
    fake_ctx.settings.update(library_dirs=[first, long_dir])
    page.resize(880, 640)
    page.show_section("library")
    qtbot.waitUntil(lambda: page.folders_editor.row(long_dir) is not None, timeout=2000)
    qtbot.wait(50)  # layouts settle
    section = page.sections["library"]
    viewport = section.viewport()
    for path in (first, long_dir):
        row = page.folders_editor.row(path)
        right = row.remove_button.mapTo(viewport, row.remove_button.rect().topRight()).x()
        assert right <= viewport.width(), path
        assert row.path_label.toolTip() == path and row.path_label.full_text() == path
    assert "…" in page.folders_editor.row(long_dir).path_label.text()


def test_library_folders_show_free_space(page, fake_ctx, qtbot) -> None:
    page.show_section("library")
    path = fake_ctx.settings.get().library_dirs[0]
    qtbot.waitUntil(lambda: "free" in page.folders_editor.row(path).info.text(), timeout=3000)


# ---------------------------------------------------------------------------
# 7-Zip
# ---------------------------------------------------------------------------


def test_seven_zip_is_detected_with_version(page: SettingsPage, qtbot) -> None:
    page.show_section("downloads")
    qtbot.waitUntil(lambda: page.seven_zip_panel.status.kind == "success", timeout=3000)
    assert "7-Zip 24.09" in page.seven_zip_panel.status.text()
    assert "Detected automatically" in page.seven_zip_panel.path_label.text()


@pytest.mark.parametrize(("path", "error", "expected"), [
    (None, None, "not found"),
    (None, NotImplementedError(), "not available"),
    (None, OSError("boom"), "failed"),
])
def test_seven_zip_problems_are_explained(page, stubs, qtbot, path, error, expected) -> None:
    stub, _ = stubs
    stub.path, stub.error = path, error
    page.show_section("downloads")
    page.seven_zip_panel.detect("")
    qtbot.waitUntil(lambda: page.seven_zip_panel.status.kind == "warning", timeout=3000)
    assert expected in page.seven_zip_panel.status.text()
    assert not page.seven_zip_panel.get_button.isHidden()


def test_seven_zip_browse_and_auto_detect(page, fake_ctx, stubs, monkeypatch, qtbot) -> None:
    stub, _ = stubs
    custom = r"D:\Tools\7-Zip\7z.exe"
    monkeypatch.setattr(settings_sevenzip.QFileDialog, "getOpenFileName", lambda *a, **k: (custom, ""))
    page.show_section("downloads")
    page.seven_zip_panel.browse_button.click()
    qtbot.waitUntil(lambda: fake_ctx.settings.get().seven_zip_path == custom, timeout=3000)
    qtbot.waitUntil(lambda: "Custom location" in page.seven_zip_panel.path_label.text(), timeout=3000)
    page.seven_zip_panel.detect_button.click()
    assert fake_ctx.settings.get().seven_zip_path == ""
    qtbot.waitUntil(lambda: "Detected automatically" in page.seven_zip_panel.path_label.text(), timeout=3000)
    assert stub.located_with[-1] is None


def test_seven_zip_browse_rejects_other_programs(page, fake_ctx, stubs, monkeypatch, qtbot) -> None:
    stub, _ = stubs
    stub.version = "Some other tool 1.0"
    monkeypatch.setattr(settings_sevenzip.QFileDialog, "getOpenFileName", lambda *a, **k: (r"C:\x\notepad.exe", ""))
    page.show_section("downloads")
    page.seven_zip_panel.browse_button.click()
    qtbot.waitUntil(lambda: page.seven_zip_panel.status.kind == "error", timeout=3000)
    assert fake_ctx.settings.get().seven_zip_path == ""


class FakeSevenZip:
    """Stand-in for ``SevenZip``: records which programs would have been run."""

    ran: list[str] = []
    banner = "7-Zip 24.09"

    def __init__(self, exe: str) -> None:
        FakeSevenZip.ran.append(exe)
        folder, name = os.path.split(exe)
        self.exe_path = os.path.join(folder, "7z.exe") if name.lower() in ("7zfm.exe", "7zg.exe") else exe

    def version(self) -> str:
        return FakeSevenZip.banner


@pytest.fixture
def fake_seven_zip(monkeypatch: pytest.MonkeyPatch) -> type[FakeSevenZip]:
    FakeSevenZip.ran = []
    FakeSevenZip.banner = "7-Zip 24.09"
    monkeypatch.setattr(settings_sevenzip, "SevenZip", FakeSevenZip)
    return FakeSevenZip


def test_check_seven_zip_never_runs_other_programs(fake_seven_zip) -> None:
    info = settings_sevenzip.check_seven_zip(r"D:\Games\Elden Ring\eldenring.exe")
    assert not info.found and "7z.exe" in info.error
    assert fake_seven_zip.ran == []  # a game or installer must never be started with "i"


def test_check_seven_zip_remembers_the_console_program(fake_seven_zip) -> None:
    info = settings_sevenzip.check_seven_zip(r"C:\Program Files\7-Zip\7zFM.exe")
    assert info.found and info.path == r"C:\Program Files\7-Zip\7z.exe" and info.version == "7-Zip 24.09"


def test_check_seven_zip_needs_a_version_banner(fake_seven_zip) -> None:
    fake_seven_zip.banner = "7-Zip"  # what SevenZip.version() returns for an unrecognised banner
    info = settings_sevenzip.check_seven_zip(r"C:\Tools\7z.exe")
    assert not info.found and "does not look like 7-Zip" in info.error


@pytest.mark.parametrize("configured", [r"C:\Tools\7-Zip\7z.exe", r"C:\Tools\7-Zip", r"C:\Tools\7-Zip\7zFM.exe"])
def test_detect_reports_a_configured_location_as_custom(fake_seven_zip, monkeypatch, configured) -> None:
    monkeypatch.setattr(fake_seven_zip, "locate", staticmethod(lambda c=None: r"C:\Tools\7-Zip\7z.exe"),
                        raising=False)
    assert settings_sevenzip.detect_seven_zip(configured).custom is True
    assert settings_sevenzip.detect_seven_zip("").custom is False


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------


def test_account_section_follows_auth_live(page: SettingsPage, fake_ctx, nav, qtbot) -> None:
    page.show_section("account")
    assert page.account_name.text() == "Not signed in"
    assert not page.sign_in_button.isHidden()
    page.sign_in_button.click()
    assert ("request_login", (), {}) in nav.calls

    fake_ctx.auth.login("player@example.com", "password")
    qtbot.waitUntil(lambda: page.account_name.text() == "player", timeout=3000)
    assert page.account_detail.text() == "player@example.com"
    assert page.avatar.initials() == "PL"
    assert page.sign_in_button.isHidden() and not page.sign_out_button.isHidden()

    page.sign_out_button.click()
    qtbot.waitUntil(lambda: page.account_name.text() == "Not signed in", timeout=3000)
    assert fake_ctx.auth.user is None
    assert ("Signed out of AnkerGames.", "success") in nav.toasts


def test_open_account_page_uses_profile_url(page, fake_ctx, monkeypatch, qtbot) -> None:
    opened: list[str] = []
    monkeypatch.setattr(settings_page, "open_url", lambda url: opened.append(url) or True)
    page.show_section("account")
    fake_ctx.auth._user = UserInfo(display_name="Nova", email="n@x.io", profile_url="https://ankergames.net/u/nova",
                                   is_subscriber=True)
    fake_ctx.events.publish(ev.AuthChanged(fake_ctx.auth.user))
    qtbot.waitUntil(lambda: page.account_name.text() == "Nova", timeout=3000)
    assert not page.account_badge.isHidden()
    page.profile_button.click()
    assert opened == ["https://ankergames.net/u/nova"]


# ---------------------------------------------------------------------------
# appearance
# ---------------------------------------------------------------------------


def test_theme_card_applies_theme_live_and_persists(page: SettingsPage, fake_ctx, theme, qtbot) -> None:
    page.show_section("appearance")
    card = page.theme_picker.cards()["daylight"]
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert theme.current_key == "daylight"
    assert palette.current().key == "daylight"
    assert fake_ctx.settings.get().theme == "daylight"
    assert page.theme_picker.current() == "daylight"
    fake_ctx.settings.update(theme="terminal")
    qtbot.waitUntil(lambda: page.theme_picker.current() == "terminal", timeout=2000)


# ---------------------------------------------------------------------------
# advanced
# ---------------------------------------------------------------------------


def test_catalog_stats_and_sync_now(page: SettingsPage, fake_ctx, nav, qtbot) -> None:
    page.show_section("advanced")
    qtbot.waitUntil(lambda: "60 games" in page.catalog_row.description_label.text(), timeout=3000)
    page.sync_button.click()
    assert not page.sync_button.isEnabled()
    assert not page.sync_progress_holder.isHidden()
    qtbot.waitUntil(lambda: page.sync_progress.maximum() == 3 and page.sync_progress.value() >= 1, timeout=3000)
    assert "of 3" in page.sync_status.text()
    qtbot.waitUntil(page.sync_button.isEnabled, timeout=5000)
    assert page.sync_progress_holder.isHidden()
    assert ("Store index is up to date.", "success") in nav.toasts


def test_catalog_sync_can_be_cancelled(page: SettingsPage, nav, qtbot) -> None:
    page.show_section("advanced")
    page.sync_button.click()
    page.sync_cancel_button.click()
    qtbot.waitUntil(page.sync_button.isEnabled, timeout=3000)
    assert not any("Store index" in t for t, _ in nav.toasts)


def test_sync_progress_from_elsewhere_is_shown(page: SettingsPage, fake_ctx, qtbot) -> None:
    page.show_section("advanced")
    fake_ctx.events.publish(ev.CatalogSyncProgress(2, 5))
    qtbot.waitUntil(lambda: not page.sync_progress_holder.isHidden(), timeout=2000)
    assert page.sync_progress.value() == 2 and page.sync_status.text().endswith("page 2 of 5")
    fake_ctx.events.publish(ev.CatalogUpdated(60, 1))
    qtbot.waitUntil(page.sync_progress_holder.isHidden, timeout=2000)


def test_image_cache_size_and_clear(page: SettingsPage, fake_ctx, nav, qtbot) -> None:
    fake_ctx.images.fetch("https://fake.invalid/poster/one.png")
    page.show_section("advanced")
    page.on_activated()
    qtbot.waitUntil(lambda: "KB" in page.cache_row.description_label.text(), timeout=3000)
    page.clear_cache_button.click()
    qtbot.waitUntil(lambda: page.cache_row.description_label.text() == "Using 0 B on disk.", timeout=3000)
    assert ("Image cache cleared.", "success") in nav.toasts
    assert fake_ctx.images.size_bytes() == 0


def test_log_level_applies_to_handlers(page: SettingsPage, fake_ctx, monkeypatch) -> None:
    levels: list[str] = []
    monkeypatch.setattr(settings_page.logging_setup, "set_level", levels.append)
    page.show_section("advanced")
    choose(page.findChild(QComboBox, "setting_log_level"), "DEBUG")
    assert fake_ctx.settings.get().log_level == "DEBUG"
    assert levels == ["DEBUG"]


def test_open_folders_use_the_shell(page: SettingsPage, fake_ctx, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(settings_controls.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    page.show_section("advanced")
    buttons = {b.text(): b for b in page.sections["advanced"].findChildren(settings_page.QPushButton)}
    buttons["Open logs folder"].click()
    buttons["Open data folder"].click()
    assert [Path(p) for p in opened] == [fake_ctx.paths.logs_dir, fake_ctx.paths.config_dir]


def test_reset_settings_keeps_library_folders(page, fake_ctx, theme, monkeypatch, qtbot, tmp_path) -> None:
    lib = [fake_ctx.settings.get().library_dirs[0], str(tmp_path / "B")]
    fake_ctx.settings.update(library_dirs=lib, default_library=lib[1], theme="vaporwave", max_concurrent_downloads=4,
                             close_behavior="quit", log_level="DEBUG")
    theme.apply("vaporwave")
    build(page, "advanced", "downloads")
    monkeypatch.setattr(settings_page, "confirm", lambda *a, **k: False)
    page.reset_button.click()
    assert fake_ctx.settings.get().max_concurrent_downloads == 4
    levels: list[str] = []
    monkeypatch.setattr(settings_page.logging_setup, "set_level", levels.append)
    monkeypatch.setattr(settings_page, "confirm", lambda *a, **k: True)
    page.reset_button.click()
    s = fake_ctx.settings.get()
    assert s.library_dirs == lib and s.default_library == lib[1]
    assert (s.theme, s.max_concurrent_downloads, s.close_behavior, s.log_level) == ("midnight", 1, "ask", "INFO")
    assert s.first_run_completed is True
    assert theme.current_key == "midnight" and levels == ["INFO"]
    assert page.findChild(QSpinBox, "setting_max_concurrent_downloads").value() == 1


def test_run_setup_wizard_again(page: SettingsPage, fake_ctx, qtbot) -> None:
    page.run_setup_wizard()
    wizard = page.setup_wizard()
    assert wizard is not None and wizard.isVisible()
    page.run_setup_wizard()  # second click focuses the same wizard
    assert page.setup_wizard() is wizard
    wizard.reject()
    qtbot.waitUntil(lambda: page.setup_wizard() is None, timeout=2000)


# ---------------------------------------------------------------------------
# about
# ---------------------------------------------------------------------------


def test_check_for_updates_up_to_date(page: SettingsPage, qtbot) -> None:
    page.show_section("about")
    assert page.version_label.text() == f"Version {__version__}"
    page.update_button.click()
    qtbot.waitUntil(lambda: page.update_status.kind == "success", timeout=3000)
    assert "up to date" in page.update_status.text()


def test_check_for_updates_offers_a_new_release(page, fake_ctx, monkeypatch, qtbot) -> None:
    release = AppRelease(version="1.2.0", url="https://github.com/x/releases/tag/v1.2.0",
                         download_url="https://github.com/x/AnkerClient-Setup.exe")
    monkeypatch.setattr(fake_ctx.app_updates, "check", lambda *, token=None: release)
    opened: list[str] = []
    monkeypatch.setattr(settings_page, "open_url", lambda url: opened.append(url) or True)
    page.show_section("about")
    page.update_button.click()
    qtbot.waitUntil(lambda: not page.download_update_button.isHidden(), timeout=3000)
    assert "1.2.0" in page.update_status.text()
    assert page.update_button.property("variant") == "secondary"  # one primary action: the download
    page.download_update_button.click()
    assert opened == [release.download_url]


def test_update_check_errors_are_shown(page, fake_ctx, monkeypatch, qtbot) -> None:
    def fail(*, token=None):
        raise ConnectionError("offline")

    monkeypatch.setattr(fake_ctx.app_updates, "check", fail)
    page.show_section("about")
    page.update_button.click()
    qtbot.waitUntil(lambda: page.update_status.kind == "error", timeout=3000)
    assert "offline" in page.update_status.text() and page.update_button.isEnabled()


def test_app_update_event_is_shown(page: SettingsPage, fake_ctx, qtbot) -> None:
    page.show_section("about")
    fake_ctx.events.publish(ev.AppUpdateAvailable(AppRelease(version="2.0.0", url="https://example.invalid/r")))
    qtbot.waitUntil(lambda: not page.download_update_button.isHidden(), timeout=2000)
    assert "2.0.0" in page.update_status.text()


def test_events_for_unbuilt_sections_are_kept_for_later(page: SettingsPage, fake_ctx, qtbot) -> None:
    assert not page.is_built("about") and not page.is_built("advanced") and not page.is_built("account")
    fake_ctx.events.publish(ev.AppUpdateAvailable(AppRelease(version="3.0.0", url="https://example.invalid/r")))
    fake_ctx.events.publish(ev.CatalogSyncProgress(1, 4))
    fake_ctx.auth.login("player@example.com", "password")
    qtbot.wait(50)  # events delivered while the sections do not exist yet
    page.show_section("about")
    assert "3.0.0" in page.update_status.text() and not page.download_update_button.isHidden()
    page.show_section("account")
    assert page.account_name.text() == "player"


def test_sections_are_built_lazily(page: SettingsPage) -> None:
    assert page.is_built("general")
    assert not any(page.is_built(k) for k in SECTIONS if k != "general")
    page.show_section("downloads")
    assert page.is_built("downloads") and page.speed_combo.currentData() == 0


# ---------------------------------------------------------------------------
# visuals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["midnight", "daylight"])
def test_screenshots(qtbot, fake_ctx, bridge, theme, nav, stubs, monkeypatch, theme_key: str) -> None:
    def slow_sync(*, full: bool = False, token: Any, on_progress: Any = None) -> int:
        fake_ctx.events.publish(ev.CatalogSyncProgress(12, 37))
        token.wait(10)
        return 0

    monkeypatch.setattr(fake_ctx.catalog, "sync", slow_sync)
    theme.apply(theme_key)
    p = SettingsPage(fake_ctx, bridge, nav, theme)  # type: ignore[arg-type]
    try:
        p.on_activated()
        # One representative, busy state per theme here; every section is rendered by the manual QA script.
        p.show_section("advanced")
        p.sync_catalog()
        path = screenshot(p, f"settings_ui_settings_advanced_syncing_{theme_key}", (1100, 760))
        assert path.exists()
        assert p.sync_status.text() == "Syncing the store index… page 12 of 37"
        p.sync_cancel_button.click()
        qtbot.waitUntil(p.sync_button.isEnabled, timeout=5000)
    finally:
        p.shutdown()
        destroy(p)
        QApplication.processEvents()
