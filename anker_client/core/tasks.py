"""Background work without Qt: cancellation tokens and a bounded thread pool.

Every long-running service method accepts a ``token: CancelToken`` keyword and
calls ``token.raise_if_cancelled()`` between blocking steps (raising
:class:`~anker_client.core.errors.OperationCancelled`). Blocking waits use
``token.wait(seconds)`` instead of ``time.sleep`` so cancellation is immediate.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Generic, TypeVar

from anker_client.core.errors import OperationCancelled

log = logging.getLogger(__name__)
T = TypeVar("T")


class CancelToken:
    """Cooperative cancellation flag with an optional reason (e.g. ``"pause"``)."""

    __slots__ = ("_callbacks", "_event", "_lock", "_parent_unsub", "reason")

    def __init__(self, parent: CancelToken | None = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []
        self.reason = ""
        self._parent_unsub: Callable[[], None] | None = None
        if parent is not None:
            self._parent_unsub = parent.on_cancel(lambda: self.cancel(parent.reason))

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("Cancel callback failed")

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run ``callback`` when cancelled (immediately if already cancelled). Returns an unregister fn."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def unregister() -> None:
                    with self._lock:
                        if callback in self._callbacks:
                            self._callbacks.remove(callback)

                return unregister
        callback()
        return lambda: None

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled()

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep up to ``timeout`` seconds; returns True if cancelled meanwhile."""
        return self._event.wait(timeout)

    def sleep(self, seconds: float) -> None:
        """Like ``time.sleep`` but raises ``OperationCancelled`` as soon as the token fires."""
        if self._event.wait(max(0.0, seconds)):
            raise OperationCancelled()

    def child(self) -> CancelToken:
        """A token cancelled whenever this one is (but cancellable on its own too)."""
        return CancelToken(parent=self)


#: A token that is never cancelled, for callers that do not need cancellation.
NEVER = CancelToken()


class TaskHandle(Generic[T]):
    def __init__(self, future: Future[T], token: CancelToken, name: str) -> None:
        self.future = future
        self.token = token
        self.name = name

    def cancel(self, reason: str = "") -> None:
        self.token.cancel(reason)
        self.future.cancel()

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled

    def done(self) -> bool:
        return self.future.done()

    def result(self, timeout: float | None = None) -> T:
        return self.future.result(timeout)

    def add_done_callback(self, callback: Callable[[TaskHandle[T]], None]) -> None:
        self.future.add_done_callback(lambda _f: callback(self))


class TaskRunner:
    """Bounded pool for short/medium background work (network fetches, scans, disk I/O).

    Long-lived loops (download workers, process monitors) own dedicated threads
    instead, so they can never starve this pool.
    """

    def __init__(self, max_workers: int = 8, name: str = "anker-task") -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._root = CancelToken()
        self._closed = False

    def submit(self, fn: Callable[..., T], /, *args: Any, name: str = "", **kwargs: Any) -> TaskHandle[T]:
        """Run ``fn(*args, token=<CancelToken>, **kwargs)`` in the pool.

        ``fn`` must accept a ``token`` keyword argument.
        """
        if self._closed:
            raise RuntimeError("TaskRunner is shut down")
        token = self._root.child()
        task_name = name or getattr(fn, "__qualname__", repr(fn))

        def run() -> T:
            token.raise_if_cancelled()
            try:
                return fn(*args, token=token, **kwargs)
            except OperationCancelled:
                raise
            except Exception:
                log.debug("Background task %s failed", task_name, exc_info=True)
                raise

        return TaskHandle(self._executor.submit(run), token, task_name)

    def shutdown(self, wait: bool = True) -> None:
        self._closed = True
        self._root.cancel("shutdown")
        self._executor.shutdown(wait=wait, cancel_futures=True)
