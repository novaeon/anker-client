"""StorePage: tabs, discover rows, browse paging/sort/genres, search merge + stale cancellation,
wishlist, live card badges and the card context menu (FakeContext, offscreen)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from PyQt6.QtWidgets import QApplication

from anker_client.core import events as ev
from anker_client.core.errors import NetworkError
from anker_client.core.models import JobState, ListingPage, SortOrder
from anker_client.ui.pages.store import BROWSE, DISCOVER, SEARCH, WISHLIST, StorePage
from anker_client.ui.widgets.store_common import ResultsPanel
from tests.gui.test_store_ui_support import UI, game, make_job, ui_session

pytestmark = pytest.mark.gui


@pytest.fixture
def ui(qtbot: Any, fake_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[UI]:
    yield from ui_session(fake_ctx, monkeypatch)


def make_page(qtbot: Any, ui: UI, size: tuple[int, int] = (1060, 800)) -> StorePage:
    page = StorePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(page)
    page.resize(*size)
    page.show()
    page.on_activated()
    return page


def record_calls(monkeypatch: pytest.MonkeyPatch, obj: Any, name: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = getattr(obj, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, **{k: v for k, v in kwargs.items() if k != "token"}})
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)
    return calls


def keys(view: Any) -> list[str]:
    return [i.key for i in view.grid_model.items()]


def wait_grid(qtbot: Any, panel: ResultsPanel, count: int | None = None) -> None:
    if count is None:
        qtbot.waitUntil(lambda: panel.state == ResultsPanel.GRID, timeout=5000)
    else:
        qtbot.waitUntil(lambda: panel.grid.grid_model.rowCount() == count, timeout=5000)


def scroll_to_end(view: Any) -> None:
    view.doItemsLayout()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())
    view.near_end.emit()


# --- tabs & discover --------------------------------------------------------------------------


def test_tabs_switch_views_and_load_lazily(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    assert page.current_view() == DISCOVER
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == ["Discover", "Browse", "Wishlist"]
    qtbot.waitUntil(lambda: page.discover.state == "content", timeout=5000)

    page.tabs.setCurrentIndex(1)
    assert page.current_view() == BROWSE
    wait_grid(qtbot, page.browse.panel)

    page.tabs.setCurrentIndex(2)
    assert page.current_view() == WISHLIST
    qtbot.waitUntil(lambda: page.wishlist.panel.state == ResultsPanel.EMPTY, timeout=5000)
    assert page.wishlist.panel.empty._title.text() == "Your wishlist is empty"
    page.wishlist.panel.empty.action_clicked.emit()  # "Browse the store"
    assert page.current_view() == BROWSE


def test_discover_rows_badges_and_see_all(qtbot: Any, ui: UI) -> None:
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "hollow-knight-silksong"), True)
    page = make_page(qtbot, ui)
    qtbot.waitUntil(lambda: len(page.discover.rows) == 5 and page.states.loaded, timeout=5000)
    rows = {row.title: row for row in page.discover.rows}
    assert list(rows) == ["Trending Games", "Upcoming Games", "Latest Games", "Masterpiece Collection", "Top games"]
    assert len(rows["Trending Games"].items()) == 12
    see_all = {title: not row.see_all_button.isHidden() for title, row in rows.items()}
    assert see_all == {"Trending Games": True, "Upcoming Games": False, "Latest Games": True,
                       "Masterpiece Collection": False, "Top games": True}

    model = rows["Trending Games"].view.grid_model
    qtbot.waitUntil(lambda: model.item("hollow-knight").badges != (), timeout=3000)
    assert [b.text for b in model.item("hollow-knight").badges] == ["Installed"]
    assert [b.text for b in model.item("elden-ring").badges] == ["Update"]
    gears = model.item("gears-of-war-e-day")
    assert gears.badges[0].text.startswith("Downloading") and gears.progress is not None
    assert model.item("hollow-knight-silksong").favorite is True

    # horizontal strip: no wrapping, fixed height, horizontal scrolling
    view = rows["Trending Games"].view
    assert not view.isWrapping()
    assert view.horizontalScrollBar().maximum() > 0

    rows["Latest Games"].see_all_button.click()
    assert page.current_view() == BROWSE
    assert page.browse.sort is SortOrder.NEWEST

    view.item_activated.emit("cyberpunk-2077")
    call = ui.nav.named("show_game")[-1]
    assert call.args[0] == "cyberpunk-2077" and call.args[1].title == "Cyberpunk 2077"


def test_discover_is_cached_for_ten_minutes(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, ui.ctx.client, "home_sections")
    now = [1000.0]
    page = make_page(qtbot, ui)
    page.discover._clock = lambda: now[0]
    qtbot.waitUntil(lambda: page.discover.state == "content", timeout=5000)
    assert len(calls) == 1
    page.on_activated()
    assert len(calls) == 1
    now[0] += 601
    page.on_activated()
    qtbot.waitUntil(lambda: len(calls) == 2, timeout=3000)
    assert page.discover.state == "content"  # silent refresh keeps the rows


def test_discover_partial_and_total_failure(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    client = ui.ctx.client
    originals = {name: getattr(client, name) for name in ("home_sections", "top_games")}

    def broken(**_kwargs: Any) -> Any:
        raise NetworkError()

    monkeypatch.setattr(client, "home_sections", broken)
    page = make_page(qtbot, ui)
    qtbot.waitUntil(lambda: page.discover.state == "content", timeout=5000)
    assert [r.title for r in page.discover.rows] == ["Top games"]  # partial success still renders

    other = make_page(qtbot, ui)
    monkeypatch.setattr(client, "top_games", broken)
    other.discover.ensure_loaded(force=True)
    qtbot.waitUntil(lambda: other.discover.state == "error", timeout=5000)
    assert "internet connection" in other.discover.error._message.text()
    for name, fn in originals.items():
        monkeypatch.setattr(client, name, fn)
    other.discover.error.action_clicked.emit()  # Retry
    qtbot.waitUntil(lambda: other.discover.state == "content" and len(other.discover.rows) == 5, timeout=5000)


def test_discover_genre_chips_open_browse(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    qtbot.waitUntil(lambda: page.discover.genre_flow.chips() != [] and page.discover.state == "content", timeout=5000)
    names = [chip.text() for chip in page.discover.genre_flow.chips()]
    assert "Action" in names and "VR" in names and "NSFW" not in names
    action = next(chip for chip in page.discover.genre_flow.chips() if chip.text() == "Action")
    action.click()
    assert page.current_view() == BROWSE
    assert page.browse.genre == "action"


# --- browse ---------------------------------------------------------------------------------------


def test_browse_infinite_scroll_pages_until_end(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, ui.ctx.client, "browse")
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    grid = page.browse.panel.grid
    wait_grid(qtbot, page.browse.panel, 24)
    qtbot.wait(50)
    assert [c["page"] for c in calls] == [1]  # first page fills the viewport; no eager second page
    assert page.browse.status.text() == "Showing 24 games"

    scroll_to_end(grid)
    assert page.browse.panel.footer.mode == "loading"
    assert page.browse.panel.footer.text.text() == "Loading more…"
    wait_grid(qtbot, page.browse.panel, 48)
    scroll_to_end(grid)
    wait_grid(qtbot, page.browse.panel, 60)
    assert [c["page"] for c in calls] == [1, 2, 3]
    assert page.browse.has_next is False
    assert page.browse.panel.footer.mode == "end"
    assert "60 games" in page.browse.panel.footer.text.text()
    assert not page.browse.panel.footer.text.isHidden()  # a scrolled list shows where it ends

    scroll_to_end(grid)  # past the end: nothing more is requested
    qtbot.wait(50)
    assert len(calls) == 3
    assert len(set(keys(grid))) == 60


def test_browse_sort_change_requeries_and_persists(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, ui.ctx.client, "browse")
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    wait_grid(qtbot, page.browse.panel, 24)
    page.browse.sort_combo.setCurrentIndex(list(SortOrder).index(SortOrder.TITLE))
    qtbot.waitUntil(lambda: calls[-1]["sort"] is SortOrder.TITLE, timeout=3000)
    assert calls[-1]["page"] == 1
    first = min(ui.ctx.client.games, key=lambda g: g.title.casefold()).slug
    qtbot.waitUntil(lambda: keys(page.browse.panel.grid)[:1] == [first], timeout=3000)
    assert ui.ctx.settings.get().store_sort == SortOrder.TITLE.value

    # an external settings change (e.g. another window) re-sorts too
    ui.ctx.settings.update(store_sort=SortOrder.RELEASE_DATE.value)
    qtbot.waitUntil(lambda: page.browse.sort is SortOrder.RELEASE_DATE, timeout=3000)
    qtbot.waitUntil(lambda: calls[-1]["sort"] is SortOrder.RELEASE_DATE, timeout=3000)


def test_browse_genre_chips_requery_and_nsfw_toggle(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, ui.ctx.client, "browse")
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    wait_grid(qtbot, page.browse.panel)
    qtbot.waitUntil(lambda: len(page.browse.chip_buttons()) > 3, timeout=3000)
    chips = {c.text(): c for c in page.browse.chip_buttons()}
    assert next(iter(chips)) == "All" and chips["All"].isChecked()
    assert "VR" in chips and "NSFW" not in chips

    chips["Action"].click()
    qtbot.waitUntil(lambda: calls[-1]["genre"] == "action", timeout=3000)
    wait_grid(qtbot, page.browse.panel, 5)
    assert all(i.payload.primary_genre == "Action" for i in page.browse.panel.grid.grid_model.items())
    assert {c.text(): c.isChecked() for c in page.browse.chip_buttons()}["Action"]

    page.set_genre("Open World")  # display names resolve to slugs
    qtbot.waitUntil(lambda: calls[-1]["genre"] == "open-world", timeout=3000)
    assert {c.text(): c.isChecked() for c in page.browse.chip_buttons()}["Open World"]

    ui.ctx.settings.update(show_nsfw=True)
    qtbot.waitUntil(lambda: "NSFW" in [c.text() for c in page.browse.chip_buttons()], timeout=3000)


def test_browse_errors_and_retry(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ui.ctx.client.browse
    fail_from_page = [1]
    pages: list[int] = []

    def flaky(**kwargs: Any) -> ListingPage:
        pages.append(kwargs.get("page", 1))
        if kwargs.get("page", 1) >= fail_from_page[0]:
            raise NetworkError()
        return original(**kwargs)

    monkeypatch.setattr(ui.ctx.client, "browse", flaky)
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: page.browse.panel.state == ResultsPanel.ERROR, timeout=5000)

    fail_from_page[0] = 2  # first page works now, later pages still fail
    page.browse.panel.error.action_clicked.emit()
    wait_grid(qtbot, page.browse.panel, 24)
    scroll_to_end(page.browse.panel.grid)
    qtbot.waitUntil(lambda: page.browse.panel.footer.mode == "error", timeout=3000)
    assert page.browse.panel.state == ResultsPanel.GRID  # existing results stay
    failed_requests = len(pages)

    # further scrolling must not re-send the failing request on every wheel tick
    for _ in range(3):
        scroll_to_end(page.browse.panel.grid)
    qtbot.wait(60)
    assert len(pages) == failed_requests and page.browse.panel.footer.mode == "error"

    fail_from_page[0] = 99
    page.browse.panel.footer.retry_button.click()
    wait_grid(qtbot, page.browse.panel, 48)
    assert pages[-1] == 2


# --- search ---------------------------------------------------------------------------------------


def test_search_merges_local_and_server_without_duplicates(qtbot: Any, ui: UI,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    local_only = [game(ui.ctx, "hollow-knight")]
    monkeypatch.setattr(ui.ctx.catalog, "search", lambda query, **_k: [g.copy() for g in local_only])
    gate = threading.Event()
    original = ui.ctx.client.search

    def slow_server(query: str, **kwargs: Any) -> ListingPage:
        gate.wait(5)
        return original(query, **kwargs)

    monkeypatch.setattr(ui.ctx.client, "search", slow_server)
    page = make_page(qtbot, ui)
    stack_top = page._stack.geometry().top()
    page.set_query("hollow")
    assert page.current_view() == SEARCH
    assert page.tabs.tabText(page.tabs.count() - 1) == "Search: hollow"
    page.layout().activate()
    assert page._stack.geometry().top() == stack_top  # the extra tab doesn't push the page down
    wait_grid(qtbot, page.search.panel, 1)  # instant local result while the server is still busy
    assert page.search.panel.footer.mode == "loading"
    gate.set()
    wait_grid(qtbot, page.search.panel, 2)
    assert keys(page.search.panel.grid) == ["hollow-knight", "hollow-knight-silksong"]  # local first, no dupes
    qtbot.waitUntil(lambda: page.search.panel.footer.mode == "end", timeout=3000)
    assert page.search.count.text() == "2 results"
    # two cards don't scroll: no "End of results" line floating at the bottom of the view
    assert page.search.panel.footer.text.isHidden()


def test_search_keeps_paging_when_a_page_adds_nothing_visible(qtbot: Any, ui: UI,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Server pages that fill nothing (too few cards, or only already-shown / filtered games)
    must not stall pagination: there's no scroll event left to ask for more."""
    monkeypatch.setattr(ui.ctx.catalog, "search", lambda query, **_k: [])
    games = ui.ctx.client.games
    nsfw = game(ui.ctx, "celeste")
    nsfw.primary_genre = "NSFW"
    server_pages = {1: [nsfw], 2: games[:3], 3: games[3:6]}
    requested: list[int] = []

    def server(query: str, *, page: int = 1, token: Any = None) -> ListingPage:
        requested.append(page)
        return ListingPage(games=[g.copy() for g in server_pages[page]], page=page, has_next=page < 3)

    monkeypatch.setattr(ui.ctx.client, "search", server)
    page = make_page(qtbot, ui)
    page.set_query("anything")
    qtbot.waitUntil(lambda: page.search.panel.footer.mode == "end", timeout=5000)
    assert requested == [1, 2, 3]  # page 1 was entirely filtered out; 2 didn't fill the view
    assert keys(page.search.panel.grid) == [g.slug for g in games[:6]]


