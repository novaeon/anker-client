"""StartupTasks: isolated background chain + periodic update checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from PyQt6.QtCore import QObject

from anker_client.services import legacy
from anker_client.services.legacy import MigrationReport
from anker_client.ui.shell_startup import (
    STEP_AUTH,
    STEP_CATALOG,
    STEP_GAME_UPDATES,
    STEP_IMAGES,
    STEP_LIBRARY,
    STEP_MIGRATION,
    StartupTasks,
    is_due,
    parse_iso,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def recorder(fake_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()

    def migrate(paths: Any, db: Any, library: Any, images: Any) -> MigrationReport:
        rec.calls.append("migrate")
        return MigrationReport(adopted=["a"])

    monkeypatch.setattr(legacy, "migrate", migrate)
    original_scan = fake_ctx.library.scan
    monkeypatch.setattr(fake_ctx.library, "scan", lambda *, token=None: (rec.calls.append("scan"),
                                                                         original_scan(token=token))[1])
    monkeypatch.setattr(fake_ctx.auth, "restore", lambda *, token=None: rec.calls.append("restore"))
    monkeypatch.setattr(fake_ctx.catalog, "needs_sync", lambda max_age: rec.calls.append("needs_sync") or True)
    monkeypatch.setattr(fake_ctx.catalog, "sync",
                        lambda *, full=False, token, on_progress=None: rec.calls.append(f"sync:{full}") or 0)
    monkeypatch.setattr(fake_ctx.images, "prune", lambda: rec.calls.append("prune") or 0)
    monkeypatch.setattr(fake_ctx.updates, "last_checked", lambda: "")
    monkeypatch.setattr(fake_ctx.updates, "check",
                        lambda *, token, install_ids=None, on_progress=None: rec.calls.append("updates") or [])
    monkeypatch.setattr(fake_ctx.app_updates, "check", lambda *, token=None: rec.calls.append("app_update"))
    return rec


@pytest.fixture
def owner(qapp: Any) -> QObject:
    obj = QObject()
    yield obj
    obj.deleteLater()


def _tasks(fake_ctx: Any, owner: QObject, **kwargs: Any) -> StartupTasks:
    kwargs.setdefault("clock", lambda: NOW)
    return StartupTasks(fake_ctx, owner, **kwargs)


def test_parse_and_due() -> None:
    assert parse_iso("") is None and parse_iso("garbage") is None
    assert parse_iso("2026-10-06T10:00:00Z") == datetime(2026, 10, 6, 10, tzinfo=UTC)
    assert parse_iso("2026-10-06T10:00:00").tzinfo is UTC
    assert is_due("", timedelta(hours=6), now=NOW)
    assert is_due("2026-10-06T05:00:00+00:00", timedelta(hours=6), now=NOW)
    assert not is_due("2026-10-06T07:00:00+00:00", timedelta(hours=6), now=NOW)
    assert is_due("2027-01-01T00:00:00+00:00", timedelta(hours=6), now=NOW)  # clock went backwards


def test_chain_runs_in_order_then_checks_updates(qtbot: Any, fake_ctx: Any, owner: QObject,
                                                 recorder: Recorder) -> None:
    tasks = _tasks(fake_ctx, owner)
    with qtbot.waitSignal(tasks.chain_finished, timeout=5000) as blocker:
        tasks.start()
    results = blocker.args[0]
    assert [r.name for r in results] == [STEP_MIGRATION, STEP_LIBRARY, STEP_AUTH, STEP_CATALOG, STEP_IMAGES]
    assert all(r.ok for r in results)
    chain = [c for c in recorder.calls if c not in ("app_update", "updates")]
    assert chain == ["migrate", "scan", "restore", "needs_sync", "sync:False", "prune"]
    qtbot.waitUntil(lambda: "updates" in recorder.calls, timeout=3000)
    assert recorder.calls.count("app_update") == 1
    tasks.stop()


def test_failing_step_is_isolated(qtbot: Any, fake_ctx: Any, owner: QObject, recorder: Recorder,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any) -> None:
        raise NotImplementedError("not built yet")

    def broken_scan(*, token: Any = None) -> None:
        raise OSError("disk unplugged")

    monkeypatch.setattr(legacy, "migrate", broken)
    monkeypatch.setattr(fake_ctx.library, "scan", broken_scan)
    tasks = _tasks(fake_ctx, owner)
    failures: list[tuple[str, BaseException]] = []
    tasks.step_failed.connect(lambda name, exc: failures.append((name, exc)))
    with qtbot.waitSignal(tasks.chain_finished, timeout=5000) as blocker:
        tasks.start()
    results = {r.name: r for r in blocker.args[0]}
    assert not results[STEP_MIGRATION].ok and not results[STEP_LIBRARY].ok
    assert results[STEP_AUTH].ok and results[STEP_CATALOG].ok and results[STEP_IMAGES].ok
    assert "prune" in recorder.calls
    qtbot.waitUntil(lambda: len(failures) == 2, timeout=2000)
    assert [name for name, _ in failures] == [STEP_MIGRATION, STEP_LIBRARY]
    assert isinstance(failures[1][1], OSError)
    tasks.stop()


def test_catalog_sync_skipped_when_fresh(qtbot: Any, fake_ctx: Any, owner: QObject, recorder: Recorder,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    ages: list[timedelta] = []
    monkeypatch.setattr(fake_ctx.catalog, "needs_sync", lambda max_age: ages.append(max_age) or False)
    fake_ctx.settings.update(catalog_sync_interval_hours=12)
    tasks = _tasks(fake_ctx, owner)
    with qtbot.waitSignal(tasks.chain_finished, timeout=5000) as blocker:
        tasks.start()
    catalog = next(r for r in blocker.args[0] if r.name == STEP_CATALOG)
    assert catalog.ok and catalog.skipped
    assert ages == [timedelta(hours=12)]
    assert not any(c.startswith("sync") for c in recorder.calls)
    tasks.stop()


def test_already_migrated_counts_as_skipped(qtbot: Any, fake_ctx: Any, owner: QObject, recorder: Recorder,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy, "migrate", lambda *_a: MigrationReport(already_done=True))
    tasks = _tasks(fake_ctx, owner)
    with qtbot.waitSignal(tasks.chain_finished, timeout=5000) as blocker:
        tasks.start()
    assert blocker.args[0][0].skipped
    tasks.stop()


def test_game_update_check_respects_interval_and_setting(qtbot: Any, fake_ctx: Any, owner: QObject,
                                                         recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = _tasks(fake_ctx, owner)
    monkeypatch.setattr(fake_ctx.updates, "last_checked", (NOW - timedelta(hours=1)).isoformat)
    assert not tasks.check_game_updates()  # checked an hour ago, interval 6 h
    fake_ctx.settings.update(game_update_interval_hours=1)
    assert tasks.check_game_updates()
    qtbot.waitUntil(lambda: recorder.calls.count("updates") == 1, timeout=2000)
    # last_checked() did not move (e.g. the check failed): our own attempt time still throttles
    assert not tasks.check_game_updates()
    fake_ctx.settings.update(check_game_updates=False)
    qtbot.wait(20)
    assert not tasks.check_game_updates()
    assert tasks.check_game_updates(force=True)
    qtbot.waitUntil(lambda: recorder.calls.count("updates") == 2, timeout=2000)
    tasks.stop()


def test_update_check_failure_is_reported(qtbot: Any, fake_ctx: Any, owner: QObject, recorder: Recorder,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*, token: Any, install_ids: Any = None, on_progress: Any = None) -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(fake_ctx.updates, "check", fail)
    tasks = _tasks(fake_ctx, owner)
    with qtbot.waitSignal(tasks.step_failed, timeout=3000) as blocker:
        tasks.check_game_updates(force=True)
    assert blocker.args[0] == STEP_GAME_UPDATES
    tasks.stop()


def test_app_update_check_daily_and_optional(qtbot: Any, fake_ctx: Any, owner: QObject, recorder: Recorder) -> None:
    now = [NOW]
    tasks = _tasks(fake_ctx, owner, clock=lambda: now[0])
    assert tasks.check_app_update()
    qtbot.waitUntil(lambda: recorder.calls.count("app_update") == 1, timeout=2000)
    tasks.check_periodic()  # chain not done, app check not due yet
    qtbot.wait(50)
    assert recorder.calls.count("app_update") == 1
    now[0] = NOW + timedelta(days=1, minutes=1)
    tasks.check_periodic()
    qtbot.waitUntil(lambda: recorder.calls.count("app_update") == 2, timeout=2000)
    fake_ctx.settings.update(check_app_updates=False)
    assert not tasks.check_app_update()
    tasks.stop()


def test_stop_prevents_new_work(fake_ctx: Any, owner: QObject, recorder: Recorder) -> None:
    tasks = _tasks(fake_ctx, owner)
    tasks.stop()
    tasks.start()
    assert not tasks.check_game_updates(force=True)
    assert not tasks.check_app_update()
    assert not tasks.chain_done
