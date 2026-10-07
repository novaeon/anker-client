"""DownloadsPage against FakeContext: rows per job, in-place updates, per-state actions, header controls."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from anker_client.core import events as ev
from anker_client.core.models import DownloadJob, DownloadKind, DownloadOption, ErrorKind, JobState
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.game_dialogs import ImportArchiveDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.pages import downloads as downloads_module
from anker_client.ui.pages.downloads import _STACK_CONTENT, _STACK_EMPTY, DownloadsPage
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets import library_common
from anker_client.ui.widgets.job_row import JobRow

pytestmark = pytest.mark.gui


class RecordingNav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, args))

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a in self.calls if n == name]

    def toasts(self) -> list[str]:
        return [a[0] for a in self.named("toast")]


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
    pages: list[DownloadsPage] = []

    def make() -> DownloadsPage:
        page = DownloadsPage(fake_ctx, bridge, nav, loader)
        qtbot.addWidget(page)
        page.resize(1280, 900)
        page.show()
        pages.append(page)
        qtbot.waitUntil(lambda: page._loaded, timeout=3000)
        return page

    yield SimpleNamespace(ctx=fake_ctx, bridge=bridge, nav=nav, make=make, qtbot=qtbot)
    for page in pages:
        page.shutdown()
        page.close()
    quiesce(fake_ctx)
    bridge.close()
    _KEEP_ALIVE.extend((bridge, loader))


def job_by_title(ctx, title: str) -> DownloadJob:
    return next(j for j in ctx.downloads.jobs() if j.title == title)


def active_titles(page: DownloadsPage) -> list[str]:
    box = page._active_box
    return [box.itemAt(i).widget().job.title for i in range(box.count())]


def history_titles(page: DownloadsPage) -> list[str]:
    box = page._history_box
    return [box.itemAt(i).widget().job.title for i in range(box.count())]


def click(row: JobRow, action: str) -> None:
    btn = row.button_for(action)
    assert btn.isVisibleTo(row) and btn.isEnabled(), action
    btn.click()


# --- structure ------------------------------------------------------------------------------------


def test_one_row_per_job_split_into_sections(env):
    page = env.make()
    jobs = env.ctx.downloads.jobs()
    assert all(isinstance(page.row(j.id), JobRow) for j in jobs)
    assert active_titles(page) == ["Gears of War: E-Day", "Cyberpunk 2077", "Baldur's Gate 3",
                                   "Onimusha: Warlords", "Wasteland 3"]
    assert sorted(history_titles(page)) == ["Hollow Knight", "Wild West Pioneers"]
    assert page._stack.currentIndex() == _STACK_CONTENT
    assert page.active_count.text() == "5 items"


def test_summary_header(env):
    page = env.make()
    assert page.stat_speed.value.text() == "18.4 MB/s"
    assert page.stat_active.value.text() == "2"  # downloading + extracting
    assert page.stat_queued.value.text() == "2"  # queued + waiting
    assert page.stat_eta.value.text() != "—"
    assert "1 paused" in page.summary_text.text() and "1 failed" in page.summary_text.text()
    assert page.pause_all_button.isEnabled() and page.resume_all_button.isEnabled()


def test_row_shows_progress_and_details(env):
    page = env.make()
    row = page.row(job_by_title(env.ctx, "Gears of War: E-Day").id)
    assert row.badge.text() == "Downloading"
    assert 360 <= row.progress.value() <= 380
    assert "MB/s" in row.detail.text() and "left" in row.detail.text()
    assert row.percent.text().endswith("%")
    extracting = page.row(job_by_title(env.ctx, "Wasteland 3").id)
    assert extracting.detail.text() == "Extracting 48%"
    assert extracting.progress.property("state") == "install"
    paused = page.row(job_by_title(env.ctx, "Baldur's Gate 3").id)
    assert paused.progress.property("state") == "paused"


def test_job_updated_updates_existing_row_without_rebuilding(env):
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    row = page.row(job.id)
    rows_before = dict(page._rows)
    for fraction in (0.5, 0.75):
        updated = job.copy()
        updated.bytes_done = int(fraction * (job.bytes_total or 0)) + 1024**2
        env.ctx.events.publish(ev.JobUpdated(updated))
        env.qtbot.waitUntil(lambda f=fraction: abs(row.progress.value() - f * 1000) <= 1, timeout=2000)
    assert page.row(job.id) is row
    assert page._rows == rows_before
    assert row.percent.text() == "75%"


def test_live_ticks_from_running_queue(env):
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    row = page.row(job.id)
    start = row.progress.value()
    env.ctx.start()
    env.qtbot.waitUntil(lambda: row.progress.value() > start, timeout=3000)
    assert page.row(job.id) is row


def test_job_added_and_removed(env):
    page = env.make()
    game = env.ctx.client.games[20]
    option = env.ctx.client.game_details(game.slug).primary_option
    added = env.ctx.downloads.enqueue(slug=game.slug, title=game.title, option=option)
    env.qtbot.waitUntil(lambda: page.row(added.id) is not None, timeout=2000)
    assert game.title in active_titles(page)
    env.ctx.downloads.cancel(added.id)
    env.qtbot.waitUntil(lambda: game.title in history_titles(page), timeout=2000)
    env.ctx.downloads.remove(added.id)
    env.qtbot.waitUntil(lambda: page.row(added.id) is None, timeout=2000)


def test_empty_state(env):
    env.ctx.downloads._jobs.clear()
    page = env.make()
    assert page._stack.currentIndex() == _STACK_EMPTY
    page._empty.action_clicked.emit()
    assert env.nav.named("show_store")


def test_history_is_collapsible(env):
    page = env.make()
    assert page._history_container.isVisible()
    page.history_toggle.click()
    assert not page._history_container.isVisible()
    assert page.history_toggle.text().startswith("History")
    page.history_toggle.click()
    assert page._history_container.isVisible()


# --- per-state actions -------------------------------------------------------------------------------


def test_actions_per_state_visible(env):
    page = env.make()
    expected = {
        "Gears of War: E-Day": ["pause", "cancel"],
        "Cyberpunk 2077": ["pause", "move_up", "move_down", "cancel"],
        "Baldur's Gate 3": ["resume", "cancel"],
        "Onimusha: Warlords": ["retry", "pause", "cancel"],
        "Wild West Pioneers": ["retry", "open_browser", "import_archive", "remove"],
        "Wasteland 3": ["cancel"],
        "Hollow Knight": ["play", "show_in_library", "remove"],
    }
    for title, actions in expected.items():
        row = page.row(job_by_title(env.ctx, title).id)
        assert row.visible_actions() == actions, title
        assert [a for a in actions if row.button_for(a).isVisibleTo(row)] == actions


def test_pause_then_resume(env):
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    row = page.row(job.id)
    click(row, "pause")
    env.qtbot.waitUntil(lambda: row.job.state is JobState.PAUSED, timeout=2000)
    assert row.visible_actions() == ["resume", "cancel"]
    assert env.ctx.downloads.get(job.id).state is JobState.PAUSED
    click(row, "resume")
    env.qtbot.waitUntil(lambda: row.job.state is JobState.DOWNLOADING, timeout=2000)


def test_retry_failed(env):
    page = env.make()
    job = job_by_title(env.ctx, "Wild West Pioneers")
    row = page.row(job.id)
    click(row, "retry")
    env.qtbot.waitUntil(lambda: env.ctx.downloads.get(job.id).state is JobState.QUEUED, timeout=2000)
    env.qtbot.waitUntil(lambda: "Wild West Pioneers" in active_titles(page), timeout=2000)
    assert page.row(job.id) is row


def test_cancel_asks_for_confirmation(env, monkeypatch):
    asked: list[dict[str, Any]] = []
    answer = {"value": False}

    def fake_confirm(parent, **kwargs):
        asked.append(kwargs)
        return answer["value"]

    monkeypatch.setattr(downloads_module, "confirm", fake_confirm)
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    row = page.row(job.id)
    click(row, "cancel")
    assert "Gears of War: E-Day" in asked[0]["text"]
    assert "downloaded so far will be deleted" in asked[0]["informative"]
    env.qtbot.wait(150)
    assert env.ctx.downloads.get(job.id).state is JobState.DOWNLOADING
    answer["value"] = True
    click(row, "cancel")
    env.qtbot.waitUntil(lambda: "Gears of War: E-Day" in history_titles(page), timeout=2000)
    assert env.ctx.downloads.get(job.id).state is JobState.CANCELLED


def test_install_downloaded_job(env):
    env.ctx.downloads._add(env.ctx.client.games[30], JobState.COMPLETED)  # downloaded, not installed
    page = env.make()
    job = job_by_title(env.ctx, env.ctx.client.games[30].title)
    row = page.row(job.id)
    assert job.title in active_titles(page)
    assert row.badge.text() == "Downloaded"
    click(row, "install")
    env.qtbot.waitUntil(lambda: row.job.state is JobState.EXTRACTING, timeout=2000)


def test_remove_finished_job(env):
    page = env.make()
    job = job_by_title(env.ctx, "Hollow Knight")
    click(page.row(job.id), "remove")
    env.qtbot.waitUntil(lambda: page.row(job.id) is None, timeout=2000)
    assert "Hollow Knight" not in history_titles(page)


def test_move_queued_jobs(env):
    downloads = env.ctx.downloads
    downloads._add(env.ctx.client.games[25], JobState.QUEUED)
    downloads._add(env.ctx.client.games[26], JobState.QUEUED)
    page = env.make()
    queued = ["Cyberpunk 2077", env.ctx.client.games[25].title, env.ctx.client.games[26].title]
    assert [t for t in active_titles(page) if t in queued] == queued
    first = page.row(job_by_title(env.ctx, "Cyberpunk 2077").id)
    last = page.row(job_by_title(env.ctx, queued[2]).id)
    assert not first.button_for("move_up").isEnabled() and first.button_for("move_down").isEnabled()
    assert last.button_for("move_up").isEnabled() and not last.button_for("move_down").isEnabled()
    click(first, "move_down")
    expected = [queued[1], "Cyberpunk 2077", queued[2]]
    env.qtbot.waitUntil(lambda: [t for t in active_titles(page) if t in queued] == expected, timeout=2000)
    ordered = [j.title for j in downloads.jobs() if j.title in queued]
    assert ordered == expected
    click(last, "move_up")
    expected = [queued[1], queued[2], "Cyberpunk 2077"]
    env.qtbot.waitUntil(lambda: [t for t in active_titles(page) if t in queued] == expected, timeout=2000)


def test_waiting_countdown(env):
    page = env.make()
    job = job_by_title(env.ctx, "Onimusha: Warlords")
    row = page.row(job.id)
    assert row.detail.text() == "Waiting 42s (rate limited)"
    updated = job.copy()
    updated.retry_at = time.time() + 42
    updated.error_kind = ErrorKind.RATE_LIMITED
    env.ctx.events.publish(ev.JobUpdated(updated))
    env.qtbot.waitUntil(lambda: row.detail.text().startswith("Retrying in 4"), timeout=2000)
    assert "slow down" in row.detail.text()
    assert page._countdown.isActive()
    row.tick(now=updated.retry_at - 5)
    assert row.detail.text().startswith("Retrying in 5s")
    page.on_deactivated()
    assert not page._countdown.isActive()
    page.on_activated()
    assert page._countdown.isActive()


def test_external_host_failure_offers_browser_and_import(env, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(library_common.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    page = env.make()
    job = job_by_title(env.ctx, "Wild West Pioneers")
    row = page.row(job.id)
    assert row.detail.property("role") == "error"
    click(row, "open_browser")
    assert opened == ["https://ankergames.net/download/example"]
    click(row, "import_archive")
    assert isinstance(page._dialog, ImportArchiveDialog)
    assert page._dialog.picker.search.text() == "Wild West Pioneers"
    page._dialog.reject()


def test_completed_job_play_and_show_in_library(env):
    page = env.make()
    row = page.row(job_by_title(env.ctx, "Hollow Knight").id)
    click(row, "show_in_library")
    env.qtbot.waitUntil(lambda: bool(env.nav.named("show_library")), timeout=2000)
    assert env.nav.named("show_library")[0] == ("hollow-knight",)
    click(row, "play")
    env.qtbot.waitUntil(lambda: env.ctx.launcher.is_running("hollow-knight"), timeout=2000)


# --- header controls ---------------------------------------------------------------------------------


def test_pause_all_resume_all_and_clear_finished(env):
    page = env.make()
    page.pause_all_button.click()
    env.qtbot.waitUntil(lambda: page.row(job_by_title(env.ctx, "Gears of War: E-Day").id).job.state
                        is JobState.PAUSED, timeout=2000)
    env.qtbot.waitUntil(lambda: not page.pause_all_button.isEnabled(), timeout=2000)
    page.resume_all_button.click()
    env.qtbot.waitUntil(lambda: not page.resume_all_button.isEnabled(), timeout=2000)
    assert page.clear_button.isEnabled()
    page.clear_button.click()
    env.qtbot.waitUntil(lambda: history_titles(page) == [], timeout=2000)
    assert not page._history_header.isVisible()
    assert not page.clear_button.isEnabled()


def test_speed_limit_presets_and_custom(env, monkeypatch):
    page = env.make()
    assert page.speed_button.text() == "Limit: Unlimited"
    page._speed_actions[5 * 1024].trigger()
    env.qtbot.waitUntil(lambda: env.ctx.settings.get().speed_limit_kbps == 5120, timeout=2000)
    assert page.speed_button.text() == "Limit: 5 MB/s"
    monkeypatch.setattr(page, "_ask_custom_limit", lambda current: 7.5)
    page._custom_action.trigger()
    env.qtbot.waitUntil(lambda: env.ctx.settings.get().speed_limit_kbps == 7680, timeout=2000)
    assert page.speed_button.text() == "Limit: 7.5 MB/s"
    assert page._custom_action.isChecked()
    monkeypatch.setattr(page, "_ask_custom_limit", lambda current: None)  # cancelled dialog
    page._custom_action.trigger()
    assert env.ctx.settings.get().speed_limit_kbps == 7680
    env.ctx.settings.update(speed_limit_kbps=0)  # changed elsewhere (Settings page)
    env.qtbot.waitUntil(lambda: page.speed_button.text() == "Limit: Unlimited", timeout=2000)
    assert page._speed_actions[0].isChecked()


def test_stale_snapshot_never_overrides_newer_event(env):
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    stale = job.copy()
    page._reload()
    seq = page._load_seq
    newer = job.copy()
    newer.state = JobState.COMPLETED
    newer.install_path = "C:/Games/Gears"
    page._on_job_updated(newer)
    page._on_loaded(seq, [stale])
    assert page.row(job.id).job.state is JobState.COMPLETED
    env.qtbot.wait(200)  # the real reload result (same sequence) must not regress it either
    assert page.row(job.id).job.state is JobState.COMPLETED


def test_shutdown_stops_listening(env):
    page = env.make()
    job = job_by_title(env.ctx, "Gears of War: E-Day")
    row = page.row(job.id)
    before = row.progress.value()
    page.shutdown()
    updated = job.copy()
    updated.bytes_done = job.bytes_total or 0
    env.ctx.events.publish(ev.JobUpdated(updated))
    env.ctx.events.publish(ev.QueueChanged())
    env.qtbot.wait(200)
    assert row.progress.value() == before
    assert not page._reload_timer.isActive()


# --- review regressions ------------------------------------------------------------------------------


def shown_buttons(row: JobRow) -> list[str]:
    """Visible action buttons in layout order."""
    layout = row._button_layout
    by_widget = {id(btn): name for name, btn in row._buttons.items()}
    names = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None and widget.isVisibleTo(row):
            names.append(by_widget[id(widget)])
    return names


def recording_confirm(monkeypatch, answer: dict[str, bool]) -> list[dict[str, Any]]:
    asked: list[dict[str, Any]] = []

    def fake_confirm(parent, **kwargs):
        asked.append(kwargs)
        return answer["value"]

    monkeypatch.setattr(downloads_module, "confirm", fake_confirm)
    return asked


def test_row_buttons_are_laid_out_in_action_order(env):
    page = env.make()
    job = job_by_title(env.ctx, "Onimusha: Warlords")
    row = page.row(job.id)
    assert shown_buttons(row) == ["retry", "pause", "cancel"]
    assert row.button_for("retry").text() == "Retry now"
    paused = job.copy()
    paused.state = JobState.PAUSED
    env.ctx.events.publish(ev.JobUpdated(paused))
    env.qtbot.waitUntil(lambda: shown_buttons(row) == ["resume", "cancel"], timeout=2000)


def test_clear_finished_keeps_downloads_waiting_for_install(env):
    ready = env.ctx.downloads._add(env.ctx.client.games[30], JobState.COMPLETED)  # downloaded, not installed
    page = env.make()
    assert ready.title in active_titles(page)
    page.clear_button.click()
    env.qtbot.waitUntil(lambda: history_titles(page) == [], timeout=2000)
    env.qtbot.wait(150)
    assert env.ctx.downloads.get(ready.id) is not None
    assert page.row(ready.id) is not None and ready.title in active_titles(page)


def test_clear_finished_uses_the_bulk_call_when_nothing_waits_for_install(env, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(env.ctx.downloads, "clear_finished", lambda: calls.append(1))
    page = env.make()
    page.clear_button.click()
    env.qtbot.waitUntil(lambda: calls == [1], timeout=2000)


def test_clear_finished_confirms_before_deleting_partial_data(env, monkeypatch):
    failed = job_by_title(env.ctx, "Wild West Pioneers")
    env.ctx.downloads._jobs[failed.id].bytes_done = 3 * 1024**3
    answer = {"value": False}
    asked = recording_confirm(monkeypatch, answer)
    page = env.make()
    page.clear_button.click()
    assert "3.00 GB" in asked[0]["informative"] and "Wild West Pioneers" in asked[0]["informative"]
    env.qtbot.wait(150)
    assert len(history_titles(page)) == 2
    answer["value"] = True
    page.clear_button.click()
    env.qtbot.waitUntil(lambda: history_titles(page) == [], timeout=2000)


def test_remove_failed_download_with_data_asks_first(env, monkeypatch):
    failed = job_by_title(env.ctx, "Wild West Pioneers")
    env.ctx.downloads._jobs[failed.id].bytes_done = 512 * 1024**2
    answer = {"value": False}
    asked = recording_confirm(monkeypatch, answer)
    page = env.make()
    row = page.row(failed.id)
    click(row, "remove")
    assert "Wild West Pioneers" in asked[0]["text"] and "512 MB" in asked[0]["informative"]
    env.qtbot.wait(150)
    assert page.row(failed.id) is not None
    answer["value"] = True
    click(row, "remove")
    env.qtbot.waitUntil(lambda: page.row(failed.id) is None, timeout=2000)


def test_install_failure_offers_retry_install(env):
    failed = env.ctx.downloads._add(env.ctx.client.games[31], JobState.FAILED)
    failed.error = "The archive could not be extracted."
    failed.error_kind = ErrorKind.EXTRACTION
    failed.archive_path = r"C:\Games\.ankerclient\downloads\job\game.zip"
    page = env.make()
    row = page.row(failed.id)
    assert row.visible_actions() == ["retry", "remove"]
    assert row.button_for("retry").text() == "Retry install"
    click(row, "retry")  # retry() re-installs from the archive, or downloads again when it was lost
    env.qtbot.waitUntil(lambda: env.ctx.downloads.get(failed.id).state is JobState.QUEUED, timeout=2000)


def test_import_archive_from_a_failed_patch_keeps_its_kind(env):
    job = env.ctx.downloads._add(env.ctx.client.games[0], JobState.FAILED)
    job.option = DownloadOption(3000, "Update Only From V 1.0.0 To V 1.1.0", DownloadKind.PATCH)
    job.error_kind = ErrorKind.EXTERNAL_HOST
    job.error = "Hosted elsewhere."
    page = env.make()
    click(page.row(job.id), "import_archive")
    assert page._dialog.selected_kind() is DownloadKind.PATCH  # never re-installed as a full game
    page._dialog.reject()


def test_snapshot_queue_positions_apply_to_jobs_updated_meanwhile(env):
    downloads = env.ctx.downloads
    second = downloads._add(env.ctx.client.games[25], JobState.QUEUED)
    page = env.make()
    first = job_by_title(env.ctx, "Cyberpunk 2077")
    queued = ("Cyberpunk 2077", second.title)
    assert [t for t in active_titles(page) if t in queued] == list(queued)
    page._reload()
    seq = page._load_seq
    # The snapshot carries a reorder (reorders publish QueueChanged, never per-job events)...
    snapshot = downloads.jobs()
    a = next(j for j in snapshot if j.id == first.id)
    b = next(j for j in snapshot if j.id == second.id)
    a.position, b.position = b.position, a.position
    # ...while a progress event for the moved job, carrying its old position, arrived meanwhile.
    tick = page.row(first.id).job.copy()
    tick.status_text = "still queued"
    page._on_job_updated(tick)
    page._on_loaded(seq, snapshot)
    assert [t for t in active_titles(page) if t in queued] == [second.title, "Cyberpunk 2077"]
    assert page._jobs[first.id].status_text == "still queued"  # the newer event's data is kept