def test_search_cancels_superseded_queries(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    tokens: dict[str, Any] = {}
    original = ui.ctx.client.search

    def server(query: str, *, page: int = 1, token: Any = None) -> ListingPage:
        tokens[query] = token
        if query == "elden":
            gate.wait(5)
        return original(query, page=page, token=token)

    monkeypatch.setattr(ui.ctx.client, "search", server)
    page = make_page(qtbot, ui)
    page.set_query("elden")
    qtbot.waitUntil(lambda: "elden" in tokens, timeout=3000)
    page.set_query("cyberpunk")
    assert tokens["elden"].cancelled  # superseded request cancelled
    qtbot.waitUntil(lambda: keys(page.search.panel.grid) == ["cyberpunk-2077"], timeout=3000)
    gate.set()
    qtbot.wait(150)
    assert keys(page.search.panel.grid) == ["cyberpunk-2077"]  # late "elden" results never appear
    assert page.search.title.text() == "Results for “cyberpunk”"


def test_search_server_errors(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ui.ctx.client.search
    original_local = ui.ctx.catalog.search

    def broken(query: str, **_kwargs: Any) -> ListingPage:
        raise NetworkError()

    monkeypatch.setattr(ui.ctx.client, "search", broken)
    page = make_page(qtbot, ui)
    page.set_query("hollow")
    qtbot.waitUntil(lambda: page.search.panel.footer.mode == "error", timeout=3000)
    assert page.search.panel.state == ResultsPanel.GRID  # local results stay visible
    assert len(keys(page.search.panel.grid)) == 2
    monkeypatch.setattr(ui.ctx.client, "search", original)
    page.search.panel.footer.retry_button.click()
    qtbot.waitUntil(lambda: page.search.panel.footer.mode == "end", timeout=3000)

    # nothing local and the server fails: full error state
    monkeypatch.setattr(ui.ctx.catalog, "search", lambda query, **_k: [])
    monkeypatch.setattr(ui.ctx.client, "search", broken)
    page.set_query("celeste")
    qtbot.waitUntil(lambda: page.search.panel.state == ResultsPanel.ERROR, timeout=3000)
    assert page.search.panel.error._title.text() == "Search failed"
    monkeypatch.setattr(ui.ctx.client, "search", original)
    monkeypatch.setattr(ui.ctx.catalog, "search", original_local)
    page.search.panel.error.action_clicked.emit()
    qtbot.waitUntil(lambda: keys(page.search.panel.grid) == ["celeste"], timeout=3000)


def test_search_empty_state_and_clearing(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(2)
    page.set_query("zzzz")
    qtbot.waitUntil(lambda: page.search.panel.state == ResultsPanel.EMPTY, timeout=3000)
    assert "zzzz" in page.search.panel.empty._title.text()

    page.set_query("")  # back to the tab the user came from
    assert page.current_view() == WISHLIST
    assert page.tabs.count() == 3

    page.set_query("zzzz")
    qtbot.waitUntil(lambda: page.search.panel.state == ResultsPanel.EMPTY, timeout=3000)
    page.search.panel.empty.action_clicked.emit()  # "Browse the store"
    assert page.current_view() == BROWSE
    assert page.tabs.count() == 3
    assert page.search.query == ""


def test_search_paginates_server_results(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui.ctx.catalog, "search", lambda query, **_k: [])
    calls = record_calls(monkeypatch, ui.ctx.client, "search")
    page = make_page(qtbot, ui, size=(900, 600))
    page.set_query("e")  # matches more than one server page
    wait_grid(qtbot, page.search.panel, 24)
    scroll_to_end(page.search.panel.grid)
    qtbot.waitUntil(lambda: page.search.server_page == 2, timeout=3000)
    assert [c["page"] for c in calls] == [1, 2]
    assert len(keys(page.search.panel.grid)) == len(set(keys(page.search.panel.grid))) > 24


# --- live state, wishlist, context menu ----------------------------------------------------------------


def test_card_badges_follow_download_and_library_events(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    wait_grid(qtbot, page.browse.panel, 24)
    qtbot.waitUntil(lambda: page.states.loaded, timeout=3000)
    model = page.browse.panel.grid.grid_model

    job = make_job("red-dead-redemption-2", "Red Dead Redemption 2", JobState.DOWNLOADING, done=450, total=1000,
                   job_id="rdr2")
    ui.ctx.events.publish(ev.JobAdded(job))
    qtbot.waitUntil(lambda: model.item("red-dead-redemption-2").badges != (), timeout=3000)
    item = model.item("red-dead-redemption-2")
    assert item.badges[0].text == "Downloading 45%" and item.progress == pytest.approx(0.45)

    job.state = JobState.EXTRACTING
    job.phase_progress = 0.5
    ui.ctx.events.publish(ev.JobUpdated(job.copy()))
    qtbot.waitUntil(lambda: model.item("red-dead-redemption-2").badges[0].text == "Installing 50%", timeout=3000)

    ui.ctx.events.publish(ev.JobRemoved("rdr2"))
    qtbot.waitUntil(lambda: model.item("red-dead-redemption-2").badges == (), timeout=3000)

    ui.ctx.library.set_update_state("hollow-knight", latest_version="v1.2.0", available=True)
    qtbot.waitUntil(lambda: [b.text for b in model.item("hollow-knight").badges] == ["Update"], timeout=3000)

    ui.ctx.launcher.launch("hollow-knight")
    qtbot.waitUntil(lambda: [b.text for b in model.item("hollow-knight").badges] == ["Playing"], timeout=3000)


def test_card_badge_moves_on_to_the_next_job_of_a_game(qtbot: Any, ui: UI,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    wait_grid(qtbot, page.browse.panel, 24)
    qtbot.waitUntil(lambda: page.states.loaded, timeout=3000)
    model = page.browse.panel.grid.grid_model
    slug, title = "red-dead-redemption-2", "Red Dead Redemption 2"
    main = make_job(slug, title, JobState.DOWNLOADING, done=500, total=1000, job_id="main")
    queued = make_job(slug, title, JobState.QUEUED, job_id="addon")
    ui.ctx.events.publish(ev.JobAdded(main))
    ui.ctx.events.publish(ev.JobAdded(queued))  # less relevant: the running download stays on the card
    qtbot.waitUntil(lambda: model.item(slug).badges[:1] != () and
                    model.item(slug).badges[0].text == "Downloading 50%", timeout=3000)
    qtbot.wait(30)
    assert model.item(slug).badges[0].text == "Downloading 50%"

    monkeypatch.setattr(ui.ctx.downloads, "job_for", lambda s: queued.copy() if s == slug else None)
    main.state = JobState.COMPLETED
    ui.ctx.events.publish(ev.JobUpdated(main.copy()))
    qtbot.waitUntil(lambda: [b.text for b in model.item(slug).badges] == ["Queued"], timeout=3000)


def test_context_menu_actions(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(1)
    wait_grid(qtbot, page.browse.panel, 24)
    qtbot.waitUntil(lambda: page.states.loaded, timeout=3000)
    grid = page.browse.panel.grid

    def menu_for(slug: str) -> dict[str, Any]:
        grid.context_requested.emit(slug, grid.mapToGlobal(grid.rect().center()))
        menu = page.browse.last_menu
        assert menu is not None
        actions = {a.text(): a for a in menu.actions() if a.text()}
        return actions

    # A card menu deletes itself once it hides, so open a fresh one for every action
    # (exactly like a user would) instead of reusing actions across event processing.
    actions = menu_for("cyberpunk-2077")
    assert list(actions) == ["View details", "Add to wishlist", "Open on website", "Copy link"]
    actions["Add to wishlist"].trigger()
    qtbot.waitUntil(lambda: ui.ctx.catalog.is_wishlisted("cyberpunk-2077"), timeout=3000)
    qtbot.waitUntil(lambda: grid.grid_model.item("cyberpunk-2077").favorite, timeout=3000)

    actions = menu_for("cyberpunk-2077")
    assert "Remove from wishlist" in actions
    actions["View details"].trigger()
    assert ui.nav.named("show_game")[-1].args[0] == "cyberpunk-2077"

    actions = menu_for("cyberpunk-2077")
    actions["Copy link"].trigger()
    assert QApplication.clipboard().text() == "https://ankergames.net/game/cyberpunk-2077"
    assert ui.nav.named("toast")[-1].args == ("Link copied to clipboard", "success")
    actions["Open on website"].trigger()
    assert ui.opened_urls == ["https://ankergames.net/game/cyberpunk-2077"]

    actions = menu_for("hollow-knight")  # installed → "Show in library"
    actions["Show in library"].trigger()
    assert ui.nav.named("show_library")[-1].args == ("hollow-knight",)

    page.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: keys(page.wishlist.panel.grid) == ["cyberpunk-2077"], timeout=3000)
    assert page.wishlist.count.text() == "1 game"


def test_wishlist_tab_follows_events_and_shutdown(qtbot: Any, ui: UI) -> None:
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: page.wishlist.panel.state == ResultsPanel.EMPTY, timeout=3000)
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "celeste"), True)
    qtbot.waitUntil(lambda: keys(page.wishlist.panel.grid) == ["celeste"], timeout=3000)
    assert page.wishlist.panel.state == ResultsPanel.GRID
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "celeste"), False)
    qtbot.waitUntil(lambda: page.wishlist.panel.state == ResultsPanel.EMPTY, timeout=3000)

    page.set_query("hollow")
    page.shutdown()  # cancels in-flight work
    assert page.search._server_handle is None
    page.refresh()  # still usable afterwards (F5)
    qtbot.waitUntil(lambda: page.search.panel.state == ResultsPanel.GRID, timeout=3000)


