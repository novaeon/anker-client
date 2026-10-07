"""GamePage: immediate render, details fetch, action-card state machine, live job/library/launch
updates, wishlist sync with the Store, carousel + lightbox, responsive layout, error + Retry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from anker_client.core import events as ev
from anker_client.core.errors import ExecutableNotSetError, NetworkError, NotFoundError
from anker_client.core.models import DownloadKind, GameDetails, JobState
from anker_client.ui.pages.game import GamePage
from anker_client.ui.pages.store import StorePage
from anker_client.ui.theme import palette
from anker_client.ui.widgets import game_common
from anker_client.ui.widgets.game_actions import ActionKind
from anker_client.ui.widgets.store_common import ResultsPanel
from tests.gui.test_store_ui_support import UI, game, make_job, ui_session

pytestmark = pytest.mark.gui


@pytest.fixture
def ui(qtbot: Any, fake_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[UI]:
    yield from ui_session(fake_ctx, monkeypatch)


@pytest.fixture
def confirmations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def fake_confirm(_parent: Any, _title: str, text: str, *_args: Any) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr(game_common, "confirm", fake_confirm)
    return asked


def make_page(qtbot: Any, ui: UI, size: tuple[int, int] = (1060, 900)) -> GamePage:
    page = GamePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(page)
    page.resize(*size)
    page.show()
    return page


def loaded(qtbot: Any, page: GamePage, slug: str, *, summary: bool = True, ui: UI | None = None) -> None:
    assert ui is not None
    page.load(slug, game(ui.ctx, slug) if summary else None)
    qtbot.waitUntil(lambda: page.details is not None and page.details.slug == slug
                    and page._state_handle is None, timeout=5000)


def spy(monkeypatch: pytest.MonkeyPatch, obj: Any, name: str, *, passthrough: bool = True) -> list[tuple]:
    calls: list[tuple] = []
    original = getattr(obj, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs) if passthrough else None

    monkeypatch.setattr(obj, name, wrapper)
    return calls


# --- loading ----------------------------------------------------------------------------------


def test_renders_summary_immediately_then_details(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    summary = game(ui.ctx, "red-dead-redemption-2")
    page.load(summary.slug, summary)
    assert page.slug == "red-dead-redemption-2"
    assert page.hero.title == "Red Dead Redemption 2"  # straight from the summary, before any request returns
    assert page._stack.currentWidget() is page.scroll
    assert page.action_state.kind is ActionKind.LOADING
    qtbot.waitUntil(lambda: page.details is not None, timeout=5000)
    assert page.hero.genres == page.details.genres
    assert "v1.1.0" in page.hero.meta and "36.70 GB" in page.hero.meta
    assert page.carousel.count() == 4
    assert page.about.text().startswith("Red Dead Redemption 2 is a sample game")
    assert page.requirements.values["Processor"] == "Intel Core i5"
    assert page.facts.value_of("Size") == "36.70 GB"
    assert page.facts.value_of("Torrent") == "Requires sign-in"


def test_loading_overlay_then_stale_results_are_dropped(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    page.load("celeste")
    assert page._stack.currentWidget() is page.loading
    page.load("terraria", game(ui.ctx, "terraria"))  # supersedes "celeste" before it returns
    qtbot.waitUntil(lambda: page.details is not None, timeout=5000)
    qtbot.wait(100)
    assert page.details.slug == "terraria"
    assert page.hero.title == "Terraria"
    assert page._stack.currentWidget() is page.scroll


def test_error_state_and_retry(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ui.ctx.client.game_details

    def broken(slug: str, **_kwargs: Any) -> GameDetails:
        raise NetworkError()

    monkeypatch.setattr(ui.ctx.client, "game_details", broken)
    page = make_page(qtbot, ui)
    page.load("celeste")
    qtbot.waitUntil(lambda: page._stack.currentWidget() is page.error_view, timeout=5000)
    assert page.error_view.title.text() == "Couldn't load this game"
    assert "internet connection" in page.error_view.message.text()
    assert not page.error_view.retry_button.isHidden()

    monkeypatch.setattr(ui.ctx.client, "game_details", original)
    page.error_view.retry_button.click()
    qtbot.waitUntil(lambda: page.details is not None, timeout=5000)
    assert page._stack.currentWidget() is page.scroll
    page.error_view.back_button.click()
    assert ui.nav.named("back")


def test_refresh_failure_with_summary_shows_banner(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ui.ctx.client.game_details

    def broken(slug: str, **_kwargs: Any) -> GameDetails:
        raise NetworkError()

    monkeypatch.setattr(ui.ctx.client, "game_details", broken)
    page = make_page(qtbot, ui)
    page.load("celeste", game(ui.ctx, "celeste"))
    qtbot.waitUntil(page.banner.isVisible, timeout=5000)
    assert page._stack.currentWidget() is page.scroll
    assert page.action_state.kind is ActionKind.UNAVAILABLE
    assert "Couldn't refresh game details" in page.banner.text.text()

    monkeypatch.setattr(ui.ctx.client, "game_details", original)
    page.banner.action.click()
    qtbot.waitUntil(lambda: page.details is not None and not page.banner.isVisible(), timeout=5000)
    assert page.action_state.kind is ActionKind.INSTALL


def test_not_found(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(qtbot, ui)
    page.load("no-such-game")
    qtbot.waitUntil(lambda: page._stack.currentWidget() is page.error_view, timeout=5000)
    assert page.error_view.title.text() == "Game not found"
    assert page.error_view.retry_button.isHidden()

    def gone(slug: str, **_kwargs: Any) -> GameDetails:
        raise NotFoundError()

    monkeypatch.setattr(ui.ctx.client, "game_details", gone)
    page.load("hollow-knight")  # removed from the site but installed: still playable
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.PLAY, timeout=5000)
    qtbot.waitUntil(lambda: page._stack.currentWidget() is page.scroll, timeout=3000)
    assert page.hero.title == "Hollow Knight"
    assert "no longer listed" in page.banner.text.text()


# --- action card state machine ----------------------------------------------------------------------


def test_install_requests_install_with_primary_option(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "red-dead-redemption-2", ui=ui)
    card = page.action_card
    assert page.action_state.kind is ActionKind.INSTALL
    assert card.primary.text() == "Install" and card.primary.isEnabled()
    assert card.caption.text() == "Direct · 36.70 GB"
    card.primary.click()
    details, option = ui.nav.named("request_install")[-1].args
    assert details.slug == "red-dead-redemption-2"
    assert option.kind is DownloadKind.FULL and option.download_id == 1005

    # several options → menu: FULL entries enabled, PATCH/ADDON disabled until installed
    assert not card.menu_button.isHidden()
    entries = {a.text(): a.isEnabled() for a in card.menu().actions() if a.text()}
    assert entries == {"Direct": True, "Update Only From V 1.0.0 To V 1.1.0 (124 MB)": False}


def test_job_progress_and_controls(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch,
                                   confirmations: list[str]) -> None:
    pauses = spy(monkeypatch, ui.ctx.downloads, "pause")
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "gears-of-war-e-day", ui=ui)  # the fakes have a running download for it
    card = page.action_card
    assert page.action_state.kind is ActionKind.JOB
    assert card.job_title.text() == "Downloading 37%"
    assert card.progress.value() == pytest.approx(370, abs=2)
    assert "of 14.8 GB" in card.job_detail.text()

    job = page.action_state.job
    job.bytes_done = int(job.bytes_total * 0.8)
    ui.ctx.events.publish(ev.JobUpdated(job.copy()))
    qtbot.waitUntil(lambda: card.job_title.text() == "Downloading 80%", timeout=3000)
    assert card.progress.value() == 800

    card.pause_button.click()
    qtbot.waitUntil(lambda: page.action_state.job is not None
                    and page.action_state.job.state is JobState.PAUSED, timeout=3000)
    assert pauses == [(job.id,)]
    assert card.resume_button.isVisibleTo(card) and not card.pause_button.isVisibleTo(card)
    assert card.progress.property("state") == "paused"

    card.resume_button.click()
    qtbot.waitUntil(lambda: page.action_state.job.state is JobState.DOWNLOADING, timeout=3000)

    card.cancel_button.click()
    assert confirmations and "Gears of War: E-Day" in confirmations[-1]
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.INSTALL, timeout=3000)


def test_job_events_drive_states_and_failure_retry(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    retries = spy(monkeypatch, ui.ctx.downloads, "retry", passthrough=False)
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    assert page.action_state.kind is ActionKind.INSTALL
    job = make_job("celeste", "Celeste", JobState.QUEUED, job_id="c1")
    ui.ctx.events.publish(ev.JobAdded(job))
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.JOB, timeout=3000)
    assert page.action_card.job_title.text() == "Queued"

    job.state = JobState.EXTRACTING
    job.phase_progress = 0.25
    ui.ctx.events.publish(ev.JobUpdated(job.copy()))
    qtbot.waitUntil(lambda: page.action_card.job_title.text() == "Installing 25%", timeout=3000)
    assert page.action_card.cancel_button.isHidden()

    job.state = JobState.FAILED
    job.error = "The archive is damaged."
    ui.ctx.events.publish(ev.JobUpdated(job.copy()))
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.INSTALL, timeout=3000)
    assert page.action_card.failure.text() == "Download failed: The archive is damaged."
    page.action_card.failure_retry.click()
    qtbot.waitUntil(lambda: retries == [("c1",)], timeout=3000)
    assert page.action_card.failure.isHidden()

    ui.ctx.events.publish(ev.JobAdded(make_job("celeste", "Celeste", JobState.DOWNLOADING, job_id="c2")))
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.JOB, timeout=3000)
    ui.ctx.events.publish(ev.JobRemoved("c2"))
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.INSTALL, timeout=3000)


def test_several_jobs_for_one_game_show_the_most_relevant(qtbot: Any, ui: UI,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    game_job = make_job("celeste", "Celeste", JobState.DOWNLOADING, done=300, job_id="game")
    addon_job = make_job("celeste", "Celeste", JobState.QUEUED, job_id="addon", kind=DownloadKind.ADDON)
    ui.ctx.events.publish(ev.JobAdded(game_job))
    qtbot.waitUntil(lambda: page.action_state.job is not None, timeout=3000)
    ui.ctx.events.publish(ev.JobAdded(addon_job))  # queued behind the running download
    ui.ctx.events.publish(ev.JobUpdated(game_job.copy()))
    qtbot.wait(50)
    assert page.action_state.job.id == "game"  # no flipping between the two jobs

    # when the shown job finishes, the page picks up the one still queued
    monkeypatch.setattr(ui.ctx.downloads, "job_for", lambda slug: addon_job.copy())
    game_job.state = JobState.CANCELLED
    ui.ctx.events.publish(ev.JobUpdated(game_job.copy()))
    qtbot.waitUntil(lambda: page.action_state.job is not None and page.action_state.job.id == "addon",
                    timeout=3000)
    assert page.action_state.kind is ActionKind.JOB


def test_out_of_order_library_lookups_keep_the_newest(qtbot: Any, ui: UI,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    page = make_page(qtbot, ui)
    loaded(qtbot, page, "hollow-knight", ui=ui)
    assert page.action_state.kind is ActionKind.PLAY
    stale = ui.ctx.library.find_by_slug("hollow-knight")
    gate = threading.Event()
    calls: list[int] = []

    def find_by_slug(slug: str) -> Any:
        calls.append(1)
        if len(calls) == 1:  # the first (older) lookup is slow and returns what it saw back then
            gate.wait(5)
            return stale
        return None  # uninstalled meanwhile

    monkeypatch.setattr(ui.ctx.library, "find_by_slug", find_by_slug)
    ui.ctx.events.publish(ev.LibraryChanged(frozenset({"hollow-knight"})))
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=3000)
    ui.ctx.events.publish(ev.LibraryChanged(frozenset({"hollow-knight"})))
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.INSTALL, timeout=3000)
    gate.set()
    qtbot.wait(100)
    assert page.action_state.kind is ActionKind.INSTALL  # the late, older answer is ignored


def test_launch_during_initial_state_read_is_not_lost(qtbot: Any, ui: UI,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    gate = threading.Event()
    real = ui.ctx.launcher.is_running
    calls: list[str] = []

    def is_running(install_id: str) -> bool:
        calls.append(install_id)
        answer = real(install_id)
        if len(calls) == 1:
            gate.wait(5)  # the first read answers "not running", but only after the launch happened
        return answer

    monkeypatch.setattr(ui.ctx.launcher, "is_running", is_running)
    page = make_page(qtbot, ui)
    page.load("hollow-knight", game(ui.ctx, "hollow-knight"))
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=3000)
    ui.ctx.launcher.launch("hollow-knight")  # e.g. from the tray while the page is opening
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.RUNNING, timeout=3000)
    gate.set()
    qtbot.wait(100)
    assert page.action_state.kind is ActionKind.RUNNING


def test_destroyed_page_ignores_bridge_signals(qtbot: Any, ui: UI) -> None:
    from PyQt6 import sip

    page = GamePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    page.resize(1060, 900)
    page.show()
    loaded(qtbot, page, "hollow-knight", ui=ui)
    page.shutdown()
    sip.delete(page)
    ui.ctx.events.publish(ev.UpdatesFound(()))
    ui.ctx.events.publish(ev.LibraryChanged())
    ui.ctx.events.publish(ev.WishlistChanged("hollow-knight", True))
    qtbot.wait(80)  # pytest-qt fails the test if any slot raised


def test_installed_play_stop_and_manage(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch,
                                        confirmations: list[str]) -> None:
    launches = spy(monkeypatch, ui.ctx.launcher, "launch")
    folders = spy(monkeypatch, ui.ctx.launcher, "open_folder")
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "hollow-knight", ui=ui)
    card = page.action_card
    assert page.action_state.kind is ActionKind.PLAY
    assert card.primary.text() == "Play"
    assert card.status.text() == "Installed · v1.1.0"
    assert "41.0 hours" in card.caption.text()

    card.primary.click()
    qtbot.waitUntil(lambda: launches == [("hollow-knight",)], timeout=3000)
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.RUNNING, timeout=3000)
    assert card.primary.text() == "Stop"
    card.primary.click()
    assert "Hollow Knight" in confirmations[-1]
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.PLAY, timeout=3000)

    def actions() -> dict[str, Any]:
        # Looked up fresh each time: the page rebuilds this menu whenever its state refreshes
        # (e.g. on download progress events), deleting the previous QActions.
        return {a.text(): a for a in card.menu().actions() if a.text()}

    assert list(actions()) == ["Show in library", "Open install folder", "Choose executable…", "Reinstall…"]
    actions()["Show in library"].trigger()
    assert ui.nav.named("show_library")[-1].args == ("hollow-knight",)
    actions()["Open install folder"].trigger()
    qtbot.waitUntil(lambda: folders == [("hollow-knight",)], timeout=3000)
    actions()["Reinstall…"].trigger()
    assert ui.nav.named("request_install")[-1].args[1].kind is DownloadKind.FULL

    def no_exe(install_id: str) -> None:
        raise ExecutableNotSetError()

    monkeypatch.setattr(ui.ctx.launcher, "launch", no_exe)
    card.primary.click()
    qtbot.waitUntil(lambda: bool(ui.nav.named("choose_executable")), timeout=3000)
    assert ui.nav.named("choose_executable")[-1].args == ("hollow-knight",)

    ui.ctx.library._change("hollow-knight", executable="")  # → "Choose executable" state
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.SETUP, timeout=3000)
    card.primary.click()
    assert len(ui.nav.named("choose_executable")) == 2


def test_update_states(qtbot: Any, ui: UI) -> None:
    ui.ctx.library._change("hollow-knight", version="v1.0.0", update_available=True, latest_version="v1.1.0")
    ui.ctx.library._change("minecraft", version="v1.0.5")
    page = make_page(qtbot, ui)
    card = page.action_card

    # patch when the installed version equals the patch's from_version
    loaded(qtbot, page, "hollow-knight", ui=ui)
    state = page.action_state
    assert state.kind is ActionKind.UPDATE and state.is_patch
    assert card.primary.text() == "Update v1.0.0 → v1.1.0"
    assert card.caption.text() == "Patch · 124 MB"
    assert card.secondary.text() == "Play v1.0.0" and not card.secondary.isHidden()
    card.primary.click()
    _details, option = ui.nav.named("request_install")[-1].args
    assert option.kind is DownloadKind.PATCH and option.download_id == 3000
    card.secondary.click()
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.RUNNING, timeout=3000)

    # no matching patch → full download
    loaded(qtbot, page, "elden-ring", ui=ui)
    state = page.action_state
    assert state.kind is ActionKind.UPDATE and not state.is_patch
    assert card.caption.text() == "Full download · 29.40 GB"
    card.primary.click()
    _details, option = ui.nav.named("request_install")[-1].args
    assert option.kind is DownloadKind.FULL and option.download_id == 1004

    # not flagged by the update checker, but the site lists a newer version
    loaded(qtbot, page, "minecraft", ui=ui)
    assert page.action_state.kind is ActionKind.UPDATE
    assert card.primary.text() == "Update v1.0.5 → v1.1.0"


def test_addons_install_only_when_base_installed(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "hollow-knight", ui=ui)  # installed, has a Language Pack add-on
    assert page.addons.isVisible()
    (button,) = page.addons.buttons.values()
    assert button.isEnabled()
    button.click()
    _details, option = ui.nav.named("request_install")[-1].args
    assert option.kind is DownloadKind.ADDON

    # an add-on download starts: buttons disable once, then progress ticks leave the rows alone
    job = make_job("hollow-knight", "Hollow Knight", JobState.DOWNLOADING, done=100, job_id="lp",
                   kind=DownloadKind.ADDON)
    ui.ctx.events.publish(ev.JobAdded(job.copy()))
    qtbot.waitUntil(lambda: not next(iter(page.addons.buttons.values())).isEnabled(), timeout=3000)
    (busy_button,) = page.addons.buttons.values()
    job.bytes_done = 600
    ui.ctx.events.publish(ev.JobUpdated(job.copy()))
    qtbot.waitUntil(lambda: page.action_card.job_title.text() == "Downloading 60%", timeout=3000)
    assert next(iter(page.addons.buttons.values())) is busy_button  # not rebuilt per tick
    assert page.addons.hint.text() == "Available when the current download finishes."

    loaded(qtbot, page, "forza-horizon-6", ui=ui)  # not installed, has an add-on
    (button,) = page.addons.buttons.values()
    assert not button.isEnabled()
    assert page.addons.hint.text() == "Install the game first to add these."


def test_library_change_switches_to_installed(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    assert page.action_state.kind is ActionKind.INSTALL
    ui.ctx.library.adopt("local:roadhouse simulator", slug="celeste", title="Celeste")
    qtbot.waitUntil(lambda: page.action_state.kind is ActionKind.PLAY, timeout=3000)
    assert page.facts.value_of("Installed")


# --- wishlist / links ---------------------------------------------------------------------------------


def test_wishlist_toggle_updates_game_and_store(qtbot: Any, ui: UI) -> None:
    store = StorePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(store)
    store.resize(1060, 800)
    store.show()
    store.on_activated()
    store.tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: store.browse.panel.state == ResultsPanel.GRID and store.states.loaded, timeout=5000)
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "cyberpunk-2077", ui=ui)
    assert page.wishlist_button.text() == "Add to wishlist"

    page.wishlist_button.click()
    qtbot.waitUntil(lambda: ui.ctx.catalog.is_wishlisted("cyberpunk-2077"), timeout=3000)
    assert page.wishlist_button.isChecked() and page.wishlist_button.text() == "On your wishlist"
    model = store.browse.panel.grid.grid_model
    qtbot.waitUntil(lambda: model.item("cyberpunk-2077").favorite, timeout=3000)
    store.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: [i.key for i in store.wishlist.panel.grid.grid_model.items()] == ["cyberpunk-2077"],
                    timeout=3000)

    # removing it elsewhere (store context menu) updates the game page
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "cyberpunk-2077"), False)
    qtbot.waitUntil(lambda: not page.wishlist_button.isChecked(), timeout=3000)
    qtbot.waitUntil(lambda: not model.item("cyberpunk-2077").favorite, timeout=3000)
    qtbot.waitUntil(lambda: store.wishlist.panel.state == ResultsPanel.EMPTY, timeout=3000)


def test_links_sign_in_fact_and_hero_navigation(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    page.copy_button.click()
    assert QApplication.clipboard().text() == "https://ankergames.net/game/celeste"
    assert ui.nav.named("toast")[-1].args == ("Link copied to clipboard", "success")
    page.website_button.click()
    assert ui.opened_urls == ["https://ankergames.net/game/celeste"]

    fact = next(f for f in page.facts.facts if f.name == "Torrent")
    assert fact.action_text == "Sign in"
    fact.action()
    assert ui.nav.named("request_login")
    ui.ctx.auth.login("me@example.com", "password")
    qtbot.waitUntil(lambda: page.facts.value_of("Torrent") == "Available", timeout=3000)

    chips = page.hero.chips.chips()
    assert [c.text() for c in chips] == page.details.genres
    chips[2].click()
    assert ui.nav.named("show_store")[-1].kwargs["genre"] == game_common.genre_slug(page.details.genres[2])
    page.hero.back_button.click()
    assert ui.nav.named("back")


# --- carousel / lightbox / layout ------------------------------------------------------------------------


def test_carousel_keyboard_and_lightbox(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    carousel = page.carousel
    assert carousel.current_index == 0 and carousel.main.counter == "1 / 4"
    carousel.setFocus()
    qtbot.keyClick(carousel, Qt.Key.Key_Right)
    assert carousel.current_index == 1
    qtbot.keyClick(carousel, Qt.Key.Key_Left)
    qtbot.keyClick(carousel, Qt.Key.Key_Left)  # wraps around
    assert carousel.current_index == 3
    carousel.thumbs()[2].clicked.emit()
    assert carousel.current_index == 2
    assert [t.selected for t in carousel.thumbs()] == [False, False, True, False]

    qtbot.mouseClick(carousel.main, Qt.MouseButton.LeftButton)
    box = page.last_lightbox
    assert box is not None and box.isVisible() and box.current_index == 2
    # keys go to whatever has focus inside the dialog, as for a real user; an overlay button
    # with focus would turn the arrows into focus moves
    qtbot.waitUntil(lambda: box.focusWidget() is not None, timeout=2000)
    focused = box.focusWidget()
    assert focused is box
    qtbot.keyClick(focused, Qt.Key.Key_Right)
    assert box.current_index == 3 and carousel.current_index == 3  # carousel follows
    qtbot.keyClick(focused, Qt.Key.Key_Home)
    assert box.current_index == 0
    qtbot.keyClick(focused, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: page.last_lightbox is None, timeout=2000)
    qtbot.wait(20)  # the closed dialog is deleted (WA_DeleteOnClose); the page must not touch it

    carousel.image_activated.emit(0)
    reopened = page.last_lightbox
    assert reopened is not None and reopened.isVisible()
    with qtbot.waitSignal(reopened.rejected, timeout=1000):
        page.on_deactivated()  # leaving the page closes it
    assert page.last_lightbox is None


def test_about_show_more_toggle(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(qtbot, ui, size=(1060, 900))
    original = ui.ctx.client.game_details

    def long_text(slug: str, **kwargs: Any) -> GameDetails:
        details = original(slug, **kwargs)
        details.description = "\n\n".join(["A long paragraph about the game. " * 12] * 6)
        return details

    monkeypatch.setattr(ui.ctx.client, "game_details", long_text)
    loaded(qtbot, page, "celeste", ui=ui)
    about = page.about
    qtbot.waitUntil(lambda: not about.toggle.isHidden(), timeout=3000)
    collapsed = about.text_label.maximumHeight()
    assert about.toggle.text() == "Show more"
    about.toggle.click()
    assert about.expanded and about.toggle.text() == "Show less"
    assert about.text_label.maximumHeight() > collapsed
    about.toggle.click()
    assert not about.expanded


def test_responsive_side_column_moves_below_hero(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui, size=(1300, 900))
    loaded(qtbot, page, "celeste", ui=ui)
    assert not page.is_narrow
    assert page.side_column.width() == GamePage.SIDE_WIDTH
    assert page.side_column.x() > page.main_column.x()
    page.resize(820, 900)
    qtbot.waitUntil(lambda: page.is_narrow, timeout=2000)
    qtbot.wait(50)
    assert page.side_column.y() < page.main_column.y()
    assert page.side_column.width() > GamePage.SIDE_WIDTH
    page.resize(1300, 900)
    qtbot.waitUntil(lambda: not page.is_narrow, timeout=2000)


def test_load_same_slug_refreshes_quietly(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    calls = spy(monkeypatch, ui.ctx.catalog, "details")
    page.load("celeste")
    assert page._stack.currentWidget() is page.scroll  # no loading flash
    qtbot.waitUntil(lambda: calls == [("celeste",)], timeout=3000)
    page.on_activated()
    page.shutdown()


def test_live_theme_switch_recolours_icons(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    loaded(qtbot, page, "celeste", ui=ui)
    store = StorePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(store)
    store.show()
    store.tabs.setCurrentIndex(2)
    empty = store.wishlist.panel
    qtbot.waitUntil(lambda: empty.state == ResultsPanel.EMPTY, timeout=3000)
    before = (page.website_button.icon().cacheKey(), page.action_card.primary.icon().cacheKey(),
              store.refresh_button.icon().cacheKey())
    assert _tint(empty.empty._icon.pixmap()) == palette.current().text_faint
    ui.theme.apply("daylight")
    qtbot.waitUntil(lambda: page.website_button.icon().cacheKey() != before[0], timeout=2000)
    qtbot.waitUntil(lambda: store.refresh_button.icon().cacheKey() != before[2], timeout=2000)
    assert page.action_card.primary.icon().cacheKey() != before[1]
    # the empty-state artwork is re-tinted too (it is a pixmap baked with the old palette)
    assert _tint(empty.empty._icon.pixmap()) == palette.current().text_faint
    assert empty.empty._title.text() == "Your wishlist is empty"
    ui.theme.apply("midnight")


def _tint(pixmap: Any) -> str:
    """Colour of the first fully opaque pixel of a tinted icon pixmap."""
    image = pixmap.toImage()
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if colour.alpha() == 255:
                return colour.name()
    return ""
