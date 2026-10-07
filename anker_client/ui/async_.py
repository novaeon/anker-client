"""Run service calls off the GUI thread and get results back on it.

Usage::

    run_async(self, ctx.runner, ctx.client.game_details, slug,
              on_result=self._show, on_error=self._show_error)

* ``fn`` is called as ``fn(*args, token=<CancelToken>, **kwargs)`` on a pool thread.
* Callbacks run on the GUI thread, and only while ``owner`` is alive — results
  for a destroyed widget are dropped silently (no "wrapped C/C++ object has
  been deleted" crashes).
* ``on_error`` receives the exception (``AnkerError`` subclasses carry a
  user-presentable ``user_message``; use :func:`error_text`).
* Cancellation (``handle.cancel()``) suppresses both callbacks.
* Keep the returned handle to cancel stale requests (e.g. when the user types
  a new search query before the previous one returned).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from anker_client.core.errors import AnkerError, OperationCancelled
from anker_client.core.tasks import TaskHandle, TaskRunner

log = logging.getLogger(__name__)


class _Relay(QObject):
    done = pyqtSignal(object, object)  # (result, exception)

    def __init__(self, owner: QObject) -> None:
        super().__init__(owner)  # parented: dies with the owner


def run_async(
    owner: QObject,
    runner: TaskRunner,
    fn: Callable[..., Any],
    /,
    *args: Any,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    **kwargs: Any,
) -> TaskHandle[Any]:
    relay = _Relay(owner)
    handle_box: list[TaskHandle[Any]] = []

    def deliver(result: Any, exc: BaseException | None) -> None:
        try:
            if sip.isdeleted(owner):
                return
            handle = handle_box[0] if handle_box else None
            cancelled = handle is not None and handle.cancelled
            if not cancelled:
                if exc is None:
                    if on_result is not None:
                        on_result(result)
                elif not isinstance(exc, OperationCancelled):
                    if on_error is not None:
                        on_error(exc)
                    else:
                        log.warning("Background call %s failed: %s", getattr(fn, "__qualname__", fn), exc)
            if on_finished is not None:
                on_finished()
        finally:
            if not sip.isdeleted(relay):
                relay.deleteLater()

    relay.done.connect(deliver, Qt.ConnectionType.QueuedConnection)

    handle = runner.submit(fn, *args, **kwargs)
    handle_box.append(handle)

    def completed(h: TaskHandle[Any]) -> None:  # runs on the worker thread
        future = h.future
        if future.cancelled():
            payload: tuple[Any, BaseException | None] = (None, OperationCancelled())
        else:
            exc = future.exception()
            payload = (None if exc else future.result(), exc)
        try:
            if not sip.isdeleted(relay):
                relay.done.emit(*payload)
        except RuntimeError:  # owner (and relay) destroyed concurrently
            pass

    handle.add_done_callback(completed)
    return handle


def error_text(exc: BaseException) -> str:
    """User-facing message for any exception."""
    if isinstance(exc, AnkerError):
        return exc.user_message
    return str(exc).strip() or type(exc).__name__
