"""Background work started once the window is up (docs/ARCHITECTURE.md §Startup, step 8).

Startup chain (one pool task, steps run in order, each isolated — a failing
step is logged and reported through ``step_failed`` and the next one still runs):

1. legacy migration (``services.legacy.migrate``)
2. library scan
3. sign-in restore (``auth.restore``)
4. catalog sync when older than ``catalog_sync_interval_hours`` (incremental)
5. image-cache prune

Periodic checks (``QTimer``, re-evaluated every ``POLL_INTERVAL_MS`` so
settings changes and sleep/resume are honoured):

* game updates when ``check_game_updates`` and both the last completed check
  and our last attempt are older than ``game_update_interval_hours`` (first
  evaluation right after the chain);
* AnkerClient updates when ``check_app_updates``: once at startup, then daily.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from anker_client.core.errors import OperationCancelled
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.ui.async_ import run_async

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 15 * 60 * 1000
APP_UPDATE_INTERVAL = timedelta(days=1)

STEP_MIGRATION = "legacy migration"
STEP_LIBRARY = "library scan"
STEP_AUTH = "sign-in restore"
STEP_CATALOG = "catalog sync"
STEP_IMAGES = "image cache prune"
STEP_GAME_UPDATES = "game update check"
STEP_APP_UPDATES = "app update check"


@dataclass(slots=True)
class StepResult:
    name: str
    ok: bool
    error: BaseException | None = None
    skipped: bool = False


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def is_due(last_iso: str | None, interval: timedelta, *, now: datetime | None = None) -> bool:
    last = parse_iso(last_iso)
    if last is None:
        return True
    now = now or datetime.now(UTC)
    return now - last >= interval or last > now + timedelta(minutes=5)  # clock jumped backwards


class StartupTasks(QObject):
    step_finished = pyqtSignal(object)  # StepResult
    step_failed = pyqtSignal(str, object)  # step name, exception
    chain_finished = pyqtSignal(object)  # list[StepResult]

    def __init__(self, ctx: Any, parent: QObject | None = None, *, poll_interval_ms: int = POLL_INTERVAL_MS,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._clock = clock
        self._handles: list[TaskHandle[Any]] = []
        self._game_check: TaskHandle[Any] | None = None
        self._app_check: TaskHandle[Any] | None = None
        self._last_app_check: datetime | None = None
        self._last_game_attempt: datetime | None = None
        self._chain_done = False
        self._stopped = False
        self.results: list[StepResult] = []
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self.check_periodic)

    # --- lifecycle --------------------------------------------------------------------------------
    def start(self) -> None:
        if self._stopped:
            return
        self._submit(self._run_chain, on_result=self._on_chain_finished)
        self.check_app_update()
        self._timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        for handle in self._handles:
            handle.cancel("shutdown")
        self._handles.clear()

    @property
    def chain_done(self) -> bool:
        return self._chain_done

    # --- chain ------------------------------------------------------------------------------------
    def steps(self) -> list[tuple[str, Callable[[CancelToken], Any]]]:
        return [
            (STEP_MIGRATION, self._migrate),
            (STEP_LIBRARY, self._scan_library),
            (STEP_AUTH, self._restore_auth),
            (STEP_CATALOG, self._sync_catalog),
            (STEP_IMAGES, self._prune_images),
        ]

    def _run_chain(self, *, token: CancelToken) -> list[StepResult]:
        results: list[StepResult] = []
        for name, step in self.steps():
            token.raise_if_cancelled()
            started = time.monotonic()
            try:
                outcome = step(token)
            except OperationCancelled:
                raise
            except Exception as exc:
                log.exception("Startup step '%s' failed", name)
                result = StepResult(name, ok=False, error=exc)
            else:
                skipped = outcome is _SKIPPED
                log.info("Startup step '%s' %s in %.1fs", name, "skipped" if skipped else "done",
                         time.monotonic() - started)
                result = StepResult(name, ok=True, skipped=skipped)
            results.append(result)
            self._emit_step(result)
        return results

    def _emit_step(self, result: StepResult) -> None:
        # Called on the worker thread; signals of a QObject owned by the GUI thread are queued there.
        try:
            self.step_finished.emit(result)
            if not result.ok:
                self.step_failed.emit(result.name, result.error)
        except RuntimeError:  # deleted during shutdown
            pass

    def _on_chain_finished(self, results: list[StepResult]) -> None:
        self.results = results
        self._chain_done = True
        self.chain_finished.emit(results)
        self.check_periodic()

    # --- steps (worker thread) ----------------------------------------------------------------------
    def _migrate(self, token: CancelToken) -> Any:
        from anker_client.services import legacy

        report = legacy.migrate(self._ctx.paths, self._ctx.db, self._ctx.library, self._ctx.images)
        if getattr(report, "already_done", False):
            return _SKIPPED
        log.info("Legacy migration: %d games adopted, %d covers imported",
                 len(getattr(report, "adopted", []) or []), getattr(report, "covers_imported", 0))
        return report

    def _scan_library(self, token: CancelToken) -> Any:
        return self._ctx.library.scan(token=token)

    def _restore_auth(self, token: CancelToken) -> Any:
        # restore() honours remember_login itself (cookies + keyring); it never raises for network errors.
        return self._ctx.auth.restore(token=token)

    def _sync_catalog(self, token: CancelToken) -> Any:
        hours = self._ctx.settings.get().catalog_sync_interval_hours
        if not self._ctx.catalog.needs_sync(timedelta(hours=hours)):
            return _SKIPPED
        return self._ctx.catalog.sync(full=False, token=token)

    def _prune_images(self, token: CancelToken) -> Any:
        return self._ctx.images.prune()

    # --- periodic checks ------------------------------------------------------------------------------
    def check_periodic(self) -> None:
        if self._stopped:
            return
        if self._chain_done:
            self.check_game_updates()
        if self._last_app_check is None or self._clock() - self._last_app_check >= APP_UPDATE_INTERVAL:
            self.check_app_update()

    def check_game_updates(self, *, force: bool = False) -> bool:
        """Start a game-update check when due (or ``force``). Returns True when one was started."""
        if self._stopped or (self._game_check is not None and not self._game_check.done()):
            return False
        settings = self._ctx.settings.get()
        if not force and not settings.check_game_updates:
            return False
        now = self._clock()
        if not force:
            interval = timedelta(hours=settings.game_update_interval_hours)
            try:
                last = self._ctx.updates.last_checked()
            except Exception:
                log.warning("Could not read the last update check time", exc_info=True)
                last = ""
            # Our own attempt time too: a failing check never advances last_checked().
            attempted = self._last_game_attempt.isoformat() if self._last_game_attempt else ""
            if not is_due(last, interval, now=now) or not is_due(attempted, interval, now=now):
                return False
        self._last_game_attempt = now
        self._game_check = self._submit(
            lambda *, token: self._ctx.updates.check(token=token),
            on_error=lambda exc: self._report_failure(STEP_GAME_UPDATES, exc),
        )
        return True

    def check_app_update(self) -> bool:
        if self._stopped or (self._app_check is not None and not self._app_check.done()):
            return False
        if not self._ctx.settings.get().check_app_updates:
            return False
        self._last_app_check = self._clock()
        self._app_check = self._submit(
            lambda *, token: self._ctx.app_updates.check(token=token),
            on_error=lambda exc: self._report_failure(STEP_APP_UPDATES, exc),
        )
        return True

    def _report_failure(self, name: str, exc: BaseException) -> None:
        log.warning("%s failed: %s", name.capitalize(), exc, exc_info=exc)
        self.step_failed.emit(name, exc)

    # --- helpers ------------------------------------------------------------------------------------
    def _submit(self, fn: Callable[..., Any], *, on_result: Callable[[Any], None] | None = None,
                on_error: Callable[[BaseException], None] | None = None) -> TaskHandle[Any]:
        handle = run_async(self, self._ctx.runner, fn, on_result=on_result,
                           on_error=on_error or (lambda exc: log.warning("Startup task failed: %s", exc)),
                           on_finished=self._prune_handles)
        self._handles.append(handle)
        return handle

    def _prune_handles(self) -> None:
        self._handles = [h for h in self._handles if not h.done()]


class _Skipped:
    def __repr__(self) -> str:
        return "<skipped>"


_SKIPPED = _Skipped()
