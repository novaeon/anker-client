"""MainWindow (shell) tests on FakeContext with recording placeholder pages."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from anker_client.core import events as ev
from anker_client.core.errors import ExecutableNotSetError, LaunchError
from anker_client.core.models import (
    AppRelease,
    DownloadJob,
    DownloadKind,
    DownloadOption,
    GameUpdate,
    JobState,
    UserInfo,
)
from anker_client.ui import main_window as main_window_mod
from anker_client.ui import shell_dialogs
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.main_window import Location, MainWindow
from anker_client.ui.shell_pages import PagePlaceholder
from anker_client.ui.theme import palette
from anker_client.ui.theme.manager import ThemeManager
from tests.fakes import sample_details, screenshot

PAGE_MODULES = {
    "store": ("anker_client.ui.pages.store", "StorePage"),
    "game": ("anker_client.ui.pages.game", "GamePage"),
    "library": ("anker_client.ui.pages.library", "LibraryPage"),
    "downloads": ("anker_client.ui.pages.downloads", "DownloadsPage"),
    "settings": ("anker_client.ui.pages.settings", "SettingsPage"),
}


class RecordingPage(QWidget):
    """Stand-in page recording every Navigator-facing call."""

    key = ""

    def __init__(self, ctx: Any, bridge: Any, nav: Any, extra: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self.nav = nav
        self.extra = extra
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)

    def set_query(self, query: str) -> None:
        self._record("set_query", query)

    def set_genre(self, genre: str) -> None:
        self._record("set_genre", genre)

    def load(self, slug: str, summary: Any = None) -> None:
        self._record("load", slug)

    def select(self, install_id: str) -> None:
        self._record("select", install_id)

    def show_section(self, section: str) -> None:
        self._record("show_section", section)

    def set_filter(self, name: str) -> None:
        self._record("set_filter", name)

    def refresh(self) -> None:
        self._record("refresh")

    def on_activated(self) -> None:
        self._record("on_activated")

    def on_deactivated(self) -> None:
        self._record("on_deactivated")

    def shutdown(self) -> None:
        self._record("shutdown")

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]


def _dispose(widget: QWidget) -> None:
    """Delete now: leftover windows would make every later stylesheet change slower."""
    widget.hide()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


@pytest.fixture
def theme(qapp: QApplication) -> ThemeManager:
    manager = ThemeManager(QApplication.instance())
    if palette.current().key != "midnight" or not qapp.styleSheet():
        manager.apply("midnight")
    else:
        manager._key = "midnight"  # same palette already applied by an earlier test: skip the restyle
    return manager


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> dict[str, RecordingPage]:
    import importlib

    created: dict[str, RecordingPage] = {}
    for key, (module_name, class_name) in PAGE_MODULES.items():
        module = importlib.import_module(module_name)

        def factory(*args: Any, _key: str = key, **kwargs: Any) -> RecordingPage:
            page = RecordingPage(*args, **kwargs)
            page.key = _key
            created[_key] = page
            return page

        monkeypatch.setattr(module, class_name, factory)
    return created


@pytest.fixture
def quits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(main_window_mod, "quit_application", lambda: calls.append(1))
    return calls


@pytest.fixture
def opened_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(main_window_mod, "open_url", urls.append)
    return urls


def _make_window(fake_ctx: Any, theme: ThemeManager, **kwargs: Any) -> tuple[MainWindow, QtEventBridge]:
    bridge = QtEventBridge(fake_ctx.events)
    loader = ImageLoader(fake_ctx.images, fake_ctx.runner)
    kwargs.setdefault("tray_available", False)
    window = MainWindow(fake_ctx, bridge, theme, loader=loader, **kwargs)
    return window, bridge


@pytest.fixture
def window(qtbot: Any, fake_ctx: Any, theme: ThemeManager, pages: dict, quits: list[int]) -> MainWindow:
    win, bridge = _make_window(fake_ctx, theme)
    yield win
    win.shutdown()
    bridge.close()
    _dispose(win)


def _flush(qtbot: Any, ms: int = 350) -> None:
    qtbot.wait(ms)


# --- construction & chrome ---------------------------------------------------------------------------


def test_initial_state_shows_store_and_badges(window: MainWindow, pages: dict) -> None:
    assert window.current_page_key() == "store"
    assert window.sidebar.current() == "store"
    assert window.sidebar.button("library").badge_text() == "5"
    # FakeDownloads: downloading, queued, paused, waiting, extracting are unfinished
    assert window.sidebar.button("downloads").badge_text() == "5"
    assert window.sidebar.button("downloads").progress is not None
    assert window.header.updates_chip.text() == "2 updates"
    assert not window.header.updates_chip.isHidden()
    assert window.status_strip.summary_text().startswith("Downloading ")
    assert window.minimumWidth() >= 1100 and window.minimumHeight() >= 700
    assert not window.header.back_button.isEnabled()
    assert pages["store"].named("on_activated")


def test_navigation_history_and_back(window: MainWindow, pages: dict) -> None:
    window.show_library()
    window.show_downloads()
    window.show_settings("advanced")
    assert window.current_page_key() == "settings"
    assert pages["settings"].named("show_section") == [("show_section", "advanced")]
    assert window.header.back_button.isEnabled()

    window.back()
    assert window.current_page_key() == "downloads"
    window.back()
    assert window.current_page_key() == "library"
    assert window.sidebar.current() == "library"
    window.back()
    assert window.current_page_key() == "store"
    window.back()  # no history left: stays
    assert window.current_page_key() == "store"
    assert not window.header.back_button.isEnabled()
    assert pages["library"].named("on_deactivated")


def test_same_location_is_not_recorded_twice(window: MainWindow) -> None:
    window.show_downloads()
    window.show_downloads()
    assert [loc.page for loc in window.history()] == ["store", "downloads"]


def test_game_page_loads_and_keeps_section_highlight(window: MainWindow, pages: dict) -> None:
    window.show_library("hollow-knight")
    assert pages["library"].named("select") == [("select", "hollow-knight")]
    window.show_game("celeste")
    assert window.current_page_key() == "game"
    assert pages["game"].named("load") == [("load", "celeste")]
    assert window.sidebar.current() == "library"
    window.back()
    assert window.current_page_key() == "library"
    window.show_game("celeste")  # same slug again: page keeps its state
    assert len(pages["game"].named("load")) == 1
    window.show_game("elden-ring")
    assert pages["game"].named("load")[-1] == ("load", "elden-ring")


def test_search_debounce_calls_store_and_replaces_history(qtbot: Any, window: MainWindow, pages: dict) -> None:
    window.show_library()
    window.header.search.setText("hol")
    qtbot.waitUntil(lambda: ("set_query", "hol") in pages["store"].calls, timeout=2000)
    assert window.current_page_key() == "store"
    window.header.search.setText("hollow")
    qtbot.waitUntil(lambda: ("set_query", "hollow") in pages["store"].calls, timeout=2000)
    # search refinements replace each other in the history
    assert [(loc.page, loc.query) for loc in window.history()] == [("store", ""), ("library", ""),
                                                                   ("store", "hollow")]
    window.back()
    assert window.current_page_key() == "library"
    assert window.header.search_text() == ""


def test_search_enter_is_immediate_and_clear_returns_to_browse(qtbot: Any, window: MainWindow,
                                                               pages: dict) -> None:
    window.header.search.setText("celeste")
    window.header.search.returnPressed.emit()
    assert ("set_query", "celeste") in pages["store"].calls
    window.header.search.clear()
    qtbot.waitUntil(lambda: pages["store"].calls[-1] == ("set_query", ""), timeout=2000)


def test_show_store_with_genre_and_back_restores_query(window: MainWindow, pages: dict) -> None:
    window.show_store(query="elden")
    window.show_game("elden-ring")
    window.show_store(genre="rpg")
    window.back()  # game
    window.back()  # store "elden"
    assert window.current_page_key() == "store"
    store_calls = [c for c in pages["store"].calls if c[0] in ("set_query", "set_genre")]
    assert store_calls[-3:] == [("set_query", ""), ("set_genre", "rpg"), ("set_query", "elden")]
    assert window.header.search_text() == "elden"


def test_repeating_a_search_reaches_the_store_page_again(qtbot: Any, window: MainWindow, pages: dict) -> None:
    # The real store page can leave a search on its own (search tab close button); the shell cannot
    # see that, so an explicit search must always be passed on (the page ignores true repeats).
    window.header.search.setText("hollow")
    window.header.search.returnPressed.emit()
    assert pages["store"].named("set_query") == [("set_query", "hollow")]
    window.header.search.returnPressed.emit()  # Enter again, same text
    assert pages["store"].named("set_query") == [("set_query", "hollow")] * 2
    window.show_store(query="hollow")  # e.g. a page asking for the same search
    assert len(pages["store"].named("set_query")) == 3
    window.show_store(genre="rpg")
    window.show_store(genre="rpg")
    assert pages["store"].named("set_genre") == [("set_genre", "rpg")] * 2


def test_sidebar_store_keeps_the_store_as_the_user_left_it(window: MainWindow, pages: dict) -> None:
    window.show_store(query="celeste")
    window.show_library()
    before = list(pages["store"].calls)
    window.sidebar.button("store").click()
    assert window.current_page_key() == "store"
    new_calls = [c for c in pages["store"].calls[len(before):] if c[0] in ("set_query", "set_genre")]
    assert new_calls == []  # no reset of the page's own tab/search state
    assert window.header.search_text() == "celeste"


def test_clearing_the_search_box_leaves_the_search(qtbot: Any, window: MainWindow, pages: dict) -> None:
    window.show_store(query="celeste")
    window.header.search.clear()
    qtbot.waitUntil(lambda: pages["store"].calls[-1] == ("set_query", ""), timeout=2000)
    assert [(loc.page, loc.query) for loc in window.history()] == [("store", "")]
    window.show_library()
    window.sidebar.button("store").click()
    assert window.header.search_text() == ""


def test_shortcuts_switch_pages(qtbot: Any, window: MainWindow) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    shortcuts = {sc.key().toString(): sc for sc in window._shortcuts}
    assert {"Ctrl+F", "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4", "Alt+Left", "F5", "Ctrl+Q"} <= set(shortcuts)
    shortcuts["Ctrl+2"].activated.emit()
    assert window.current_page_key() == "library"
    shortcuts["Ctrl+3"].activated.emit()
    assert window.current_page_key() == "downloads"
    shortcuts["Alt+Left"].activated.emit()
    assert window.current_page_key() == "library"
    shortcuts["Ctrl+4"].activated.emit()
    assert window.current_page_key() == "settings"
    shortcuts["Ctrl+1"].activated.emit()
    assert window.current_page_key() == "store"


def test_f5_refreshes_current_page(window: MainWindow, pages: dict) -> None:
    window.show_downloads()
    window.refresh_current_page()
    assert pages["downloads"].named("refresh")


def test_status_strip_click_opens_downloads(window: MainWindow) -> None:
    window.status_strip.downloads_button.click()
    assert window.current_page_key() == "downloads"


# --- live updates ---------------------------------------------------------------------------------------


def test_download_badge_and_status_update_from_events(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    job = DownloadJob(id="new-job", slug="celeste", title="Celeste", option=DownloadOption(1, "Direct"),
                      library_root="C:/Games", state=JobState.DOWNLOADING, bytes_total=100, bytes_done=50,
                      speed_bps=1024.0)
    fake_ctx.events.publish(ev.JobAdded(job))
    qtbot.waitUntil(lambda: window.sidebar.button("downloads").badge_text() == "6", timeout=2000)
    assert window.status_strip.summary_text().startswith("2 downloading")
    fake_ctx.events.publish(ev.JobRemoved("new-job"))
    qtbot.waitUntil(lambda: window.sidebar.button("downloads").badge_text() == "5", timeout=2000)


def test_queue_changed_reloads_jobs(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    fake_ctx.downloads.pause_all()
    qtbot.waitUntil(lambda: window.download_summary.downloading == 0, timeout=2000)
    assert window.status_strip.summary_text().startswith("Extracting")
    fake_ctx.downloads.clear_finished()
    qtbot.waitUntil(lambda: window.download_summary.failed == 0, timeout=2000)


def test_library_badge_and_updates_chip_follow_library(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    fake_ctx.library.set_hidden("hollow-knight", True)
    qtbot.waitUntil(lambda: window.sidebar.button("library").badge_text() == "4", timeout=2000)
    update_id = next(g.install_id for g in fake_ctx.library.games() if g.update_available)
    fake_ctx.library.set_update_state(update_id, latest_version="", available=False)
    qtbot.waitUntil(lambda: window.header.updates_chip.text() == "1 update", timeout=2000)


def test_notification_event_shows_toast_and_tray_when_hidden(qtbot: Any, window: MainWindow,
                                                             fake_ctx: Any) -> None:
    fake_ctx.events.publish(ev.Notification("Celeste installed", "Ready to play.", "success", tray=True))
    qtbot.waitUntil(lambda: len(window.toasts.toasts()) == 1, timeout=2000)
    toast = window.toasts.toasts()[0]
    assert (toast.title, toast.message, toast.level) == ("Celeste installed", "Ready to play.", "success")
    assert window.tray.last_message == ("Celeste installed", "Ready to play.", "success")  # window never shown

    window.show()
    qtbot.waitExposed(window)
    window.tray.last_message = None
    fake_ctx.events.publish(ev.Notification("Visible", "No balloon while the window is visible.", tray=True))
    qtbot.waitUntil(lambda: len(window.toasts.toasts()) == 2, timeout=2000)
    assert window.tray.last_message is None

    window.hide()
    fake_ctx.settings.update(notifications_enabled=False)
    fake_ctx.events.publish(ev.Notification("Muted", "Notifications are off.", tray=True))
    qtbot.waitUntil(lambda: len(window.toasts.toasts()) == 3, timeout=2000)
    assert window.tray.last_message is None


def test_game_installed_needing_executable_offers_picker(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.ui.dialogs import game_dialogs

    opened: list[str] = []

    class FakePicker(QDialog):
        def __init__(self, ctx: Any, install_id: str, parent: Any = None) -> None:
            super().__init__(parent)
            opened.append(install_id)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(game_dialogs, "ExecutablePickerDialog", FakePicker)
    fake_ctx.events.publish(ev.GameInstalled("celeste", "Celeste", needs_executable=True))
    qtbot.waitUntil(lambda: len(window.toasts.toasts()) == 1, timeout=2000)
    toast = window.toasts.toasts()[0]
    assert toast.action_button is not None and toast.action_button.text() == "Choose executable"
    toast.action_button.click()
    assert opened == ["celeste"]
    assert toast.is_dismissed


def test_game_installed_without_executable_problem_has_no_toast(qtbot: Any, window: MainWindow,
                                                                fake_ctx: Any) -> None:
    fake_ctx.events.publish(ev.GameInstalled("celeste", "Celeste"))
    _flush(qtbot)
    assert window.toasts.toasts() == []


def test_app_update_shows_chip_and_download_action(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                                   opened_urls: list[str]) -> None:
    release = AppRelease(version="1.2.0", url="https://github.com/novaeon/anker-client/releases/tag/v1.2.0")
    fake_ctx.events.publish(ev.AppUpdateAvailable(release))
    qtbot.waitUntil(lambda: not window.header.app_update_chip.isHidden(), timeout=2000)
    assert "1.2.0" in window.header.app_update_chip.text()
    toast = window.toasts.toasts()[-1]
    assert toast.action_button is not None and toast.action_button.text() == "Download"
    toast.action_button.click()
    assert opened_urls == [release.url]
    window.header.app_update_chip.click()
    assert opened_urls == [release.url, release.url]


def test_updates_found_toasts_only_new_and_chip_opens_filtered_library(qtbot: Any, window: MainWindow,
                                                                        fake_ctx: Any, pages: dict) -> None:
    known = fake_ctx.updates.pending()
    fake_ctx.events.publish(ev.UpdatesFound(tuple(known)))
    _flush(qtbot)
    assert window.toasts.toasts() == []  # already announced at startup (library flags)

    fresh = GameUpdate("celeste", "celeste", "Celeste", "v1.0", "v1.1")
    fake_ctx.events.publish(ev.UpdatesFound((*known, fresh)))
    qtbot.waitUntil(lambda: len(window.toasts.toasts()) == 1, timeout=2000)
    assert "Celeste" in window.toasts.toasts()[0].message

    window.header.updates_chip.click()
    assert window.current_page_key() == "library"
    assert pages["library"].named("set_filter") == [("set_filter", "updates")]


def test_updates_of_hidden_games_are_not_announced(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    hidden_id = "hollow-knight"  # no update flagged at startup
    fake_ctx.library.set_hidden(hidden_id, True)
    qtbot.waitUntil(lambda: window.sidebar.button("library").badge_text() == "4", timeout=2000)
    fake_ctx.events.publish(ev.UpdatesFound((GameUpdate(hidden_id, hidden_id, "Hollow Knight", "v1.0", "v1.2"),)))
    _flush(qtbot)
    assert window.toasts.toasts() == []


def test_update_is_news_again_after_it_was_installed(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    update_id = next(g.install_id for g in fake_ctx.library.games() if g.update_available)
    game = fake_ctx.library.get(update_id)
    fake_ctx.events.publish(ev.UpdatesFound(tuple(fake_ctx.updates.pending())))
    _flush(qtbot)
    assert window.toasts.toasts() == []  # known at startup

    fake_ctx.library.set_update_state(update_id, latest_version=game.latest_version, available=False)  # updated
    qtbot.waitUntil(lambda: window.header.updates_chip.text() == "1 update", timeout=2000)
    fake_ctx.library.set_update_state(update_id, latest_version="v9.0", available=True)  # a newer build
    fake_ctx.events.publish(ev.UpdatesFound((GameUpdate(update_id, game.slug, game.title, game.version, "v9.0"),)))
    qtbot.waitUntil(lambda: any(game.title in t.message for t in window.toasts.toasts()), timeout=2000)


def test_mark_updates_known_after_a_late_library_load(qtbot: Any, fake_ctx: Any, theme: ThemeManager,
                                                      pages: dict, quits: list[int],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    real_games = fake_ctx.library.games
    loaded: list[bool] = []
    monkeypatch.setattr(fake_ctx.library, "games",
                        lambda *, include_hidden=True: real_games(include_hidden=include_hidden) if loaded else [])
    win, bridge = _make_window(fake_ctx, theme)  # constructed before the startup scan: nothing known yet
    try:
        loaded.append(True)
        win.mark_updates_known()
        fake_ctx.events.publish(ev.UpdatesFound(tuple(fake_ctx.updates.pending())))
        _flush(qtbot)
        assert win.toasts.toasts() == []
    finally:
        win.shutdown()
        bridge.close()
        _dispose(win)


def test_game_launch_and_exit_update_status(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    fake_ctx.launcher.launch("hollow-knight")
    qtbot.waitUntil(lambda: window.status_strip.running_text() == "Playing Hollow Knight", timeout=2000)
    fake_ctx.launcher.stop("hollow-knight")
    qtbot.waitUntil(lambda: window.status_strip.running_text() == "", timeout=2000)


def test_minimize_on_game_launch(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    fake_ctx.settings.update(minimize_on_game_launch=True)
    window.show()
    qtbot.waitExposed(window)
    fake_ctx.events.publish(ev.GameLaunched("hollow-knight", "Hollow Knight"))
    qtbot.waitUntil(window.isMinimized, timeout=2000)


def test_theme_setting_change_applies_theme_and_wallpaper(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                                          theme: ThemeManager) -> None:
    fake_ctx.settings.update(theme="vaporwave")
    qtbot.waitUntil(lambda: theme.current_key == "vaporwave", timeout=2000)
    assert window.surface.wallpaper == "vaporwave.gif"
    assert window.surface.is_animated
    fake_ctx.settings.update(theme="daylight")
    qtbot.waitUntil(lambda: theme.current_key == "daylight", timeout=2000)
    assert window.surface.wallpaper == ""
    theme.apply("midnight")


def test_wallpaper_pauses_while_hidden(qtbot: Any, window: MainWindow, theme: ThemeManager) -> None:
    theme.apply("vaporwave")
    try:
        window.show()
        qtbot.waitExposed(window)
        assert window.surface.is_playing
        window.hide()
        assert not window.surface.is_playing
    finally:
        theme.apply("midnight")


# --- account ------------------------------------------------------------------------------------------------


def test_account_chip_signed_out_opens_login(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.ui.dialogs import login

    opened: list[bool] = []

    class FakeLogin(QDialog):
        def __init__(self, ctx: Any, parent: Any = None) -> None:
            super().__init__(parent)
            opened.append(True)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(login, "LoginDialog", FakeLogin)
    window.sidebar.account.click()
    assert opened == [True]


def test_login_dialog_failure_becomes_toast(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.ui.dialogs import login

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(login, "LoginDialog", broken)
    window.request_login()
    assert window.toasts.toasts()[-1].level == "error"


def test_auth_change_updates_chip_and_menu_signs_out(qtbot: Any, window: MainWindow, fake_ctx: Any) -> None:
    fake_ctx.auth.login("jane.doe@example.com", "password")
    qtbot.waitUntil(lambda: window.sidebar.account.user is not None, timeout=2000)
    assert window.sidebar.account.avatar.text() == "JD"  # "jane.doe"
    menu = window.account_menu(fake_ctx.auth.user)
    texts = [a.text() for a in menu.actions()]
    assert "Open profile on website" in texts and "Sign out" in texts and "Account settings" in texts
    menu.deleteLater()
    window.sign_out()
    qtbot.waitUntil(lambda: window.sidebar.account.user is None, timeout=2000)


def test_account_menu_settings_entry(window: MainWindow, pages: dict) -> None:
    menu = window.account_menu(UserInfo(display_name="Jane Doe"))
    next(a for a in menu.actions() if a.text() == "Account settings").trigger()
    assert window.current_page_key() == "settings"
    assert pages["settings"].named("show_section") == [("show_section", "account")]
    menu.deleteLater()


# --- install flow -------------------------------------------------------------------------------------------


class _FakeInstallDialog(QDialog):
    result_code = QDialog.DialogCode.Accepted
    instances: list[_FakeInstallDialog] = []

    def __init__(self, ctx: Any, details: Any, option: Any = None, parent: Any = None, **kwargs: Any) -> None:
        super().__init__(parent)
        self.details = details
        self.option = option or details.primary_option
        self.kwargs = kwargs
        _FakeInstallDialog.instances.append(self)

    def exec(self) -> int:
        return int(self.result_code.value)

    def selected_option(self) -> DownloadOption:
        return self.option

    def selected_library(self) -> str:
        return "D:/Games"


@pytest.fixture
def fake_install_dialog(monkeypatch: pytest.MonkeyPatch) -> type[_FakeInstallDialog]:
    from anker_client.ui.dialogs import install_dialog

    _FakeInstallDialog.instances = []
    _FakeInstallDialog.result_code = QDialog.DialogCode.Accepted
    monkeypatch.setattr(install_dialog, "InstallDialog", _FakeInstallDialog)
    return _FakeInstallDialog


def test_request_install_enqueues_and_toasts(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                             fake_install_dialog: type[_FakeInstallDialog]) -> None:
    details = sample_details(fake_ctx.client.games[20])  # Celeste: not installed, not queued
    window.request_install(details)
    qtbot.waitUntil(lambda: any(j.slug == details.slug for j in fake_ctx.downloads.jobs()), timeout=2000)
    job = next(j for j in fake_ctx.downloads.jobs() if j.slug == details.slug)
    assert job.library_root == "D:/Games"
    assert job.option.kind is DownloadKind.FULL
    assert fake_install_dialog.instances[0].kwargs.get("loader") is not None
    qtbot.waitUntil(lambda: any(t.title == "Added to downloads" for t in window.toasts.toasts()), timeout=2000)
    toast = next(t for t in window.toasts.toasts() if t.title == "Added to downloads")
    assert toast.action_button is not None and toast.action_button.text() == "View"
    toast.action_button.click()
    assert window.current_page_key() == "downloads"


def test_request_install_patch_uses_target_version(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                                   fake_install_dialog: type[_FakeInstallDialog]) -> None:
    details = sample_details(fake_ctx.client.games[20])
    patch = next(o for o in details.download_options if o.kind is DownloadKind.PATCH)
    seen: list[dict[str, Any]] = []
    original = fake_ctx.downloads.enqueue

    def spy(**kwargs: Any) -> DownloadJob:
        seen.append(kwargs)
        return original(**kwargs)

    fake_ctx.downloads.enqueue = spy
    window.request_install(details, patch)
    qtbot.waitUntil(lambda: bool(seen), timeout=2000)
    assert seen[0]["version"] == "1.1.0"
    assert seen[0]["option"] == patch


def test_request_install_existing_job_says_so(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                              fake_install_dialog: type[_FakeInstallDialog]) -> None:
    details = sample_details(fake_ctx.client.games[3])  # already queued in FakeDownloads
    window.request_install(details)
    qtbot.waitUntil(lambda: any("already in your downloads" in t.message for t in window.toasts.toasts()),
                    timeout=2000)


def test_request_install_cancelled_does_nothing(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                                fake_install_dialog: type[_FakeInstallDialog]) -> None:
    fake_install_dialog.result_code = QDialog.DialogCode.Rejected
    before = len(fake_ctx.downloads.jobs())
    window.request_install(sample_details(fake_ctx.client.games[20]))
    _flush(qtbot)
    assert len(fake_ctx.downloads.jobs()) == before
    assert window.toasts.toasts() == []


def test_request_install_without_options_warns(window: MainWindow, fake_ctx: Any,
                                               fake_install_dialog: type[_FakeInstallDialog]) -> None:
    details = sample_details(fake_ctx.client.games[20])
    details.download_options = []
    window.request_install(details)
    assert fake_install_dialog.instances == []
    assert window.toasts.toasts()[-1].level == "warning"


def test_request_install_enqueue_error_toasts(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                              fake_install_dialog: type[_FakeInstallDialog]) -> None:
    def failing(**_kwargs: Any) -> DownloadJob:
        raise LaunchError("Disk is on fire")

    fake_ctx.downloads.enqueue = failing
    window.request_install(sample_details(fake_ctx.client.games[20]))
    qtbot.waitUntil(lambda: any(t.level == "error" for t in window.toasts.toasts()), timeout=2000)
    assert window.toasts.toasts()[-1].message == "Disk is on fire"


# --- tray -----------------------------------------------------------------------------------------------------


def test_tray_launch_without_executable_opens_picker(qtbot: Any, window: MainWindow,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    chosen: list[str] = []
    monkeypatch.setattr(window, "choose_executable", chosen.append)
    window.tray.launch_failed.emit("local:x", ExecutableNotSetError())
    assert chosen == ["local:x"]


def test_tray_launch_other_error_toasts(window: MainWindow) -> None:
    window.tray.launch_failed.emit("hollow-knight", LaunchError("Missing file"))
    toast = window.toasts.toasts()[-1]
    assert toast.level == "error" and toast.title == "Hollow Knight could not start"


def test_tray_menu_recent_games_and_quit(window: MainWindow, quits: list[int],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    window.tray.refresh_recent()
    titles = window.tray.recent_titles()
    assert titles[0] == "Hollow Knight"  # most recently played first, unplayed/exe-less games excluded
    assert "Stardew Valley" not in titles
    assert window.tray.pause_action.isEnabled() and window.tray.resume_action.isEnabled()
    monkeypatch.setattr(shell_dialogs, "confirm_quit_with_downloads", lambda *_a: True)
    window.tray.quit_action.trigger()
    assert quits == [1]


# --- close behaviour --------------------------------------------------------------------------------------


def test_close_ask_remember_tray(qtbot: Any, fake_ctx: Any, theme: ThemeManager, pages: dict,
                                 quits: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    win, bridge = _make_window(fake_ctx, theme, tray_available=True)
    try:
        monkeypatch.setattr(shell_dialogs, "ask_close_action",
                            lambda *_a, **_k: shell_dialogs.CloseDecision(shell_dialogs.CloseChoice.TRAY, True))
        win.show()
        qtbot.waitExposed(win)
        win.close()
        assert not win.isVisible()
        assert fake_ctx.settings.get().close_behavior == "tray"
        assert fake_ctx.settings.get().window_geometry  # saved when hiding
        assert quits == []
        assert win.tray.last_message is not None and "still running" in win.tray.last_message[0]
        win.handle_instance_message("show")
        assert win.isVisible()
    finally:
        win.shutdown()
        bridge.close()
        _dispose(win)


def test_close_tray_without_tray_minimizes(qtbot: Any, window: MainWindow, fake_ctx: Any,
                                           quits: list[int]) -> None:
    fake_ctx.settings.update(close_behavior="tray")
    window.show()
    qtbot.waitExposed(window)
    window.close()
    qtbot.waitUntil(window.isMinimized, timeout=2000)
    assert quits == []


def test_close_ask_cancel_keeps_window(qtbot: Any, window: MainWindow, quits: list[int],
                                       monkeypatch: pytest.MonkeyPatch, fake_ctx: Any) -> None:
    monkeypatch.setattr(shell_dialogs, "ask_close_action", lambda *_a, **_k: None)
    window.show()
    qtbot.waitExposed(window)
    window.close()
    assert window.isVisible()
    assert fake_ctx.settings.get().close_behavior == "ask"
    assert quits == []


def test_quit_with_active_downloads_asks(qtbot: Any, window: MainWindow, fake_ctx: Any, quits: list[int],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ctx.settings.update(close_behavior="quit")
    asked: list[int] = []

    def confirm(_parent: Any, count: int) -> bool:
        asked.append(count)
        return False

    monkeypatch.setattr(shell_dialogs, "confirm_quit_with_downloads", confirm)
    window.show()
    qtbot.waitExposed(window)
    window.close()
    assert asked == [4]  # downloading + queued + waiting + extracting (paused jobs are not interrupted)
    assert quits == [] and window.isVisible()

    monkeypatch.setattr(shell_dialogs, "confirm_quit_with_downloads", lambda *_a: True)
    window.close()
    assert quits == [1] and window.quitting
    window.close()  # a close after quitting is accepted without asking again
    assert quits == [1]


def test_quit_without_downloads_does_not_ask(qtbot: Any, window: MainWindow, fake_ctx: Any, quits: list[int],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_a: Any) -> bool:
        raise AssertionError("must not ask")

    monkeypatch.setattr(shell_dialogs, "confirm_quit_with_downloads", never)
    for job in fake_ctx.downloads.jobs():
        if not job.state.is_finished and job.state is not JobState.PAUSED:
            fake_ctx.downloads.cancel(job.id)
    qtbot.waitUntil(lambda: window.download_summary.in_progress == 0, timeout=2000)
    assert window.request_quit()
    assert quits == [1]


# --- window state ---------------------------------------------------------------------------------------


def test_geometry_saved_and_restored(qtbot: Any, fake_ctx: Any, theme: ThemeManager, pages: dict,
                                     quits: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    first, bridge1 = _make_window(fake_ctx, theme)
    first.show()
    qtbot.waitExposed(first)
    first.resize(1234, 777)
    qtbot.wait(50)
    first.save_window_state()
    expected = bytes(first.saveGeometry().data())
    first.shutdown()
    bridge1.close()
    _dispose(first)
    saved = fake_ctx.settings.get().window_geometry
    assert saved and fake_ctx.settings.get().window_state

    # The offscreen screen is 800×600, so Qt clamps the restored size; check what is handed to Qt instead.
    restored: list[bytes] = []
    original = MainWindow.restoreGeometry

    def spy(self: MainWindow, data: Any) -> bool:
        restored.append(bytes(data.data()))
        return original(self, data)

    monkeypatch.setattr(MainWindow, "restoreGeometry", spy)
    second, bridge2 = _make_window(fake_ctx, theme)
    assert restored == [expected]
    second.shutdown()
    bridge2.close()
    _dispose(second)

    third, bridge3 = _make_window(fake_ctx, theme, reset_window=True)
    assert fake_ctx.settings.get().window_geometry == ""
    assert (third.width(), third.height()) == (1280, 800)
    third.shutdown()
    bridge3.close()
    _dispose(third)


def test_never_shown_window_keeps_saved_geometry(fake_ctx: Any, window: MainWindow) -> None:
    fake_ctx.settings.update(window_geometry="AAAA")
    window.save_window_state()
    assert fake_ctx.settings.get().window_geometry == "AAAA"


def test_shutdown_calls_page_shutdown_once(window: MainWindow, pages: dict) -> None:
    window.show_downloads()
    window.shutdown()
    window.shutdown()
    assert len(pages["downloads"].named("shutdown")) == 1
    assert len(pages["store"].named("shutdown")) == 1


# --- broken pages -----------------------------------------------------------------------------------------


def test_broken_page_shows_placeholder_and_retry(qtbot: Any, fake_ctx: Any, theme: ThemeManager, pages: dict,
                                                 quits: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    import anker_client.ui.pages.downloads as downloads_mod

    working = downloads_mod.DownloadsPage

    def broken(*_args: Any, **_kwargs: Any) -> QWidget:
        raise NotImplementedError("downloads page is not ready")

    monkeypatch.setattr(downloads_mod, "DownloadsPage", broken)
    win, bridge = _make_window(fake_ctx, theme)
    try:
        win.show_downloads()
        placeholder = win.page("downloads")
        assert isinstance(placeholder, PagePlaceholder)
        assert placeholder.title_label.text() == "This page failed to load"
        assert "downloads page is not ready" in placeholder.details_text
        win.show_library()  # other pages keep working
        assert win.current_page_key() == "library"
        win.back()
        monkeypatch.setattr(downloads_mod, "DownloadsPage", working)
        placeholder.retry_button.click()
        assert isinstance(win.page("downloads"), RecordingPage)
        assert win.current_page_key() == "downloads"
    finally:
        win.shutdown()
        bridge.close()
        _dispose(win)


def test_failing_page_hooks_are_contained(window: MainWindow, pages: dict) -> None:
    def explode(*_a: Any) -> None:
        raise NotImplementedError

    pages["store"].set_query = explode  # type: ignore[method-assign]
    window.show_store(query="anything")  # must not raise
    assert window.current_page_key() == "store"


def test_location_equality_ignores_summary() -> None:
    from anker_client.core.models import GameSummary

    assert Location("game", slug="a", summary=GameSummary("a", "A")) == Location("game", slug="a")


# --- screenshots ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["midnight", "daylight", "vaporwave", "bliss_xp"])
def test_screenshot_main_window(qtbot: Any, fake_ctx: Any, theme: ThemeManager, pages: dict, quits: list[int],
                                theme_key: str) -> None:
    theme.apply(theme_key)
    win, bridge = _make_window(fake_ctx, theme)
    try:
        page = pages["store"]
        from anker_client.ui.widgets.common import label, vbox

        page.setLayout(vbox(label("Discover", "display"),
                            label("Store page placeholder — the real page is provided by the store package.",
                                  "muted", wrap=True),
                            None, margins=(24, 8, 24, 24)))
        fake_ctx.events.publish(ev.CatalogSyncProgress(3, 37))
        fake_ctx.auth.login("jane.doe@example.com", "password")
        win.toast("Celeste · Direct", "success", title="Added to downloads", action_text="View")
        win.toast("Could not reach AnkerGames. Check your internet connection.", "error")
        path = screenshot(win, f"shell_main_window_{theme_key}")
        assert path.exists()
    finally:
        win.shutdown()
        bridge.close()
        _dispose(win)
        theme.apply("midnight")