def test_wishlist_removal_keeps_scroll_position(qtbot: Any, ui: UI) -> None:
    for summary in ui.ctx.client.games[:30]:
        ui.ctx.catalog.set_wishlisted(summary, True)
    page = make_page(qtbot, ui)
    page.tabs.setCurrentIndex(2)
    grid = page.wishlist.panel.grid
    wait_grid(qtbot, page.wishlist.panel, 30)
    grid.doItemsLayout()
    bar = grid.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert bar.value() > 0
    removed = ui.ctx.client.games[0]  # the last card (wishlist is shown in stored order)
    ui.ctx.catalog.set_wishlisted(removed, False)
    wait_grid(qtbot, page.wishlist.panel, 29)
    assert removed.slug not in keys(grid)
    assert bar.value() > 0  # not reset to the top


def test_shutdown_cancels_card_state_reads(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    original = ui.ctx.library.games

    def slow_games(**kwargs: Any) -> Any:
        gate.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(ui.ctx.library, "games", slow_games)
    page = make_page(qtbot, ui)
    handle = page.states._snapshot_handle
    assert handle is not None
    page.shutdown()
    assert handle.cancelled
    gate.set()
    qtbot.wait(80)
    assert not page.states.loaded  # the cancelled snapshot never lands


def test_destroyed_page_ignores_bridge_signals(qtbot: Any, ui: UI) -> None:
    """Slots on the long-lived bridge must die with the page (no calls into deleted widgets)."""
    from PyQt6 import sip

    page = StorePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    page.resize(900, 700)
    page.show()
    page.on_activated()
    page.set_query("hollow")
    qtbot.waitUntil(lambda: page.search.panel.state == ResultsPanel.GRID, timeout=3000)
    page.shutdown()
    sip.delete(page)
    ui.ctx.events.publish(ev.WishlistChanged("celeste", True))
    ui.ctx.events.publish(ev.CatalogUpdated(60, 1))
    ui.ctx.events.publish(ev.SettingsChanged(frozenset({"show_nsfw"})))
    qtbot.wait(80)  # pytest-qt fails the test if any slot raised
