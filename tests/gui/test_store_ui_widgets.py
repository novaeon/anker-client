"""store_ui building blocks: the action-card state machine (pure), text helpers, discover-row wheel
handling, chip layout, carousel and lightbox — without the full FakeContext."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QWheelEvent
from PyQt6.QtWidgets import QApplication, QPushButton

from anker_client.core.models import (
    DownloadJob,
    DownloadKind,
    DownloadOption,
    GameDetails,
    GameSummary,
    InstalledGame,
    JobState,
    SortOrder,
)
from anker_client.core.tasks import CancelToken, TaskRunner
from anker_client.ui.dialogs.lightbox import LightboxDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.carousel import ScreenshotCarousel
from anker_client.ui.widgets.cover_grid import CoverItem
from anker_client.ui.widgets.discover_row import DiscoverRow, WheelLatch
from anker_client.ui.widgets.game_actions import (
    ActionKind,
    GameActionCard,
    derive_action_state,
    find_patch_option,
    job_detail_text,
    job_headline,
    update_button_text,
    update_versions,
)
from anker_client.ui.widgets.game_common import (
    ChipFlow,
    display_version,
    format_date,
    genre_slug,
    parse_color,
    relative_day,
)
from anker_client.ui.widgets.game_sections import option_title
from anker_client.ui.widgets.store_cards import is_nsfw, job_badge, summary_item, summary_subtitle
from anker_client.ui.widgets.store_discover import section_sort

FULL = DownloadOption(1, "Direct", DownloadKind.FULL)
FULL_V2 = DownloadOption(2, "Direct V 2.0", DownloadKind.FULL)
PATCH = DownloadOption(3, "Update Only From V 1.0 To V 2.0 (120 MB)", DownloadKind.PATCH, "120 MB", "1.0", "2.0")
OLD_PATCH = DownloadOption(4, "Update Only From V 0.9 To V 1.0", DownloadKind.PATCH, "", "0.9", "1.0")
ADDON = DownloadOption(5, "Language Pack (1.2 GB)", DownloadKind.ADDON, "1.2 GB")


def details(*options: DownloadOption, version: str = "v2.0", size: str = "10 GB") -> GameDetails:
    return GameDetails(slug="g", title="Game", version=version, size_text=size, download_options=list(options))


def installed(version: str = "v1.0", *, update: bool = False, exe: str = "game.exe", managed: bool = True,
              latest: str = "") -> InstalledGame:
    return InstalledGame(install_id="g", title="Game", path="C:/Games/Game", library_root="C:/Games", slug="g",
                         version=version, executable=exe, managed=managed, update_available=update,
                         latest_version=latest)


def job(state: JobState, done: int = 250, total: int | None = 1000, **extra: Any) -> DownloadJob:
    return DownloadJob(id="j", slug="g", title="Game", option=FULL, library_root="C:/Games", state=state,
                       bytes_done=done, bytes_total=total, **extra)


# --- pure state machine ------------------------------------------------------------------------


def test_state_priority_order() -> None:
    d = details(FULL, PATCH)
    assert derive_action_state(d, installed(), job(JobState.DOWNLOADING), running=True).kind is ActionKind.RUNNING
    assert derive_action_state(d, installed(), job(JobState.PAUSED)).kind is ActionKind.JOB
    assert derive_action_state(d, None, job(JobState.QUEUED)).kind is ActionKind.JOB
    assert derive_action_state(d, None, job(JobState.COMPLETED)).kind is ActionKind.INSTALL  # finished jobs ignored
    assert derive_action_state(d, installed("v2.0")).kind is ActionKind.PLAY
    assert derive_action_state(d, installed("v2.0", exe="")).kind is ActionKind.SETUP
    assert derive_action_state(None, None, None, details_loading=True).kind is ActionKind.LOADING
    assert derive_action_state(None, None, None, details_loading=False).kind is ActionKind.UNAVAILABLE
    assert derive_action_state(details(), None, None).kind is ActionKind.UNAVAILABLE
    # running without an install record can't be stopped from here
    assert derive_action_state(d, None, None, running=True).kind is ActionKind.INSTALL


def test_install_state_uses_primary_full_option_and_size() -> None:
    state = derive_action_state(details(ADDON, FULL, PATCH), None, None)
    assert state.kind is ActionKind.INSTALL
    assert state.option == FULL and state.size_text == "10 GB"
    assert state.options == (ADDON, FULL, PATCH)


def test_update_prefers_matching_patch() -> None:
    state = derive_action_state(details(FULL, OLD_PATCH, PATCH), installed("V 1.0", update=True), None)
    assert state.kind is ActionKind.UPDATE
    assert state.option == PATCH and state.is_patch and state.size_text == "120 MB"
    assert (state.installed_version, state.latest_version) == ("V 1.0", "v2.0")


def test_update_without_matching_patch_uses_full_download() -> None:
    state = derive_action_state(details(FULL, OLD_PATCH), installed("v1.5", update=True), None)
    assert state.option == FULL and not state.is_patch and state.size_text == "10 GB"


def test_update_never_offers_an_addon_and_waits_for_details() -> None:
    assert derive_action_state(details(ADDON), installed(update=True), None).option is None
    waiting = derive_action_state(None, installed(update=True, latest="v3"), None, details_loading=True)
    assert waiting.kind is ActionKind.UPDATE and waiting.option is None and waiting.latest_version == "v3"


def test_update_detection_rules() -> None:
    assert update_versions(details(version="v2.0"), installed("v1.0")) == (True, "v2.0")
    assert update_versions(details(version="v2.0"), installed("v2.0")) == (False, "v2.0")
    assert update_versions(details(version="v1.0"), installed("v2.0")) == (False, "v1.0")  # installed is newer
    assert update_versions(details(version="v2.0"), installed("")) == (False, "v2.0")  # unknown installed version
    assert update_versions(details(version="v2.0"), installed("v1.0", managed=False)) == (False, "")
    assert update_versions(None, installed("v1.0", update=True, latest="v1.1")) == (True, "v1.1")
    # date-based detection (a version unknown) still trusts the update checker's flag
    assert update_versions(details(version=""), installed("v1.0", update=True)) == (True, "")
    assert update_versions(details(version="v2.0"), installed("", update=True)) == (True, "v2.0")


def test_stale_update_flag_never_offers_the_installed_version() -> None:
    # The checker flagged an update earlier, but the fresh page lists the installed version.
    stale = installed("v2.0", update=True, latest="v2.0")
    assert update_versions(details(FULL, version="v2.0"), stale) == (False, "v2.0")
    assert derive_action_state(details(FULL, version="v2.0"), stale, None).kind is ActionKind.PLAY


def test_update_button_text() -> None:
    assert update_button_text("1.0", "V 2.0") == "Update v1.0 → v2.0"
    assert update_button_text("", "v2.0") == "Update to v2.0"
    assert update_button_text("v1.0", "") == "Update"
    assert update_button_text("", "") == "Update"


def test_find_patch_option() -> None:
    d = details(FULL, OLD_PATCH, PATCH)
    assert find_patch_option(d, "1.0") == PATCH
    assert find_patch_option(d, "v0.9") == OLD_PATCH
    assert find_patch_option(d, "1.5") is None
    assert find_patch_option(d, "") is None
    assert find_patch_option(None, "1.0") is None


def test_job_texts() -> None:
    downloading = job(JobState.DOWNLOADING, speed_bps=2 * 1024**2, eta_seconds=125)
    assert job_headline(downloading) == "Downloading 25%"
    assert job_detail_text(downloading) == "250 B of 1000 B · 2.00 MB/s · 2m 05s left"
    assert job_badge(downloading) == ("Downloading 25%", "accent", 0.25)
    assert job_detail_text(job(JobState.WAITING, retry_at=142.0), now=lambda: 100.0) == "Retrying in 42s"
    assert job_badge(job(JobState.PAUSED, done=0)) == ("Paused", "", None)
    assert job_badge(job(JobState.QUEUED)) == ("Queued", "", None)
    extracting = job(JobState.EXTRACTING, phase_progress=0.5)
    assert job_badge(extracting)[0] == "Installing 50%" and job_headline(extracting) == "Installing 50%"
    assert job_detail_text(job(JobState.QUEUED)) == "Waiting for other downloads to finish"


def test_waiting_countdown_replaces_the_managers_static_one() -> None:
    def waiting(status: str) -> str:
        return job_detail_text(job(JobState.WAITING, retry_at=142.0, status_text=status), now=lambda: 100.0)

    assert waiting("Retry 2 of 5 in 42s") == "Retrying in 42s · Retry 2 of 5"
    assert waiting("Retry 1 of 5 in 1m 05s") == "Retrying in 42s · Retry 1 of 5"
    assert waiting("Waiting 42s (rate limited)") == "Retrying in 42s · Rate limited"
    assert waiting("Rate limited by AnkerGames") == "Retrying in 42s · Rate limited by AnkerGames"
    assert waiting("") == "Retrying in 42s"


# --- helpers -------------------------------------------------------------------------------------


def test_text_helpers() -> None:
    assert genre_slug("Open World") == "open-world"
    assert genre_slug(" RPG ") == "rpg"
    assert display_version("V 1.5.2") == "v1.5.2"
    assert display_version("") == ""
    assert display_version("Build alpha") == "Build alpha"
    assert format_date("2017-03-04") == "4 Mar 2017"
    assert format_date("garbage") == "garbage"
    today = date(2026, 10, 6)
    assert relative_day("2026-10-06", today=today) == "today"
    assert relative_day("2026-10-05", today=today) == "yesterday"
    assert relative_day("2026-10-01", today=today) == "5 days ago"
    assert relative_day("2026-08-01", today=today) == "2 months ago"
    assert relative_day("2020-01-01", today=today) == "1 Jan 2020"
    assert option_title(PATCH) == "Update Only From V 1.0 To V 2.0"
    assert option_title(FULL) == "Direct"


def test_parse_color() -> None:
    assert parse_color("rgba(6, 8, 12, 0.5)").getRgb() == (6, 8, 12, 128)
    assert parse_color("#ff0000").getRgb() == (255, 0, 0, 255)
    assert parse_color("not a colour", QColor(1, 2, 3)).getRgb() == (1, 2, 3, 255)


def test_section_sort_mapping() -> None:
    assert section_sort("Latest Games") is SortOrder.NEWEST
    assert section_sort("Trending Games") is SortOrder.MOST_VIEWED
    assert section_sort("Top games") is SortOrder.TOP_RATED
    assert section_sort("Most Liked") is SortOrder.MOST_LIKED
    assert section_sort("Upcoming Games") is None
    assert section_sort("Masterpiece Collection") is None


def test_summary_items_and_nsfw() -> None:
    g = GameSummary("x", "X", cover_url="c", primary_genre="Action", year=2020, size_text="1 GB")
    assert summary_subtitle(g) == "Action · 1 GB"
    assert summary_subtitle(GameSummary("y", "Y", primary_genre="RPG", year=2019)) == "RPG · 2019"
    item = summary_item(g)
    assert (item.key, item.title, item.cover_url, item.payload) == ("x", "X", "c", g)
    assert is_nsfw(GameSummary("n", "N", primary_genre="NSFW")) and not is_nsfw(g)


# --- Qt widgets (no FakeContext) ----------------------------------------------------------------------


class _NoImages:
    def fetch(self, url: str, *, token: CancelToken | None = None) -> Path:
        raise FileNotFoundError(url)


@pytest.fixture
def loader(qtbot: Any) -> Iterator[ImageLoader]:
    ThemeManager(QApplication.instance()).apply("midnight")
    runner = TaskRunner(max_workers=2, name="store-ui-test")
    yield ImageLoader(_NoImages(), runner)  # type: ignore[arg-type]
    runner.shutdown(wait=False)


def test_action_card_names_patch_and_addon_jobs_on_their_own_line(qtbot: Any) -> None:
    ThemeManager(QApplication.instance()).apply("midnight")
    card = GameActionCard()
    qtbot.addWidget(card)
    card.setFixedWidth(340)  # the Game page's side column
    card.show()
    patch_job = job(JobState.DOWNLOADING)
    patch_job.option = PATCH
    card.set_state(derive_action_state(details(FULL, PATCH), installed(), patch_job))
    assert card.job_title.text() == "Downloading 25%"
    assert card.job_option.text() == "Update Only From V 1.0 To V 2.0" and not card.job_option.isHidden()
    assert card.job_option.wordWrap()
    assert card.minimumSizeHint().width() <= 340  # nothing forces the card wider than its column

    card.set_state(derive_action_state(details(FULL), None, job(JobState.DOWNLOADING)))
    assert card.job_option.isHidden()


def _wheel(widget: Any, dy: int, dx: int = 0) -> QWheelEvent:
    pos = QPointF(widget.rect().center())
    return QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(), QPoint(dx, dy),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def test_discover_row_wheel_scrolls_horizontally_and_passes_through(qtbot: Any, loader: ImageLoader) -> None:
    now = [100.0]
    latch = WheelLatch(clock=lambda: now[0])
    row = DiscoverRow("Trending", loader, latch=latch, see_all=True)
    qtbot.addWidget(row)
    row.set_items([CoverItem(key=str(i), title=f"Game {i}") for i in range(15)])
    row.resize(800, 420)
    row.show()
    qtbot.waitExposed(row)
    view = row.view
    bar = view.horizontalScrollBar()
    assert not view.isWrapping() and bar.maximum() > 0
    assert view.height() == view.gridSize().height() + bar.sizeHint().height() + 4
    assert row.count_label.text() == "15"
    assert not row.prev_button.isEnabled() and row.next_button.isEnabled()

    down = _wheel(view.viewport(), -120)
    view.wheelEvent(down)  # vertical wheel scrolls the strip sideways
    assert down.isAccepted() and bar.value() == 120

    up_at_start = _wheel(view.viewport(), 120)
    bar.setValue(0)
    view.wheelEvent(up_at_start)  # at the start: let the page scroll instead
    assert not up_at_start.isAccepted() and bar.value() == 0

    latch.touch()  # the page itself is being scrolled right now
    during_page_scroll = _wheel(view.viewport(), -120)
    view.wheelEvent(during_page_scroll)
    assert not during_page_scroll.isAccepted() and bar.value() == 0
    sideways = _wheel(view.viewport(), 0, dx=-120)  # horizontal gestures always scroll the strip
    view.wheelEvent(sideways)
    assert sideways.isAccepted() and bar.value() == 120
    now[0] += 1.0
    later = _wheel(view.viewport(), -120)
    view.wheelEvent(later)
    assert later.isAccepted() and bar.value() == 240

    row.scroll_page(1)
    qtbot.waitUntil(lambda: bar.value() > 240, timeout=2000)
    with qtbot.waitSignal(row.see_all_clicked, timeout=1000):
        row.see_all_button.click()


def test_chip_flow_lays_out_every_chip(qtbot: Any) -> None:
    ThemeManager(QApplication.instance()).apply("midnight")
    flow = ChipFlow(spacing=6)
    qtbot.addWidget(flow)
    flow.resize(300, 120)
    flow.show()
    for name in ("All", "Action", "Adventure", "Open World", "VR"):
        chip = QPushButton(name)
        chip.setProperty("variant", "chip")
        flow.add(chip)
    qtbot.waitUntil(lambda: all(c.width() < 300 for c in flow.chips()), timeout=2000)
    rects = [c.geometry() for c in flow.chips()]
    assert all(r.right() <= 300 for r in rects)
    assert len({(r.x(), r.y()) for r in rects}) == 5  # no overlapping chips
    flow.clear()
    assert flow.chips() == []


def test_carousel_navigation_and_lightbox(qtbot: Any, loader: ImageLoader) -> None:
    carousel = ScreenshotCarousel(loader)
    qtbot.addWidget(carousel)
    carousel.resize(640, 500)
    carousel.show()
    urls = [f"https://fake.invalid/shot/{i}.png" for i in range(3)]
    with qtbot.waitSignal(carousel.current_changed):
        carousel.set_images(urls)
    assert carousel.current_index == 0 and len(carousel.thumbs()) == 3
    assert carousel.main.height() == min(ScreenshotCarousel.MAX_MAIN_HEIGHT, round(640 * 9 / 16))
    carousel.next()
    carousel.next()
    carousel.next()
    assert carousel.current_index == 0  # wraps
    carousel.main.next_button.click()
    assert carousel.current_index == 1
    with qtbot.waitSignal(carousel.image_activated) as activated:
        qtbot.keyClick(carousel, Qt.Key.Key_Return)
    assert activated.args == [1]

    carousel.set_images(urls[:1])  # a single image: no strip, no arrows
    assert carousel.strip.isHidden() and carousel.main.next_button.isHidden()
    carousel.set_images([])
    assert carousel.current_index == -1

    box = LightboxDialog(loader, urls, 2, title="Game")
    qtbot.addWidget(box)
    box.resize(900, 600)
    box.show()
    assert box.current_url() == urls[2]
    box.next()
    assert box.current_index == 0
    qtbot.keyClick(box, Qt.Key.Key_End)
    assert box.current_index == 2
    qtbot.waitUntil(lambda: loader.is_failed(urls[2]), timeout=2000)
    box.repaint()  # "Image unavailable" path paints without errors
    with qtbot.waitSignal(box.rejected, timeout=1000):
        qtbot.mouseClick(box, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))  # backdrop click closes
