"""Shell building blocks: toasts, sidebar, header, status strip, tray, wallpaper, page factory, dialogs."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QSize, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton, QWidget

from anker_client.core.errors import LaunchError
from anker_client.core.models import DownloadJob, DownloadOption, InstalledGame, JobState, UserInfo
from anker_client.ui import shell_dialogs
from anker_client.ui.shell_pages import PAGE_SPECS, PagePlaceholder, call_page, create_page, is_placeholder
from anker_client.ui.shell_summary import summarize_jobs
from anker_client.ui.shell_wallpaper import WallpaperSurface, cover_source_rect
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.tray import TrayController, recently_played
from anker_client.ui.widgets.header import Header
from anker_client.ui.widgets.sidebar import Sidebar, initials
from anker_client.ui.widgets.status_strip import StatusStrip
from anker_client.ui.widgets.toast import (
    DEFAULT_TIMEOUT_MS,
    ERROR_TIMEOUT_MS,
    Toast,
    ToastManager,
    default_timeout,
    normalize_level,
)


@pytest.fixture(autouse=True)
def theme(qapp: QApplication) -> ThemeManager:
    manager = ThemeManager(QApplication.instance())
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")
    return manager


@pytest.fixture
def host(qtbot: Any) -> QWidget:
    widget = QWidget()
    widget.resize(900, 600)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def _job(state: JobState, title: str = "Game", **kw: Any) -> DownloadJob:
    return DownloadJob(id=f"{title}-{state.value}", slug=title.lower(), title=title, option=DownloadOption(1, "Direct"),
                       library_root="C:/Games", state=state, **kw)


# --- toasts ----------------------------------------------------------------------------------------------


def test_toast_levels_and_timeouts() -> None:
    assert normalize_level("bogus") == "info"
    assert default_timeout("info", False) == DEFAULT_TIMEOUT_MS
    assert default_timeout("error", True) == ERROR_TIMEOUT_MS
    assert default_timeout("success", True) > DEFAULT_TIMEOUT_MS


def test_toast_stack_is_capped_newest_at_bottom(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host, animate=False)
    toasts = [manager.show_toast(f"message {i}", timeout_ms=0) for i in range(6)]
    assert manager.toasts() == toasts[2:]
    assert toasts[0].is_dismissed and toasts[1].is_dismissed
    ys = [t.y() for t in manager.toasts()]
    assert ys == sorted(ys)  # oldest at the top, newest at the bottom
    newest = manager.toasts()[-1]
    assert newest.geometry().right() <= host.width()
    assert newest.geometry().bottom() <= host.height()


def test_toast_auto_dismiss(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host, animate=False)
    toast = manager.show_toast("bye", timeout_ms=50)
    qtbot.waitUntil(lambda: manager.toasts() == [], timeout=2000)
    assert toast.is_dismissed


def test_toast_click_dismisses_and_action_runs(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host, animate=False)
    first = manager.show_toast("click me", timeout_ms=0)
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton, pos=first.rect().center())
    assert first.is_dismissed

    ran: list[int] = []
    second = manager.show_toast("with action", "success", title="Done", action_text="View",
                                on_action=lambda: ran.append(1), timeout_ms=0)
    assert second.action_button is not None
    second.action_button.click()
    assert ran == [1] and second.is_dismissed

    third = manager.show_toast("close me", timeout_ms=0)
    third.close_button.click()
    assert third.is_dismissed
    assert manager.toasts() == []


def test_toast_action_errors_are_contained(qtbot: Any, host: QWidget) -> None:
    def boom() -> None:
        raise RuntimeError("nope")

    toast = ToastManager(host, animate=False).show_toast("x", action_text="Go", on_action=boom, timeout_ms=0)
    toast.action_button.click()  # logged, not raised
    assert toast.is_dismissed


def test_toast_hover_pauses_timer(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host, animate=False)
    toast = manager.show_toast("hover", timeout_ms=150)
    QApplication.sendEvent(toast, QEvent(QEvent.Type.Enter))
    qtbot.wait(300)
    assert not toast.is_dismissed
    QApplication.sendEvent(toast, QEvent(QEvent.Type.Leave))
    qtbot.waitUntil(lambda: toast.is_dismissed, timeout=2000)


def test_toast_repositions_on_host_resize(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host, animate=False)
    toast = manager.show_toast("stay in the corner", timeout_ms=0)
    host.resize(1200, 700)
    qtbot.waitUntil(lambda: toast.geometry().right() > 900, timeout=2000)
    assert isinstance(toast, Toast)


def test_toast_wraps_long_paths_instead_of_cutting_them(qtbot: Any, host: QWidget) -> None:
    from anker_client.ui.widgets.toast import breakable

    path = r"C:\Users\Player\AppData\Local\AnkerClient\Library\.ankerclient\downloads\0123456789abcdef\Archive.part"
    toast = ToastManager(host, animate=False).show_toast(f"Could not create {path}", "error", title="Failed",
                                                         timeout_ms=0)
    message = toast._message_label  # noqa: SLF001
    assert message.minimumSizeHint().width() <= message.width()  # every line fits: nothing is cut off
    assert message.text().replace("\u200b", "") == toast.message == f"Could not create {path}"
    assert toast.close_button.geometry().right() < toast.width()
    assert breakable("short words stay as they are") == "short words stay as they are"
    assert breakable("a\nb") == "a\nb"


def test_toast_animated_stack(qtbot: Any, host: QWidget) -> None:
    manager = ToastManager(host)
    a = manager.show_toast("a", timeout_ms=0)
    manager.show_toast("b", timeout_ms=0)
    qtbot.wait(300)
    assert a.graphicsEffect() is None  # fade-in effect removed when finished
    manager.clear()
    assert manager.toasts() == []


# --- sidebar ---------------------------------------------------------------------------------------------------


def test_initials() -> None:
    assert initials("Jane Doe") == "JD"
    assert initials("player42") == "P"
    assert initials("jane.doe") == "JD"
    assert initials("") == "?"


def test_sidebar_navigation_and_badges(qtbot: Any) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    with qtbot.waitSignal(sidebar.navigate) as blocker:
        sidebar.button("library").click()
    assert blocker.args == ["library"]
    sidebar.set_current("downloads")
    assert sidebar.current() == "downloads"
    sidebar.set_current("game")  # no entry: nothing selected
    assert sidebar.current() == ""

    sidebar.set_library_count(12)
    assert sidebar.button("library").badge_text() == "12"
    sidebar.set_library_count(0)
    assert sidebar.button("library").badge_text() == ""

    summary = summarize_jobs([_job(JobState.DOWNLOADING, bytes_total=100, bytes_done=25),
                              _job(JobState.PAUSED, "Other")])
    sidebar.set_downloads(summary)
    btn = sidebar.button("downloads")
    assert btn.badge_text() == "2" and btn.badge.property("role") == "badge-accent"
    assert btn.progress == pytest.approx(0.25)
    sidebar.set_downloads(summarize_jobs([_job(JobState.PAUSED)]))
    assert btn.badge.property("role") == "badge" and btn.progress is None
    sidebar.set_downloads(summarize_jobs([_job(JobState.FAILED)]))
    assert btn.badge_text() == "!" and btn.badge.property("role") == "badge-danger"
    sidebar.set_downloads(summarize_jobs([]))
    assert btn.badge_text() == ""


def test_sidebar_account_chip(qtbot: Any) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    assert sidebar.account.name_label.text() == "Sign in"
    sidebar.set_user(UserInfo(display_name="Jane Doe", is_subscriber=True))
    assert sidebar.account.avatar.text() == "JD"
    assert sidebar.account.caption_label.text() == "Subscriber"
    assert sidebar.account.sizeHint().height() >= 52
    with qtbot.waitSignal(sidebar.account_clicked):
        sidebar.account.click()
    sidebar.refresh_icons()


# --- header -------------------------------------------------------------------------------------------------


def test_header_search_debounce_enter_and_programmatic(qtbot: Any) -> None:
    header = Header(debounce_ms=40)
    qtbot.addWidget(header)
    emitted: list[str] = []
    header.search_requested.connect(emitted.append)
    header.search.setText("ho")
    header.search.setText("hol")
    qtbot.waitUntil(lambda: emitted == ["hol"], timeout=2000)
    header.search.returnPressed.emit()  # Enter re-emits even for the same text
    assert emitted == ["hol", "hol"]
    header.set_search_text("programmatic")
    qtbot.wait(120)
    assert emitted == ["hol", "hol"]
    assert header.search_text() == "programmatic"
    header.search.clear()
    qtbot.waitUntil(lambda: emitted[-1] == "", timeout=2000)


def test_header_escape_clears(qtbot: Any) -> None:
    header = Header(debounce_ms=40)
    qtbot.addWidget(header)
    header.search.setText("abc")
    qtbot.keyClick(header.search, Qt.Key.Key_Escape)
    assert header.search_text() == ""


def test_header_sync_updates_and_back(qtbot: Any) -> None:
    header = Header()
    qtbot.addWidget(header)
    assert header.sync_text() == ""
    header.set_sync_progress(3, 37)
    assert header.sync_text() == "Syncing catalog · 3/37"
    header.set_sync_progress(2, None)
    assert header.sync_text() == "Syncing catalog · page 2"
    header.set_sync_finished(12)
    assert header.sync_text() == "12 new games in the store"
    header.set_sync_finished(0)
    assert header.sync_text() == "Catalog up to date"

    header.set_updates(1)
    assert header.updates_chip.text() == "1 update" and not header.updates_chip.isHidden()
    header.set_updates(0)
    assert header.updates_chip.isHidden()
    header.set_app_update("2.0.0")
    assert not header.app_update_chip.isHidden()
    header.set_app_update("")
    assert header.app_update_chip.isHidden()

    with qtbot.waitSignal(header.back_requested):
        header.set_back_enabled(True)
        header.back_button.click()


# --- status strip ------------------------------------------------------------------------------------------


def test_status_strip_summary_and_running(qtbot: Any) -> None:
    strip = StatusStrip()
    qtbot.addWidget(strip)
    assert strip.summary_text() == "No downloads in progress"
    strip.set_summary(summarize_jobs([_job(JobState.PAUSED)]))
    assert strip.summary_text() == "Paused · 1 download"
    strip.set_running(["Celeste"])
    assert strip.running_text() == "Playing Celeste"
    strip.set_running(["Celeste", "Hades II", "Inside"])
    assert strip.running_text() == "Playing Celeste + 2 more"
    strip.set_running([])
    assert strip.running_label.isHidden()
    with qtbot.waitSignal(strip.downloads_clicked):
        strip.downloads_button.click()


# --- tray ---------------------------------------------------------------------------------------------------


def _game(install_id: str, last_played: str, executable: str = "game.exe", hidden: bool = False) -> InstalledGame:
    return InstalledGame(install_id=install_id, title=install_id.title(), path=f"C:/Games/{install_id}",
                         library_root="C:/Games", last_played=last_played, executable=executable, hidden=hidden)


def test_recently_played_order_and_filters() -> None:
    games = [
        _game("old", "2026-01-01T00:00:00+00:00"),
        _game("new", "2026-10-01T00:00:00Z"),
        _game("never", ""),
        _game("noexe", "2026-10-02T00:00:00+00:00", executable=""),
        _game("hidden", "2026-10-03T00:00:00+00:00", hidden=True),
        _game("naive", "2026-05-01T00:00:00"),
        _game("garbage", "yesterday"),
    ]
    assert [g.install_id for g in recently_played(games)] == ["new", "naive", "old", "garbage"]
    assert len(recently_played([_game(f"g{i}", f"2026-01-0{i + 1}T00:00:00+00:00") for i in range(8)])) == 5


def test_tray_controller_menu_and_messages(qtbot: Any, fake_ctx: Any) -> None:
    from anker_client.ui.main_window import app_icon

    owner = QWidget()
    qtbot.addWidget(owner)
    tray = TrayController(fake_ctx, app_icon(), owner, available=False)
    assert not tray.available
    assert tray.show_message("t", "m") is False and tray.last_message == ("t", "m", "info")
    tray.refresh_recent()
    assert tray.recent_titles()[:1] == ["Hollow Knight"]

    tray.set_summary(summarize_jobs([]))
    assert not tray.pause_action.isEnabled() and not tray.resume_action.isEnabled()
    assert tray.tooltip() == "AnkerClient"
    tray.set_summary(summarize_jobs([_job(JobState.DOWNLOADING, bytes_total=10, bytes_done=5, speed_bps=2048)]))
    assert tray.pause_action.isEnabled()
    assert "1 download" in tray.tooltip()

    with qtbot.waitSignal(tray.open_requested):
        tray.open_action.trigger()
    with qtbot.waitSignal(tray.quit_requested):
        tray.quit_action.trigger()

    tray.resume_action.trigger()  # disabled: nothing is paused in that summary
    qtbot.wait(50)
    assert any(j.state is JobState.PAUSED for j in fake_ctx.downloads.jobs())
    tray.set_summary(summarize_jobs([_job(JobState.PAUSED)]))
    tray.resume_action.trigger()  # FakeDownloads.resume_all via the task runner
    qtbot.waitUntil(lambda: not any(j.state is JobState.PAUSED for j in fake_ctx.downloads.jobs()), timeout=2000)
    tray.shutdown()


def test_tray_launch_runs_in_background_and_reports_errors(qtbot: Any, fake_ctx: Any) -> None:
    from anker_client.ui.main_window import app_icon

    owner = QWidget()
    qtbot.addWidget(owner)
    tray = TrayController(fake_ctx, app_icon(), owner, available=False)
    tray.launch("hollow-knight")
    qtbot.waitUntil(lambda: fake_ctx.launcher.is_running("hollow-knight"), timeout=2000)

    def fail(_install_id: str) -> None:
        raise LaunchError("missing exe")

    fake_ctx.launcher.launch = fail
    with qtbot.waitSignal(tray.launch_failed, timeout=2000) as blocker:
        tray.launch("celeste")
    assert blocker.args[0] == "celeste" and isinstance(blocker.args[1], LaunchError)
    tray.shutdown()


# --- wallpaper ------------------------------------------------------------------------------------------------


def test_cover_source_rect() -> None:
    rect = cover_source_rect(QSize(1000, 500), QSize(500, 500))
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (250, 0, 500, 500)
    rect = cover_source_rect(QSize(400, 800), QSize(800, 400))
    assert (rect.x(), rect.width(), rect.height()) == (0, 400, 200)
    assert cover_source_rect(QSize(0, 0), QSize(10, 10)).width() == 0


def test_wallpaper_static_animated_and_pause(qtbot: Any) -> None:
    surface = WallpaperSurface()
    qtbot.addWidget(surface)
    surface.resize(400, 300)
    surface.show()
    surface.set_wallpaper("bliss.png")
    assert surface.wallpaper == "bliss.png" and not surface.is_animated
    surface.set_wallpaper("vaporwave.gif")
    assert surface.is_animated and surface.is_playing
    surface.set_paused(True)
    assert not surface.is_playing
    surface.set_paused(False)
    assert surface.is_playing
    surface.grab()  # paints without errors
    surface.set_wallpaper("does-not-exist.png")
    assert not surface.is_animated
    surface.set_wallpaper("")
    surface.grab()


# --- page factory ------------------------------------------------------------------------------------------------


def test_create_page_failure_gives_placeholder(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import anker_client.ui.pages.library as library_mod

    def broken(*_args: Any, **_kwargs: Any) -> QWidget:
        raise NotImplementedError("library page missing")

    monkeypatch.setattr(library_mod, "LibraryPage", broken)
    page = create_page(PAGE_SPECS["library"], None, None, None, None, None)
    qtbot.addWidget(page)
    assert isinstance(page, PagePlaceholder) and is_placeholder(page)
    assert "library page missing" in page.details.toPlainText()
    page.copy_button.click()
    assert "library page missing" in QApplication.clipboard().text()
    with qtbot.waitSignal(page.retry_requested) as blocker:
        page.retry_button.click()
    assert blocker.args == ["library"]


def test_create_page_bad_module(qtbot: Any) -> None:
    from anker_client.ui.shell_pages import PageSpec

    page = create_page(PageSpec("x", "X", "anker_client.ui.pages.does_not_exist", "Nope"), None, None, None, None,
                       None)
    qtbot.addWidget(page)
    assert is_placeholder(page)


def test_create_page_passes_theme_to_settings(monkeypatch: pytest.MonkeyPatch, qtbot: Any) -> None:
    import anker_client.ui.pages.settings as settings_mod

    received: list[tuple[Any, ...]] = []

    class Page(QWidget):
        def __init__(self, *args: Any) -> None:
            super().__init__()
            received.append(args)

    monkeypatch.setattr(settings_mod, "SettingsPage", Page)
    page = create_page(PAGE_SPECS["settings"], "ctx", "bridge", "nav", "loader", "theme")
    qtbot.addWidget(page)
    assert received == [("ctx", "bridge", "nav", "theme")]


def test_call_page_contains_errors(qtbot: Any) -> None:
    class Page(QWidget):
        def ok(self, x: int) -> int:
            return x * 2

        def bad(self) -> None:
            raise RuntimeError("bad hook")

    page = Page()
    qtbot.addWidget(page)
    assert call_page(page, "ok", 21) == 42
    assert call_page(page, "bad") is None
    assert call_page(page, "missing") is None
    assert call_page(None, "ok", 1) is None


# --- close / quit dialogs --------------------------------------------------------------------------------------


def _click_in_modal(object_name: str, *, check_remember: bool = False) -> None:
    def act() -> None:
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            QTimer.singleShot(20, act)
            return
        if check_remember:
            remember = dialog.findChild(QCheckBox, "remember")
            assert remember is not None
            remember.setChecked(True)
        target = dialog.findChild(QPushButton, object_name)
        assert target is not None, object_name
        target.click()

    QTimer.singleShot(20, act)


def test_ask_close_action_choices(qtbot: Any) -> None:
    _click_in_modal("button_tray", check_remember=True)
    decision = shell_dialogs.ask_close_action(None, tray_available=True)
    assert decision == shell_dialogs.CloseDecision(shell_dialogs.CloseChoice.TRAY, True)

    _click_in_modal("button_quit")
    decision = shell_dialogs.ask_close_action(None, tray_available=False)
    assert decision == shell_dialogs.CloseDecision(shell_dialogs.CloseChoice.QUIT, False)

    _click_in_modal("button_cancel")
    assert shell_dialogs.ask_close_action(None, tray_available=True) is None


def test_confirm_quit_with_downloads(qtbot: Any) -> None:
    _click_in_modal("button_quit")
    assert shell_dialogs.confirm_quit_with_downloads(None, 2) is True
    _click_in_modal("button_cancel")
    assert shell_dialogs.confirm_quit_with_downloads(None, 1) is False


def test_shell_dialogs_do_not_pile_up_under_their_parent(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QDialog

    parent = QWidget()
    qtbot.addWidget(parent)
    for _ in range(3):
        _click_in_modal("button_tray")
        assert shell_dialogs.ask_close_action(parent, tray_available=True) is not None
        _click_in_modal("button_cancel")
        shell_dialogs.confirm_quit_with_downloads(parent, 1)
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert parent.findChildren(QDialog) == []


# --- status strip eliding / sync failure -------------------------------------------------------------------


def test_status_strip_elides_long_titles_without_growing(qtbot: Any) -> None:
    strip = StatusStrip()
    qtbot.addWidget(strip)
    strip.resize(900, 30)
    strip.show()
    title = "An Extraordinarily Long Game Title " * 6
    summary = summarize_jobs([_job(JobState.DOWNLOADING, title.strip(), bytes_total=100, bytes_done=10,
                                   speed_bps=1024)])
    strip.set_summary(summary)
    strip.set_running([title.strip()])
    assert strip.summary_text() == summary.headline  # the full text stays available
    assert strip.downloads_button.text().endswith("…") and strip.downloads_button.text() != summary.headline
    assert summary.headline in strip.downloads_button.toolTip()
    assert strip.running_label.text().endswith("…")
    assert strip.running_text() == f"Playing {title.strip()}"
    assert strip.minimumSizeHint().width() < 500  # never forces the window wider
    strip.resize(1600, 30)
    qtbot.waitUntil(lambda: len(strip.downloads_button.text()) > 60, timeout=2000)


def test_header_sync_failure(qtbot: Any) -> None:
    header = Header()
    qtbot.addWidget(header)
    header.set_sync_progress(1, 37)
    header.set_sync_finished(0)
    header.set_sync_failed("The site did not answer")
    assert header.sync_text() == "Catalog sync failed"
    assert header.sync_indicator.toolTip() == "The site did not answer"
    header.refresh_icons()  # keeps the warning glyph
    header.set_sync_progress(2, 37)
    assert header.sync_text() == "Syncing catalog · 2/37"


def test_theme_indicator_images_are_written_once(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from anker_client.ui.theme import manager

    monkeypatch.setattr(manager, "_indicator_dir", lambda: Path(tmp_path) / "theme")
    first = manager.indicator_images(palette.current())
    second = manager.indicator_images(palette.current())
    assert first == second and first
    assert all(Path(url).read_text(encoding="utf-8").startswith("<svg") for url in first.values())
    assert not list((Path(tmp_path) / "theme").glob("*.tmp"))  # no temp files left behind
    manager._write_once(Path(first["check"]), "<svg/>")  # replacing an existing file works too
    assert Path(first["check"]).read_text(encoding="utf-8") == "<svg/>"
