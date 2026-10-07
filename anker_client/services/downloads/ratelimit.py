"""Global bandwidth limiter shared by every download connection.

Design
* Token bucket refilled continuously at ``rate_bps``. Burst capacity is
  ``max(64 KiB, 0.5 s × rate)``. The bucket starts *empty* (at construction and
  whenever a limit is switched on) so the first second of a download does not
  overshoot the configured speed.
* A request larger than the burst capacity may proceed once the bucket holds a
  full burst; the balance then goes negative (debt) and later callers wait it
  off, so the long-term average stays exact for any chunk size.
* Fairness: callers that have to wait queue up FIFO and are served strictly in
  arrival order, so N connections asking for equal chunks get equal shares and
  none can starve. Only the head of the queue sleeps on a timer; the others
  sleep until the head hands over, which keeps wake-ups O(1) per grant.
* ``rate_bps == 0`` (unlimited) is a lock-free fast path.
* ``set_rate`` wakes every waiter so a new limit (or "unlimited") applies
  immediately; cancelling a caller's token wakes just that caller.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from anker_client.core.errors import OperationCancelled
from anker_client.core.tasks import CancelToken

log = logging.getLogger(__name__)

_MIN_BURST_BYTES = 64 * 1024
_BURST_SECONDS = 0.5
# Upper bound for any single sleep, so a missed notification can never hang a caller.
_MAX_WAIT_SECONDS = 1.0


class _Waiter:
    __slots__ = ("cond",)

    def __init__(self, lock: threading.Lock) -> None:
        self.cond = threading.Condition(lock)


class RateLimiter:
    """Token bucket. ``rate_bps == 0`` means unlimited (``acquire`` returns immediately).

    Thread-safe; many connections call ``acquire`` concurrently and share the
    budget fairly (no connection may starve). Burst capacity ≈ 0.5 s of rate,
    minimum 64 KiB. ``set_rate`` takes effect immediately for waiting callers.
    Negative rates are treated as 0 (unlimited).
    """

    def __init__(self, rate_bps: int = 0, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._rate = 0
        self._capacity = float(_MIN_BURST_BYTES)
        self._tokens = 0.0
        self._stamp = clock()
        self._waiters: deque[_Waiter] = deque()
        self.set_rate(rate_bps)

    @property
    def rate_bps(self) -> int:
        return self._rate

    def set_rate(self, rate_bps: int) -> None:
        rate = max(0, int(rate_bps))
        with self._lock:
            now = self._clock()
            if self._rate:
                self._refill(now)
            else:
                self._tokens = 0.0
            self._stamp = now
            self._rate = rate
            self._capacity = max(float(_MIN_BURST_BYTES), rate * _BURST_SECONDS)
            self._tokens = min(self._tokens, self._capacity)
            for waiter in self._waiters:
                waiter.cond.notify()
        log.debug("Download speed limit set to %s", f"{rate} B/s" if rate else "unlimited")

    def acquire(self, nbytes: int, token: CancelToken | None = None) -> None:
        """Block until ``nbytes`` may be transferred (raises ``OperationCancelled`` if cancelled)."""
        if token is not None and token.cancelled:
            raise OperationCancelled()
        if nbytes <= 0 or self._rate == 0:
            return
        with self._lock:
            if self._rate == 0:
                return
            if not self._waiters and self._try_take(nbytes):
                return
            waiter = _Waiter(self._lock)
            self._waiters.append(waiter)
        # Registered outside the lock: on an already-cancelled token the callback runs inline.
        unregister = token.on_cancel(lambda: self._wake(waiter)) if token is not None else None
        try:
            self._wait_turn(waiter, nbytes, token)
        finally:
            with self._lock:
                was_head = bool(self._waiters) and self._waiters[0] is waiter
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                if was_head and self._waiters:
                    self._waiters[0].cond.notify()
            if unregister is not None:
                unregister()

    # --- internals (callers hold ``self._lock`` unless noted) -----------------------------

    def _wait_turn(self, waiter: _Waiter, nbytes: int, token: CancelToken | None) -> None:
        """Sleep until ``waiter`` is at the head of the queue and the bucket can pay (takes the lock)."""
        with self._lock:
            while True:
                if token is not None and token.cancelled:
                    raise OperationCancelled()
                if self._rate == 0:
                    return
                timeout = _MAX_WAIT_SECONDS
                if self._waiters[0] is waiter:
                    if self._try_take(nbytes):
                        return
                    deficit = min(float(nbytes), self._capacity) - self._tokens
                    timeout = min(timeout, max(deficit / self._rate, 0.0005))
                waiter.cond.wait(timeout)

    def _try_take(self, nbytes: int) -> bool:
        self._refill(self._clock())
        if self._tokens >= min(float(nbytes), self._capacity):
            self._tokens -= nbytes
            return True
        return False

    def _refill(self, now: float) -> None:
        elapsed = now - self._stamp
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._stamp = now

    def _wake(self, waiter: _Waiter) -> None:
        """Cancellation callback (called without the lock held)."""
        with self._lock:
            waiter.cond.notify()
