"""RateLimiter: accuracy, live rate changes, fairness, cancellation, burst behaviour."""

from __future__ import annotations

import threading
import time

import pytest

from anker_client.core.errors import OperationCancelled
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.ratelimit import RateLimiter

KIB = 1024
MIB = 1024 * 1024


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _run_in_thread(fn) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def target() -> None:
        started = time.perf_counter()
        try:
            fn()
            result["outcome"] = "ok"
        except OperationCancelled:
            result["outcome"] = "cancelled"
        result["elapsed"] = time.perf_counter() - started
        result["finished_at"] = time.perf_counter()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


# --- basics -------------------------------------------------------------------------------


def test_unlimited_is_a_fast_path() -> None:
    limiter = RateLimiter(0)
    assert limiter.rate_bps == 0
    started = time.perf_counter()
    for _ in range(100_000):
        limiter.acquire(256 * KIB)
    assert time.perf_counter() - started < 1.0


def test_negative_rate_means_unlimited() -> None:
    limiter = RateLimiter(-10)
    assert limiter.rate_bps == 0
    limiter.set_rate(-1)
    assert limiter.rate_bps == 0


def test_zero_or_negative_byte_requests_never_block() -> None:
    limiter = RateLimiter(1)
    started = time.perf_counter()
    limiter.acquire(0)
    limiter.acquire(-5)
    assert time.perf_counter() - started < 0.05


def test_cancelled_token_raises_even_when_unlimited() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        RateLimiter(0).acquire(1, token)
    with pytest.raises(OperationCancelled):
        RateLimiter(MIB).acquire(1, token)


# --- accuracy -----------------------------------------------------------------------------


def test_single_thread_accuracy_2mib_at_1mib_per_second() -> None:
    limiter = RateLimiter(MIB)
    started = time.perf_counter()
    for _ in range(32):
        limiter.acquire(64 * KIB)
    elapsed = time.perf_counter() - started
    assert 1.5 <= elapsed <= 2.5, elapsed


def test_multi_thread_accuracy_shares_one_budget() -> None:
    limiter = RateLimiter(2 * MIB)

    def worker() -> None:
        for _ in range(16):
            limiter.acquire(32 * KIB)  # 4 threads x 512 KiB = 2 MiB total

    started = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    elapsed = time.perf_counter() - started
    assert 0.75 <= elapsed <= 1.25, elapsed


def test_requests_larger_than_the_burst_run_into_debt() -> None:
    # 4 MiB/s → burst 2 MiB. A 4 MiB request may start once the bucket holds a full
    # burst (0.5 s) and leaves a 2 MiB debt; the next one waits that off plus a burst (1 s).
    limiter = RateLimiter(4 * MIB)
    started = time.perf_counter()
    limiter.acquire(4 * MIB)
    first = time.perf_counter() - started
    limiter.acquire(4 * MIB)
    total = time.perf_counter() - started
    assert 0.35 <= first <= 0.75, first
    assert 1.25 <= total <= 1.9, total


def test_bucket_starts_empty_then_bursts_after_idle() -> None:
    limiter = RateLimiter(4 * MIB)
    started = time.perf_counter()
    limiter.acquire(MIB)  # empty bucket → ~0.25 s
    assert time.perf_counter() - started >= 0.18

    time.sleep(0.6)  # refills to the 0.5 s burst cap (2 MiB), not more
    started = time.perf_counter()
    limiter.acquire(2 * MIB)
    assert time.perf_counter() - started < 0.1
    started = time.perf_counter()
    limiter.acquire(MIB)
    assert 0.18 <= time.perf_counter() - started <= 0.45


def test_minimum_burst_is_64_kib() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1024, clock=clock)  # 0.5 s of 1 KiB/s would be only 512 bytes
    clock.now += 1_000
    started = time.perf_counter()
    limiter.acquire(64 * KIB)  # whole minimum burst available at once
    assert time.perf_counter() - started < 0.05


