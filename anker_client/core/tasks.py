"""Bounded, cancellable background work for the Qt application.

The original UI created short-lived ``QThread`` objects directly.  Replacing a
reference while one of those threads was still running lets Qt destroy the
wrapper prematurely, which is a process-ending error.  This module keeps all
work owned by one bounded thread pool and gives callers cooperative
cancellation without exposing thread lifetimes to widgets.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal


class TaskSignals(QObject):
    """Signals shared by background tasks."""

    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class BackgroundTask(QRunnable):
    """Run a callable in a worker thread.

    The callable receives a :class:`threading.Event` as its first argument and
    should check it between blocking operations.  Results and errors are
    suppressed after cancellation, while ``finished`` is always emitted.
    """

    def __init__(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.signals = TaskSignals()
        self._function = function
        self._args = args
        self._kwargs = kwargs
        self._cancel_event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if self.is_cancelled:
                return
            value = self._function(
                self._cancel_event,
                *self._args,
                **self._kwargs,
            )
            if not self.is_cancelled:
                self.signals.result.emit(value)
        except Exception as exc:
            if not self.is_cancelled:
                message = str(exc).strip() or type(exc).__name__
                self.signals.error.emit(message)
        finally:
            self.signals.finished.emit()


class TaskRunner(QObject):
    """Own all application background work and cap concurrency."""

    def __init__(self, max_workers: int | None = None) -> None:
        super().__init__()
        self._pool = QThreadPool(self)
        default_workers = min(4, max(2, os.cpu_count() or 2))
        self._pool.setMaxThreadCount(max_workers or default_workers)
        self._tasks: set[QRunnable] = set()

    def submit(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> BackgroundTask:
        task = BackgroundTask(function, *args, **kwargs)
        self.start(task)
        return task

    def start(self, task: QRunnable) -> QRunnable:
        """Start a custom runnable exposing ``signals.finished`` and cancel()."""

        signals = getattr(task, "signals", None)
        if signals is None or not hasattr(signals, "finished"):
            raise TypeError("managed tasks must expose a signals.finished signal")
        self._tasks.add(task)
        signals.finished.connect(lambda task=task: self._discard(task))
        self._pool.start(task)
        return task

    def _discard(self, task: QRunnable) -> None:
        self._tasks.discard(task)

    def cancel_all(self) -> None:
        for task in tuple(self._tasks):
            cancel = getattr(task, "cancel", None)
            if cancel:
                cancel()
        self._pool.clear()

    def shutdown(self, timeout_ms: int = 5_000) -> bool:
        """Cancel queued work and briefly wait for running calls to unwind."""

        self.cancel_all()
        return self._pool.waitForDone(timeout_ms)


_runner: TaskRunner | None = None


def get_task_runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner
