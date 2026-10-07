"""LibraryPage against FakeContext: views, filters, selection, actions and live updates."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from anker_client.core import events as ev
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.game_dialogs import GamePropertiesDialog, ImportArchiveDialog, ImportFoldersDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.pages import library as library_module
from anker_client.ui.pages.library import (
    _STACK_EMPTY,
    _STACK_GRID,
    _STACK_NO_RESULTS,
    _STACK_TABLE,
    META_FILTER,
    LibraryPage,
)
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.library_model import COL_PLAYTIME, INSTALL_ID_ROLE

pytestmark = pytest.mark.gui


class RecordingNav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    def show_store(self, *, query: str = "", genre: str = "") -> None:
        self._rec("show_store", query=query, genre=genre)

    def show_game(self, slug: str, summary: Any = None) -> None:
        self._rec("show_game", slug)

    def show_library(self, install_id: str = "") -> None:
        self._rec("show_library", install_id)

    def show_downloads(self) -> None:
        self._rec("show_downloads")

    def show_settings(self, section: str = "") -> None:
        self._rec("show_settings", section)

    def back(self) -> None:
        self._rec("back")

    def request_install(self, details: Any, option: Any = None) -> None:
        self._rec("request_install", details, option)

    def request_login(self) -> None:
        self._rec("request_login")

    def choose_executable(self, install_id: str) -> None:
        self._rec("choose_executable", install_id)

    def toast(self, message: str, level: str = "info") -> None:
        self._rec("toast", message, level)

    def toasts(self) -> list[str]:
        return [a[0] for a, _k in self.named("toast")]


#: Bridges/loaders are kept alive for the whole session: worker threads of a finished test may
#: still be emitting into them, and destroying a QObject while another thread emits is a crash.
_KEEP_ALIVE: list[Any] = []


def quiesce(ctx: Any) -> None:
    """Close open dialogs, stop every background thread of ``ctx`` and deliver what they already queued."""
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.isVisible():
            widget.reject()
    ctx.downloads.shutdown()
    thread = getattr(ctx.downloads, "_thread", None)
    if thread is not None and thread.is_alive():
        thread.join(2)
    ctx.runner.shutdown(wait=True)
    QApplication.processEvents()


class NoImages:
    """Image cache that never has anything (keeps tests fast; covers paint as placeholders)."""

    def fetch(self, url: str, *, token: Any = None) -> Any:
        raise FileNotFoundError(url)


@pytest.fixture(scope="module", autouse=True)
def _theme(qapp):
    ThemeManager(qapp).apply("midnight")


@pytest.fixture
def env(qtbot, fake_ctx):
    bridge = QtEventBridge(fake_ctx.events)
    loader = ImageLoader(NoImages(), fake_ctx.runner)
    nav = RecordingNav()
    pages: list[LibraryPage] = []

    def make() -> LibraryPage:
        page = LibraryPage(fake_ctx, bridge, nav, loader)
        qtbot.addWidget(page)
        page.resize(1280, 800)
        page.show()
        pages.append(page)
        qtbot.waitUntil(lambda: page._loaded, timeout=3000)
        return page

    yield SimpleNamespace(ctx=fake_ctx, bridge=bridge, loader=loader, nav=nav, make=make, qtbot=qtbot)
    for page in pages:
        page.shutdown()
        page.close()
    quiesce(fake_ctx)
    bridge.close()
    _KEEP_ALIVE.extend((bridge, loader))


def grid_keys(page: LibraryPage) -> list[str]:
    return [item.key for item in page._grid.grid_model.items()]


def grid_item(page: LibraryPage, key: str):
    return page._grid.grid_model.item(key)


def click_action(page: LibraryPage, action: str) -> None:
    btn = page._panel.action_button(action)
    assert btn is not None and btn.isVisible() and btn.isEnabled(), action
    btn.click()


# --- loading & views --------------------------------------------------------------------------


def test_loads_games_into_grid_and_selects_first(env):
    page = env.make()
    assert page._stack.currentIndex() == _STACK_GRID
    assert grid_keys(page) == ["elden-ring", "hades-ii", "hollow-knight", "minecraft", "local:roadhouse simulator"]
    assert page._count.text() == "5 games"
    assert page._panel.install_id == "elden-ring"
    assert page._panel.isVisible()
    assert page._updates_chip.isVisible() and page._updates_chip.text() == "2 updates available"


def test_grid_badges_and_favorite(env):
    page = env.make()
    assert [b.text for b in grid_item(page, "hades-ii").badges] == ["Update", "Needs setup"]
    assert [(b.text, b.kind) for b in grid_item(page, "local:roadhouse simulator").badges] == [("Unmanaged", "")]
    assert grid_item(page, "minecraft").favorite
    assert grid_item(page, "hollow-knight").badges == ()


def test_toggle_list_view_persists_setting(env):
    page = env.make()
    page._list_toggle.click()
    assert page._stack.currentIndex() == _STACK_TABLE
    assert page._table_model.rowCount() == 5
    env.qtbot.waitUntil(lambda: env.ctx.settings.get().library_view == "list", timeout=2000)
    # the selection follows into the table
    assert page._table.currentIndex().data(INSTALL_ID_ROLE) == page._panel.install_id
    page._grid_toggle.click()
    assert page._stack.currentIndex() == _STACK_GRID
    env.qtbot.waitUntil(lambda: env.ctx.settings.get().library_view == "grid", timeout=2000)


def test_view_and_sort_restored_from_settings(env):
    env.ctx.settings.update(library_view="list", library_sort="playtime")
    page = env.make()
    assert page._stack.currentIndex() == _STACK_TABLE
    assert page._sort_combo.currentData() == "playtime"
    assert grid_keys(page)[0] == "minecraft"


def test_filter_text_and_combo(env):
    page = env.make()
    page._search.setText("hollow")
    assert grid_keys(page) == ["hollow-knight"]
    assert page._count.text() == "1 of 5 games"
    page._search.clear()
    for key, expected in (("updates", ["elden-ring", "hades-ii"]), ("needs_setup", ["hades-ii"]),
                          ("unmanaged", ["local:roadhouse simulator"]), ("favorites", ["minecraft"])):
        page._filter_combo.setCurrentIndex(page._filter_combo.findData(key))
        assert grid_keys(page) == expected, key
    assert page._filter_combo.itemText(page._filter_combo.findData("updates")) == "Updates available (2)"
    env.qtbot.waitUntil(lambda: env.ctx.db.get_meta(META_FILTER) == "favorites", timeout=3000)


def test_filters_restored_from_database(env):
    env.ctx.db.set_meta(META_FILTER, "updates")
    env.ctx.db.set_meta(library_module.META_QUERY, "elden")
    page = env.make()
    assert page._filter_combo.currentData() == "updates"
    assert page._search.text() == "elden"
    assert grid_keys(page) == ["elden-ring"]


def test_no_results_and_clear_filters(env):
    page = env.make()
    page._search.setText("zzz")
    assert page._stack.currentIndex() == _STACK_NO_RESULTS
    assert not page._panel.isVisible()
    page._no_results.action_clicked.emit()
    assert page._search.text() == ""
    assert page._stack.currentIndex() == _STACK_GRID
    assert len(grid_keys(page)) == 5


def test_sort_combo_orders_and_persists(env):
    page = env.make()
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("playtime"))
    assert grid_keys(page) == ["minecraft", "hollow-knight", "elden-ring", "hades-ii", "local:roadhouse simulator"]
    env.qtbot.waitUntil(lambda: env.ctx.settings.get().library_sort == "playtime", timeout=2000)
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("size"))
    sizes = [env.ctx.library.get(k).size_bytes for k in grid_keys(page)]
    assert sizes == sorted(sizes, reverse=True)


def test_table_header_sorting(env):
    page = env.make()
    page._list_toggle.click()
    page._table.sortByColumn(COL_PLAYTIME, Qt.SortOrder.DescendingOrder)
    first = page._proxy.index(0, 0).data(INSTALL_ID_ROLE)
    assert first == "minecraft"
    page._table.sortByColumn(COL_PLAYTIME, Qt.SortOrder.AscendingOrder)
    assert page._proxy.index(0, 0).data(INSTALL_ID_ROLE) == "local:roadhouse simulator"


def test_empty_library_state(env):
    env.ctx.library._games.clear()
    page = env.make()
    assert page._stack.currentIndex() == _STACK_EMPTY
    assert not page._panel.isVisible()
    assert not page._toolbar.isVisible()
    page._empty.action_clicked.emit()
    assert env.nav.named("show_store")
    page._empty_import_button.click()
    assert isinstance(page._dialog, ImportFoldersDialog)
    page._dialog.reject()


def test_load_error_shows_retry(env, monkeypatch):
    def broken(**_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(env.ctx.library, "games", broken)
    page = LibraryPage(env.ctx, env.bridge, env.nav, env.loader)
    env.qtbot.addWidget(page)
    env.qtbot.waitUntil(lambda: page._stack.currentIndex() == library_module._STACK_ERROR, timeout=3000)
    monkeypatch.undo()
    page._error_state.action_clicked.emit()
    env.qtbot.waitUntil(lambda: page._stack.currentIndex() == _STACK_GRID, timeout=3000)
    page.shutdown()


# --- selection & detail panel ----------------------------------------------------------------------


def test_select_shows_detail_and_computes_unknown_size(env, monkeypatch):
    env.ctx.library._games["hollow-knight"].size_bytes = None
    monkeypatch.setattr(env.ctx.library, "compute_size", lambda install_id, *, token=None: 3 * 1024**3)
    page = env.make()
    page.select("hollow-knight")
    assert page._panel.install_id == "hollow-knight"
    assert page._grid.current_key() == "hollow-knight"
    assert page._panel.title.text() == "Hollow Knight"
    env.qtbot.waitUntil(lambda: page._panel.stat_size.value.text() == "3.00 GB", timeout=3000)
    assert page._panel.stat_playtime.value.text() == "41.0 hours"


def test_select_clears_filters_that_hide_the_game(env):
    page = env.make()
    page._filter_combo.setCurrentIndex(page._filter_combo.findData("favorites"))
    page.select("hollow-knight")
    assert page._filter_combo.currentData() == "all"
    assert page._panel.install_id == "hollow-knight"


def test_select_before_load_is_applied_later(env, qtbot):
    page = LibraryPage(env.ctx, env.bridge, env.nav, env.loader)
    qtbot.addWidget(page)
    page.select("minecraft")
    qtbot.waitUntil(lambda: page._panel.install_id == "minecraft", timeout=3000)
    page.shutdown()


def test_clicking_grid_item_selects_it(env):
    page = env.make()
    row = page._grid.grid_model.row_of("minecraft")
    page._grid.setCurrentIndex(page._grid.grid_model.index(row))
    assert page._panel.install_id == "minecraft"


# --- play / stop -----------------------------------------------------------------------------------


def test_play_and_stop_follow_running_state(env):
    page = env.make()
    page.select("hollow-knight")
    click_action(page, "play")
    env.qtbot.waitUntil(lambda: "hollow-knight" in page._running, timeout=3000)
    assert page._panel.play_button.text() == "Stop"
    assert grid_item(page, "hollow-knight").badges[0].text == "Running"
    assert not page._panel.uninstall_button.isEnabled()
    click_action(page, "stop")
    env.qtbot.waitUntil(lambda: "hollow-knight" not in page._running, timeout=3000)
    assert page._panel.play_button.text() == "Play"
    assert env.ctx.library.get("hollow-knight").playtime_seconds == 3600 * 41 + 60


def test_play_without_executable_asks_for_one(env):
    page = env.make()
    page.select("hades-ii")
    assert page._panel._setup_box.isVisible()
    click_action(page, "play")
    env.qtbot.waitUntil(lambda: bool(env.nav.named("choose_executable")), timeout=3000)
    assert env.nav.named("choose_executable")[0][0] == ("hades-ii",)
    page._panel.setup_button.click()
    assert len(env.nav.named("choose_executable")) == 2


def test_double_click_plays(env):
    page = env.make()
    index = page._grid.grid_model.index(page._grid.grid_model.row_of("minecraft"))
    page._grid.doubleClicked.emit(index)
    env.qtbot.waitUntil(lambda: env.ctx.launcher.is_running("minecraft"), timeout=3000)


def test_launch_error_is_toasted(env, monkeypatch):
    from anker_client.core.errors import LaunchError

    def fail(install_id):
        raise LaunchError("The game's program is missing.")

    monkeypatch.setattr(env.ctx.launcher, "launch", fail)
    page = env.make()
    page.select("minecraft")
    click_action(page, "play")
    env.qtbot.waitUntil(lambda: bool(env.nav.toasts()), timeout=3000)
    assert "program is missing" in env.nav.toasts()[0]


def test_external_launch_event_updates_badges(env):
    page = env.make()
    env.ctx.events.publish(ev.GameLaunched("minecraft", "Minecraft"))
    env.qtbot.waitUntil(lambda: "minecraft" in page._running, timeout=2000)
    assert grid_item(page, "minecraft").badges[0].text == "Running"
    env.ctx.events.publish(ev.GameExited("minecraft", "Minecraft", 30))
    env.qtbot.waitUntil(lambda: "minecraft" not in page._running, timeout=2000)


# --- uninstall ----------------------------------------------------------------------------------------


def _patch_messagebox(monkeypatch, *, accept: bool) -> dict[str, str]:
    seen: dict[str, str] = {}

    def fake_exec(box: QMessageBox) -> int:
        seen["text"] = box.text()
        seen["info"] = box.informativeText()
        if accept:
            for btn in box.buttons():
                if box.buttonRole(btn) == QMessageBox.ButtonRole.DestructiveRole:
                    seen["button"] = btn.text()
                    btn.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    return seen


def test_uninstall_confirmed(env, monkeypatch):
    seen = _patch_messagebox(monkeypatch, accept=True)
    page = env.make()
    page.select("hollow-knight")
    click_action(page, "uninstall")
    assert seen["text"] == "Uninstall Hollow Knight?"
    assert "205 MB" in seen["info"] and "Hollow Knight" in seen["info"]
    assert seen["button"] == "Uninstall Hollow Knight"
    # busy state while the worker deletes the folder
    assert not page._panel.uninstall_button.isEnabled()
    assert page._panel.uninstall_button.text() == "Uninstalling…"
    env.qtbot.waitUntil(lambda: "hollow-knight" not in page._games, timeout=3000)
    assert "hollow-knight" not in grid_keys(page)
    assert any("was uninstalled" in t for t in env.nav.toasts())
    assert page._panel.install_id != "hollow-knight"


def test_uninstall_cancelled_keeps_game(env, monkeypatch):
    seen = _patch_messagebox(monkeypatch, accept=False)
    page = env.make()
    page.select("minecraft")
    click_action(page, "uninstall")
    assert "Minecraft" in seen["text"]
    env.qtbot.wait(300)
    assert env.ctx.library.get("minecraft") is not None
    assert page._panel.uninstall_button.isEnabled()


def test_uninstall_refused_while_running(env, monkeypatch):
    seen = _patch_messagebox(monkeypatch, accept=True)
    page = env.make()
    page.select("minecraft")
    env.ctx.events.publish(ev.GameLaunched("minecraft", "Minecraft"))
    env.qtbot.waitUntil(lambda: "minecraft" in page._running, timeout=2000)
    page._uninstall("minecraft")
    assert not seen
    assert "before uninstalling" in env.nav.toasts()[-1]


# --- other actions -------------------------------------------------------------------------------------


def test_favorite_and_hide(env):
    page = env.make()
    page.select("hollow-knight")
    click_action(page, "favorite")
    assert grid_item(page, "hollow-knight").favorite  # applied immediately, saved in the background
    env.qtbot.waitUntil(lambda: env.ctx.library.get("hollow-knight").favorite, timeout=3000)
    click_action(page, "hide")
    env.qtbot.waitUntil(lambda: "hollow-knight" not in grid_keys(page), timeout=3000)
    assert any("Hidden" in t for t in env.nav.toasts())
    page._filter_combo.setCurrentIndex(page._filter_combo.findData("hidden"))
    assert grid_keys(page) == ["hollow-knight"]
    assert grid_item(page, "hollow-knight").dimmed


def test_show_hidden_setting_reveals_hidden_games(env):
    env.ctx.library._games["minecraft"].hidden = True
    page = env.make()
    assert "minecraft" not in grid_keys(page)
    env.ctx.settings.update(show_hidden_games=True)
    env.qtbot.waitUntil(lambda: "minecraft" in grid_keys(page), timeout=2000)
    assert grid_item(page, "minecraft").dimmed


def test_library_change_updates_items_in_place(env):
    page = env.make()
    resets: list[int] = []
    page._grid.grid_model.modelReset.connect(lambda: resets.append(1))
    env.ctx.library.set_executable("hades-ii", "Hades.exe")
    env.qtbot.waitUntil(lambda: grid_item(page, "hades-ii").badges == (grid_item(page, "hades-ii").badges[0],),
                        timeout=3000)
    assert [b.text for b in grid_item(page, "hades-ii").badges] == ["Update"]
    assert not resets


def test_update_requests_install_with_update_option(env):
    page = env.make()
    page.select("elden-ring")
    assert page._panel.update_button.text() == "Update to v1.1.0"
    click_action(page, "update")
    env.qtbot.waitUntil(lambda: bool(env.nav.named("request_install")), timeout=3000)
    (details, option), _kw = env.nav.named("request_install")[0]
    assert details.slug == "elden-ring"
    assert option.label == "Direct"


def test_repair_requests_full_install(env):
    page = env.make()
    page.select("minecraft")
    click_action(page, "repair")
    env.qtbot.waitUntil(lambda: bool(env.nav.named("request_install")), timeout=3000)
    (details, option), _kw = env.nav.named("request_install")[0]
    assert details.slug == "minecraft" and option == details.primary_option


def test_store_page_and_unmanaged_actions(env):
    page = env.make()
    page.select("minecraft")
    click_action(page, "store")
    assert env.nav.named("show_game")[0][0] == ("minecraft",)
    page.select("local:roadhouse simulator")
    assert not page._panel.action_button("store").isVisible()
    assert not page._panel.action_button("repair").isVisible()


def test_install_prerequisites_only_when_needed(env):
    page = env.make()
    page.select("elden-ring")  # has_redist and not installed
    click_action(page, "redist")
    env.qtbot.waitUntil(lambda: env.ctx.library.get("elden-ring").redist_installed, timeout=3000)
    env.qtbot.waitUntil(lambda: not page._panel.action_button("redist").isVisible(), timeout=3000)
    assert any("prerequisite" in t for t in env.nav.toasts())
    page.select("minecraft")
    assert not page._panel.action_button("redist").isVisible()


def test_check_for_updates(env):
    page = env.make()
    page._check_button.click()
    assert page._check_button.text() == "Checking…"
    env.qtbot.waitUntil(lambda: any("2 updates available" in t for t in env.nav.toasts()), timeout=3000)
    env.qtbot.waitUntil(lambda: page._check_button.text() == "Check for updates", timeout=2000)


def test_check_single_game_update(env):
    page = env.make()
    page.select("minecraft")
    click_action(page, "check_update")
    env.qtbot.waitUntil(lambda: any("up to date" in t for t in env.nav.toasts()), timeout=3000)


def test_create_shortcuts(env):
    created: list[tuple[Any, ...]] = []

    class Shortcuts:
        def create(self, title, target_exe, *, arguments="", desktop=True, start_menu=True):
            created.append((title, target_exe, arguments, desktop, start_menu))
            return ["a.lnk"]

    env.ctx.shortcuts = Shortcuts()
    page = env.make()
    page.select("minecraft")
    click_action(page, "shortcuts")
    env.qtbot.waitUntil(lambda: bool(created), timeout=3000)
    title, target, _args, desktop, start_menu = created[0]
    assert title == "Minecraft" and target.endswith("Minecraft.exe") and desktop and start_menu
    env.qtbot.waitUntil(lambda: any("Start menu" in t for t in env.nav.toasts()), timeout=2000)


def test_context_menu_and_properties_dialog(env):
    page = env.make()
    menu = page._build_context_menu("hollow-knight")
    texts = [a.text() for a in menu.actions() if a.text()]
    assert texts[:3] == ["Play", "Properties…", "Open folder"]
    assert "Uninstall…" in texts
    properties = next(a for a in menu.actions() if a.data() == "properties")
    properties.trigger()
    assert isinstance(page._dialog, GamePropertiesDialog)
    page._dialog.reject()
    menu.close()


def test_add_games_menu(env):
    page = env.make()
    page._act_import_archive.trigger()
    assert isinstance(page._dialog, ImportArchiveDialog)
    page._dialog.reject()
    page._act_rescan.trigger()
    env.qtbot.waitUntil(lambda: any("rescanned" in t for t in env.nav.toasts()), timeout=3000)


def test_updates_chip_filters_updates(env):
    page = env.make()
    page._updates_chip.click()
    assert page._filter_combo.currentData() == "updates"
    assert not page._updates_chip.isVisible()


def test_enter_plays_and_delete_asks_to_uninstall(env, monkeypatch):
    seen = _patch_messagebox(monkeypatch, accept=False)
    page = env.make()
    page.select("minecraft")
    page._grid.setFocus()
    env.qtbot.keyClick(page._grid, Qt.Key.Key_Return)
    env.qtbot.waitUntil(lambda: env.ctx.launcher.is_running("minecraft"), timeout=3000)
    env.ctx.launcher.stop("minecraft")
    env.qtbot.waitUntil(lambda: "minecraft" not in page._running, timeout=3000)
    env.qtbot.keyClick(page._grid, Qt.Key.Key_Delete)
    assert seen["text"] == "Uninstall Minecraft?"


def test_icons_follow_theme_changes(env, qapp):
    page = env.make()
    before = page._check_button.icon().cacheKey()
    try:
        ThemeManager(qapp).apply("daylight")
        env.qtbot.waitUntil(lambda: page._check_button.icon().cacheKey() != before, timeout=2000)
    finally:
        ThemeManager(qapp).apply("midnight")


def test_shutdown_stops_listening(env):
    page = env.make()
    page.shutdown()
    seq = page._load_seq
    env.ctx.library.set_favorite("minecraft", False)
    env.qtbot.wait(250)
    assert page._load_seq == seq
    assert not page._reload_timer.isActive()


# --- review regressions ------------------------------------------------------------------------------


def test_set_filter_from_shell(env):
    page = env.make()
    page.set_filter("updates")  # MainWindow.show_updates → "N updates" chip / toast action
    assert page._filter_combo.currentData() == "updates"
    assert grid_keys(page) == ["elden-ring", "hades-ii"]
    page.set_filter("bogus")
    assert page._filter_combo.currentData() == "all"
    assert len(grid_keys(page)) == 5


def test_set_filter_before_first_load_beats_saved_filter(env, qtbot):
    env.ctx.db.set_meta(META_FILTER, "favorites")
    page = LibraryPage(env.ctx, env.bridge, env.nav, env.loader)
    qtbot.addWidget(page)
    page.set_filter("updates")
    qtbot.waitUntil(lambda: page._loaded, timeout=3000)
    assert page._filter_combo.currentData() == "updates"
    assert grid_keys(page) == ["elden-ring", "hades-ii"]
    page.shutdown()


def test_size_is_measured_again_after_the_selection_moved_on(env, monkeypatch):
    for key in ("hollow-knight", "minecraft"):
        env.ctx.library._games[key].size_bytes = None
    calls: list[str] = []

    def compute(install_id, *, token=None):
        calls.append(install_id)
        if len(calls) == 1:  # the first measurement is abandoned when another game is selected
            token.wait(5)
            token.raise_if_cancelled()
        return 2 * 1024**3

    monkeypatch.setattr(env.ctx.library, "compute_size", compute)
    page = env.make()
    page.select("hollow-knight")
    env.qtbot.waitUntil(lambda: calls == ["hollow-knight"], timeout=2000)
    assert page._panel.stat_size.value.text() == "Calculating…"
    page.select("minecraft")
    env.qtbot.waitUntil(lambda: page._panel.stat_size.value.text() == "2.00 GB", timeout=3000)
    page.select("hollow-knight")
    env.qtbot.waitUntil(lambda: page._panel.stat_size.value.text() == "2.00 GB", timeout=3000)
    assert calls == ["hollow-knight", "minecraft", "hollow-knight"]


def test_busy_state_survives_reselection_and_blocks_duplicates(env, monkeypatch):
    release = threading.Event()
    runs: list[str] = []

    def run_redist(install_id, *, token=None):
        runs.append(install_id)
        release.wait(5)
        return 1

    monkeypatch.setattr(env.ctx.launcher, "run_redist", run_redist)
    page = env.make()
    try:
        page.select("elden-ring")
        click_action(page, "redist")
        btn = page._panel.action_button("redist")
        assert not btn.isEnabled() and btn.text() == "Installing prerequisites…"
        page.select("minecraft")
        page.select("elden-ring")
        assert not btn.isEnabled() and btn.text() == "Installing prerequisites…"
        page._run_action("redist", "elden-ring")  # e.g. the context menu: must not start a second run
        menu = page._build_context_menu("elden-ring")
        assert not next(a for a in menu.actions() if a.data() == "redist").isEnabled()
        menu.close()
    finally:
        release.set()
    env.qtbot.waitUntil(lambda: not page._panel.is_busy("redist"), timeout=3000)
    assert runs == ["elden-ring"]
    assert page._panel.action_button("redist").isEnabled()


def test_play_twice_quickly_launches_once(env, monkeypatch):
    release = threading.Event()
    launches: list[str] = []

    def launch(install_id):
        launches.append(install_id)
        release.wait(5)

    monkeypatch.setattr(env.ctx.launcher, "launch", launch)
    page = env.make()
    try:
        page.select("minecraft")
        click_action(page, "play")
        assert page._panel.play_button.text() == "Starting…" and not page._panel.play_button.isEnabled()
        page._play("minecraft")  # Enter / double-click while the first launch is still starting
    finally:
        release.set()
    env.qtbot.waitUntil(lambda: not page._panel.is_busy("play"), timeout=3000)
    assert launches == ["minecraft"]


def test_launch_seen_during_a_load_survives_its_older_snapshot(env, monkeypatch):
    page = env.make()
    gate = threading.Event()
    real_games = env.ctx.library.games
    loaded: list[int] = []
    original_on_loaded = page._on_loaded

    def slow_games(**kwargs):
        gate.wait(5)
        return real_games(**kwargs)

    monkeypatch.setattr(env.ctx.library, "games", slow_games)
    monkeypatch.setattr(env.ctx.launcher, "running", set)  # taken before the launch
    monkeypatch.setattr(page, "_on_loaded", lambda seq, result: (original_on_loaded(seq, result), loaded.append(seq)))
    try:
        page._reload()
        env.ctx.events.publish(ev.GameLaunched("minecraft", "Minecraft"))
        env.qtbot.waitUntil(lambda: "minecraft" in page._running, timeout=2000)
    finally:
        gate.set()
    env.qtbot.waitUntil(lambda: bool(loaded), timeout=3000)
    assert "minecraft" in page._running
    assert grid_item(page, "minecraft").badges[0].text == "Running"


def test_uninstall_rechecks_running_after_the_confirmation(env, monkeypatch):
    def fake_exec(box: QMessageBox) -> int:
        env.ctx.events.publish(ev.GameLaunched("minecraft", "Minecraft"))  # started while the box was open
        QApplication.processEvents()
        for btn in box.buttons():
            if box.buttonRole(btn) == QMessageBox.ButtonRole.DestructiveRole:
                btn.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    uninstalls: list[str] = []

    def uninstall(install_id, *, token=None):
        uninstalls.append(install_id)

    monkeypatch.setattr(env.ctx.library, "uninstall", uninstall)
    page = env.make()
    page.select("minecraft")
    click_action(page, "uninstall")
    env.qtbot.wait(200)
    assert uninstalls == []
    assert "before uninstalling" in env.nav.toasts()[-1]


def test_quick_favorite_double_click_ends_unfavorited(env, monkeypatch):
    writes: list[bool] = []
    real_set = env.ctx.library.set_favorite

    def slow_set(install_id, favorite):
        time.sleep(0.05)
        writes.append(favorite)
        real_set(install_id, favorite)

    monkeypatch.setattr(env.ctx.library, "set_favorite", slow_set)
    page = env.make()
    page.select("hollow-knight")
    click_action(page, "favorite")
    assert grid_item(page, "hollow-knight").favorite
    click_action(page, "favorite")
    assert not grid_item(page, "hollow-knight").favorite
    env.qtbot.waitUntil(lambda: writes == [True, False], timeout=3000)  # in click order, one at a time
    env.qtbot.wait(300)  # the reloads triggered by both writes
    assert not env.ctx.library.get("hollow-knight").favorite
    assert not grid_item(page, "hollow-knight").favorite


def test_failed_favorite_save_is_reverted(env, monkeypatch):
    from anker_client.core.errors import InstallError

    def fail(install_id, favorite):
        raise InstallError("Database is read-only")

    monkeypatch.setattr(env.ctx.library, "set_favorite", fail)
    page = env.make()
    page.select("hollow-knight")
    click_action(page, "favorite")
    assert grid_item(page, "hollow-knight").favorite
    env.qtbot.waitUntil(lambda: not grid_item(page, "hollow-knight").favorite, timeout=3000)
    assert any("read-only" in t for t in env.nav.toasts())


def test_unmanaged_folder_with_a_catalog_guess_cannot_be_repaired(env):
    env.ctx.library._games["local:roadhouse simulator"].slug = "roadhouse-simulator"
    page = env.make()
    page.select("local:roadhouse simulator")
    assert page._panel.action_button("store").isVisible()
    assert not page._panel.action_button("repair").isVisible()
    assert not page._panel.action_button("check_update").isVisible()
    menu = page._build_context_menu("local:roadhouse simulator")
    data = [a.data() for a in menu.actions()]
    menu.close()
    assert "store" in data and "repair" not in data and "check_update" not in data
    page._repair("local:roadhouse simulator")
    env.qtbot.wait(100)
    assert not env.nav.named("request_install")


def test_context_menu_offers_the_panel_actions(env):
    page = env.make()

    def actions_for(install_id: str) -> list[str]:
        menu = page._build_context_menu(install_id)
        data = [a.data() for a in menu.actions() if a.data()]
        menu.close()
        return data

    elden = actions_for("elden-ring")
    assert {"play", "update", "properties", "open_folder", "shortcuts", "redist", "favorite", "hide", "store",
            "check_update", "repair", "uninstall"} <= set(elden)
    hades = actions_for("hades-ii")
    assert "choose_exe" in hades and "shortcuts" not in hades
    menu = page._build_context_menu("hades-ii")
    next(a for a in menu.actions() if a.data() == "choose_exe").trigger()
    menu.close()
    assert env.nav.named("choose_executable")[-1][0] == ("hades-ii",)


def test_refresh_rescans_quietly(env, monkeypatch):
    scans: list[int] = []
    real_scan = env.ctx.library.scan

    def scan(*, token=None):
        scans.append(1)
        return real_scan(token=token)

    monkeypatch.setattr(env.ctx.library, "scan", scan)
    page = env.make()
    page.refresh()
    env.qtbot.waitUntil(lambda: bool(scans) and "rescan" not in page._tasks, timeout=3000)
    assert not any("rescanned" in t for t in env.nav.toasts())


def test_shutdown_after_background_tasks_stopped_does_not_raise(env):
    page = env.make()
    page._search.setText("hollow")  # starts the debounced filter save
    assert page._persist_timer.isActive()
    page.hide()  # the window is gone at exit (a repaint would ask the shared image loader for covers)
    env.ctx.runner.shutdown(wait=True)  # application exit: the context went down first
    page.shutdown()
    assert not page._persist_timer.isActive()