# --- live changes -------------------------------------------------------------------------


def test_switching_to_unlimited_releases_waiters_immediately() -> None:
    limiter = RateLimiter(16 * KIB)
    thread, result = _run_in_thread(lambda: [limiter.acquire(MIB) for _ in range(3)])
    time.sleep(0.3)
    assert "outcome" not in result
    switched = time.perf_counter()
    limiter.set_rate(0)
    thread.join(2)
    assert result["outcome"] == "ok"
    assert result["finished_at"] - switched < 0.2


def test_raising_the_rate_applies_to_waiting_callers() -> None:
    limiter = RateLimiter(32 * KIB)  # a 64 KiB burst would take ~2 s to accumulate
    thread, result = _run_in_thread(lambda: limiter.acquire(64 * KIB))
    time.sleep(0.2)
    limiter.set_rate(10 * MIB)
    thread.join(2)
    assert result["outcome"] == "ok"
    assert result["elapsed"] < 0.6


def test_lowering_the_rate_applies_live() -> None:
    limiter = RateLimiter(8 * MIB)
    for _ in range(8):
        limiter.acquire(256 * KIB)  # fast phase
    limiter.set_rate(512 * KIB)
    started = time.perf_counter()
    for _ in range(16):
        limiter.acquire(32 * KIB)  # 512 KiB at 512 KiB/s
    elapsed = time.perf_counter() - started
    assert 0.6 <= elapsed <= 1.4, elapsed


# --- fairness / cancellation --------------------------------------------------------------


def test_fair_share_across_threads() -> None:
    limiter = RateLimiter(2 * MIB)
    stop = threading.Event()
    counts = [0] * 4

    def worker(index: int) -> None:
        while not stop.is_set():
            limiter.acquire(16 * KIB)
            counts[index] += 16 * KIB

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(1.5)
    stop.set()
    limiter.set_rate(0)  # release anyone still queued
    for thread in threads:
        thread.join(2)
    total = sum(counts)
    assert 2.2 * MIB <= total <= 3.8 * MIB, total
    assert min(counts) >= 0.8 * max(counts), counts


def test_head_waiter_cancellation_is_prompt() -> None:
    limiter = RateLimiter(1024)
    token = CancelToken()
    thread, result = _run_in_thread(lambda: limiter.acquire(MIB, token))
    time.sleep(0.2)
    cancelled_at = time.perf_counter()
    token.cancel("pause")
    thread.join(2)
    assert result["outcome"] == "cancelled"
    assert result["finished_at"] - cancelled_at < 0.2


def test_queued_waiter_cancellation_keeps_queue_moving() -> None:
    limiter = RateLimiter(256 * KIB)  # burst 128 KiB
    head_token, queued_token = CancelToken(), CancelToken()
    head, head_result = _run_in_thread(lambda: limiter.acquire(128 * KIB, head_token))
    time.sleep(0.05)
    queued, queued_result = _run_in_thread(lambda: limiter.acquire(128 * KIB, queued_token))
    time.sleep(0.05)
    third, third_result = _run_in_thread(lambda: limiter.acquire(16 * KIB))
    time.sleep(0.05)
    queued_token.cancel()
    queued.join(1)
    assert queued_result["outcome"] == "cancelled"
    head.join(2)
    third.join(2)
    assert head_result["outcome"] == "ok"
    assert third_result["outcome"] == "ok"
    # third only waited for the head's burst + its own bytes, not for the cancelled request
    assert third_result["elapsed"] < 0.9


def test_stress_many_threads_with_rate_flips_never_deadlocks() -> None:
    limiter = RateLimiter(8 * MIB)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(60):
                limiter.acquire(4 * KIB)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for rate in (0, 4 * MIB, 16 * MIB, 0, 2 * MIB, 32 * MIB):
        time.sleep(0.03)
        limiter.set_rate(rate)
    for thread in threads:
        thread.join(5)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
