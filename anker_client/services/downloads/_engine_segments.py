"""Byte-range bookkeeping for the download engine: segments, claiming and work stealing.

A :class:`Segment` is an inclusive byte range ``[start, end]`` of which the
first ``done`` bytes are on disk. ``end is None`` marks the single segment of
a download whose total size is not known (yet).

:class:`SegmentTable` is the only place segments are mutated; every method
takes the table lock, so connection threads, the coordinator (progress and
sidecar snapshots) and stealers always see a consistent picture.

Write protocol for a connection holding segment ``seg``::

    offset, n = table.reserve(seg, len(chunk))   # n < len(chunk) once a steal shrank seg
    part_file.write_at(offset, chunk[:n])
    finished = table.commit(seg, n)

``reserve`` marks the bytes as in flight so a concurrent steal never splits
inside a range that is being written.
"""

from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass

#: ``(start, end, done)`` with ``end`` inclusive (None = unknown).
RangeTriple = tuple[int, int | None, int]


@dataclass(slots=True)
class Segment:
    start: int
    end: int | None  # inclusive; None while the total size is unknown
    done: int = 0
    inflight: int = 0
    owner: int | None = None

    @property
    def position(self) -> int:
        """Absolute offset of the next byte to download."""
        return self.start + self.done

    @property
    def length(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1

    @property
    def complete(self) -> bool:
        return self.end is not None and self.done >= self.end - self.start + 1

    def stealable(self) -> int:
        """Bytes after the in-flight write that another connection could take over."""
        if self.end is None:
            return 0
        return max(0, self.end - (self.position + self.inflight) + 1)


def split_evenly(size: int, parts: int) -> list[Segment]:
    """``parts`` contiguous segments covering ``[0, size)`` (the last one absorbs the remainder)."""
    if size <= 0:
        return []
    parts = max(1, min(parts, size))
    step = size // parts
    segments = []
    for index in range(parts):
        start = index * step
        end = size - 1 if index == parts - 1 else start + step - 1
        segments.append(Segment(start, end))
    return segments


def coalesce(ranges: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Merge every fully downloaded range into the adjacent range that follows it.

    Keeps sidecars short after many steals. The result describes exactly the
    same bytes because ``done`` is always a prefix of its range.
    """
    merged: list[tuple[int, int, int]] = []
    for start, end, done in ranges:
        if merged:
            p_start, p_end, p_done = merged[-1]
            if p_end + 1 == start and p_done == p_end - p_start + 1:
                merged[-1] = (p_start, end, p_done + done)
                continue
        merged.append((start, end, done))
    return merged


class SegmentTable:
    """Thread-safe set of segments shared by the connections of one transfer."""

    def __init__(self, segments: list[Segment], *, steal_min: int, allow_split: bool) -> None:
        self._lock = threading.Lock()
        self._segments = sorted(segments, key=lambda s: s.start)
        self._starts = [s.start for s in self._segments]
        self._steal_min = max(1, steal_min)
        self._allow_split = allow_split
        self._steals = 0

    # --- claiming -----------------------------------------------------------------------

    def claim(self, owner: int) -> Segment | None:
        """Give ``owner`` work: an idle unfinished segment, else the second half of the busiest one.

        Returns None when nothing is left that is worth another connection.
        """
        with self._lock:
            for seg in self._segments:
                if seg.owner is None and not seg.complete:
                    seg.owner = owner
                    return seg
            if not self._allow_split:
                return None
            victim = max(
                (s for s in self._segments if s.owner is not None and not s.complete),
                key=Segment.stealable,
                default=None,
            )
            if victim is None or victim.end is None:
                return None
            free = victim.stealable()
            if free < 2 * self._steal_min:
                return None
            split_at = victim.end - free // 2 + 1
            stolen = Segment(split_at, victim.end, owner=owner)
            victim.end = split_at - 1
            index = bisect.bisect_left(self._starts, split_at)
            self._segments.insert(index, stolen)
            self._starts.insert(index, split_at)
            self._steals += 1
            return stolen

    def release(self, seg: Segment) -> None:
        """The owner stopped working on ``seg`` (finished, failed or backing off)."""
        with self._lock:
            seg.owner = None
            seg.inflight = 0

    # --- writing ------------------------------------------------------------------------

    def next_request(self, seg: Segment) -> tuple[int, int | None]:
        """``(position, end)`` to request next for ``seg``."""
        with self._lock:
            return seg.position, seg.end

    def reserve(self, seg: Segment, nbytes: int) -> tuple[int, int]:
        """Mark up to ``nbytes`` at the segment position as in flight → ``(offset, allowed)``."""
        with self._lock:
            allowed = nbytes if seg.end is None else max(0, min(nbytes, seg.end - seg.position + 1))
            seg.inflight = allowed
            return seg.position, allowed

    def commit(self, seg: Segment, nbytes: int) -> bool:
        """``nbytes`` of the reservation are on disk; returns True when the segment is finished."""
        with self._lock:
            seg.done += nbytes
            seg.inflight = 0
            return seg.complete

    def set_end(self, seg: Segment, end: int) -> None:
        """Fix the end of a segment whose size was unknown (learned from headers or at EOF)."""
        with self._lock:
            seg.end = max(end, seg.start + seg.done - 1)

    def reset(self, seg: Segment) -> None:
        """Forget the progress of ``seg`` (a non-resumable stream restarting from its start)."""
        with self._lock:
            seg.done = 0
            seg.inflight = 0

    # --- queries ------------------------------------------------------------------------

    @property
    def steals(self) -> int:
        with self._lock:
            return self._steals

    def bytes_done(self) -> int:
        with self._lock:
            return sum(_clamped_done(seg) for seg in self._segments)

    def all_complete(self) -> bool:
        with self._lock:
            return all(seg.complete for seg in self._segments)

    def snapshot(self) -> list[RangeTriple]:
        """``(start, end, done)`` triples, coalesced when every end is known."""
        with self._lock:
            ranges = [(s.start, s.end, _clamped_done(s)) for s in self._segments]
        known = [(start, end, done) for start, end, done in ranges if end is not None]
        if len(known) == len(ranges):
            return list(coalesce(known))
        return ranges

    def __len__(self) -> int:
        with self._lock:
            return len(self._segments)


def _clamped_done(seg: Segment) -> int:
    length = seg.length
    return seg.done if length is None else min(seg.done, length)
