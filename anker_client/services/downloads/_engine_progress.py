"""Speed estimation for download progress reports.

:class:`SpeedMeter` is an exponential moving average with a ~5 s time
constant. During warm-up it behaves like a plain cumulative average (the
sample weight is ``max(1 - e^(-dt/τ), dt / elapsed)``), so the first readings
are accurate instead of ramping up slowly from zero.
"""

from __future__ import annotations

import math


class SpeedMeter:
    def __init__(self, tau_seconds: float = 5.0) -> None:
        self._tau = tau_seconds
        self._origin: float | None = None
        self._last_time = 0.0
        self._last_bytes = 0
        self._speed = 0.0

    @property
    def speed(self) -> float:
        return self._speed

    def update(self, total_bytes: int, now: float) -> float:
        """Feed the cumulative byte count observed at ``now``; returns bytes/second."""
        if self._origin is None or total_bytes < self._last_bytes:
            # First sample, or the transfer restarted from zero: new baseline. The next
            # sample gets full weight (dt / elapsed == 1), so a stale speed is replaced at once.
            self._origin = now
            self._last_time = now
            self._last_bytes = total_bytes
            return self._speed
        dt = now - self._last_time
        if dt <= 0:
            return self._speed
        instant = (total_bytes - self._last_bytes) / dt
        elapsed = now - self._origin
        weight = max(1.0 - math.exp(-dt / self._tau), dt / elapsed if elapsed > 0 else 1.0)
        self._speed += min(1.0, weight) * (instant - self._speed)
        self._last_time = now
        self._last_bytes = total_bytes
        return self._speed

    @staticmethod
    def eta(remaining: int | None, speed: float) -> float | None:
        if remaining is None or speed <= 0:
            return None
        return max(0.0, remaining / speed)
