"""Visual QA renders for the Store and Game pages (build/screens/store_ui_*.png).

Opt-in because every render waits ~1.5 s for async loads: ``ANKER_SCREENSHOTS=1 pytest
tests/gui/test_store_ui_screens.py``. Covers both themes and the loading / empty /
error / populated states.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from anker_client.core import events as ev
from anker_client.core.errors import NetworkError
from anker_client.core.models import DownloadJob, DownloadKind, JobState
from anker_client.ui.pages.game import GamePage
from anker_client.ui.pages.store import StorePage
from tests.fakes import screenshot
from tests.gui.test_store_ui_support import UI, game, ui_session

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(os.environ.get("ANKER_SCREENSHOTS") != "1", reason="set ANKER_SCREENSHOTS=1 to render"),
]

THEMES = ("midnight", "daylight")


@pytest.fixture(params=THEMES)
def ui(request: pytest.FixtureRequest, qtbot: Any, fake_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[UI]:
    for session in ui_session(fake_ctx, monkeypatch, images=True):
        session.theme.apply(request.param)
        yield session


def store_page(qtbot: Any, ui: UI) -> StorePage:
    page = StorePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(page)
    page.resize(1060, 800)
    page.show()
    page.on_activated()
    return page


def game_page(qtbot: Any, ui: UI) -> GamePage:
    page = GamePage(ui.ctx, ui.bridge, ui.nav, ui.loader)
    qtbot.addWidget(page)
    return page


def test_store_screens(qtbot: Any, ui: UI) -> None:
    theme = ui.theme.current_key
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "hollow-knight-silksong"), True)
    page = store_page(qtbot, ui)
    screenshot(page, f"store_ui_discover_{theme}", (1060, 800))
    page.discover.scroll.verticalScrollBar().setValue(page.discover.scroll.verticalScrollBar().maximum())
    screenshot(page, f"store_ui_discover_bottom_{theme}", (1060, 800))
    page.tabs.setCurrentIndex(1)
    screenshot(page, f"store_ui_browse_{theme}", (1060, 800))
    page.set_query("hollow")
    screenshot(page, f"store_ui_search_{theme}", (1060, 800))
    page.set_query("zzzz")
    screenshot(page, f"store_ui_search_empty_{theme}", (1060, 800))
    page.set_query("")
    page.tabs.setCurrentIndex(2)
    screenshot(page, f"store_ui_wishlist_{theme}", (1060, 800))
    ui.ctx.catalog.set_wishlisted(game(ui.ctx, "hollow-knight-silksong"), False)
    screenshot(page, f"store_ui_wishlist_empty_{theme}", (1060, 800))
    screenshot(page, f"store_ui_wishlist_empty_min_{theme}", (860, 640))


def test_store_loading_and_error_screens(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    theme = ui.theme.current_key
    gate = threading.Event()

    def slow(**_kwargs: Any) -> Any:
        gate.wait(5)
        raise NetworkError()

    monkeypatch.setattr(ui.ctx.client, "home_sections", slow)
    monkeypatch.setattr(ui.ctx.client, "top_games", slow)
    page = store_page(qtbot, ui)
    screenshot(page, f"store_ui_loading_{theme}", (1060, 800))
    gate.set()
    qtbot.waitUntil(lambda: page.discover.state == "error", timeout=5000)
    screenshot(page, f"store_ui_error_{theme}", (1060, 800))

    # Browse: the first page works, the next one fails → footer error with Retry
    original = ui.ctx.client.browse

    def flaky(**kwargs: Any) -> Any:
        if kwargs.get("page", 1) > 1:
            raise NetworkError()
        return original(**kwargs)

    monkeypatch.setattr(ui.ctx.client, "browse", flaky)
    page.tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: page.browse.panel.grid.grid_model.rowCount() > 0, timeout=5000)
    grid = page.browse.panel.grid
    grid.doItemsLayout()
    grid.verticalScrollBar().setValue(grid.verticalScrollBar().maximum())
    qtbot.waitUntil(lambda: page.browse.panel.footer.mode == "error", timeout=5000)
    screenshot(page, f"store_ui_browse_more_error_{theme}", (1060, 800))


def test_game_screens(qtbot: Any, ui: UI) -> None:
    theme = ui.theme.current_key
    ui.ctx.library._change("hollow-knight", version="v1.0.0", update_available=True, latest_version="v1.1.0")
    page = game_page(qtbot, ui)
    page.load("red-dead-redemption-2", game(ui.ctx, "red-dead-redemption-2"))
    screenshot(page, f"store_ui_game_install_{theme}", (1060, 900))
    page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())
    screenshot(page, f"store_ui_game_install_bottom_{theme}", (1060, 900))
    page.load("hollow-knight", game(ui.ctx, "hollow-knight"))
    screenshot(page, f"store_ui_game_update_patch_{theme}", (1060, 900))
    page.load("minecraft", game(ui.ctx, "minecraft"))
    screenshot(page, f"store_ui_game_play_{theme}", (1300, 900))
    page.load("gears-of-war-e-day", game(ui.ctx, "gears-of-war-e-day"))
    screenshot(page, f"store_ui_game_downloading_{theme}", (1060, 900))
    screenshot(page, f"store_ui_game_narrow_{theme}", (860, 900))
    page.load("baldurs-gate-3", game(ui.ctx, "baldurs-gate-3"))
    qtbot.waitUntil(lambda: page.action_state.job is not None, timeout=3000)
    assert page.action_state.job.state is JobState.PAUSED
    screenshot(page, f"store_ui_game_paused_narrow_{theme}", (860, 900))

    # a patch download backing off: own line for the option, live countdown, manager reason
    page.load("hollow-knight", game(ui.ctx, "hollow-knight"))
    qtbot.waitUntil(lambda: page.details is not None, timeout=3000)
    patch = next(o for o in page.details.download_options if o.kind is DownloadKind.PATCH)
    ui.ctx.events.publish(ev.JobAdded(DownloadJob(
        id="patch", slug="hollow-knight", title="Hollow Knight", option=patch, library_root="C:/Games",
        state=JobState.WAITING, bytes_done=40 * 1024**2, bytes_total=124 * 1024**2,
        status_text="Retry 2 of 5 in 42s", retry_at=time.time() + 42)))
    screenshot(page, f"store_ui_game_patch_waiting_{theme}", (1060, 900))

    # update detected by date (no version on either side): plain "Update"
    ui.ctx.library._change("minecraft", version="", update_available=True, latest_version="")
    page.load("minecraft", game(ui.ctx, "minecraft"))
    qtbot.waitUntil(lambda: page.details is not None, timeout=3000)
    page._details.version = ""
    page._render_all()
    screenshot(page, f"store_ui_game_update_unknown_{theme}", (1060, 900))


def test_game_error_and_lightbox_screens(qtbot: Any, ui: UI, monkeypatch: pytest.MonkeyPatch) -> None:
    theme = ui.theme.current_key
    original = ui.ctx.client.game_details

    def broken(slug: str, **_kwargs: Any) -> Any:
        raise NetworkError()

    monkeypatch.setattr(ui.ctx.client, "game_details", broken)
    page = game_page(qtbot, ui)
    page.load("celeste")
    screenshot(page, f"store_ui_game_error_{theme}", (1060, 800))
    page.load("terraria", game(ui.ctx, "terraria"))
    screenshot(page, f"store_ui_game_banner_{theme}", (1060, 800))
    monkeypatch.setattr(ui.ctx.client, "game_details", original)
    page.refresh()
    qtbot.waitUntil(lambda: page.details is not None, timeout=5000)
    page.carousel.image_activated.emit(1)
    box = page.last_lightbox
    assert box is not None
    screenshot(box, f"store_ui_lightbox_{theme}", (1060, 800))
    box.reject()
