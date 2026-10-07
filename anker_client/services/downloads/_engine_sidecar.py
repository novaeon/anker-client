"""The ``<dest>.part.json`` sidecar that makes a ``<dest>.part`` file resumable.

Format (UTF-8 JSON)::

    {"version": 1, "url": "https://…", "size": 123456, "etag": "\\"abc\\"",
     "last_modified": "Tue, 06 Oct 2026 10:00:00 GMT",
     "segments": [{"start": 0, "end": 61727, "done": 61728},
                  {"start": 61728, "end": 123455, "done": 1024}]}

* ``end`` is inclusive; ``done`` counts bytes already written from ``start``.
* With a known ``size`` the segments are sorted, contiguous and cover
  ``[0, size - 1]`` exactly. With an unknown size (``"size": null``) there is
  a single segment ``{"start": 0, "end": null, "done": n}``.
* Saved atomically (temp file + ``os.replace``) so a crash never leaves a
  half-written sidecar; ``done`` may lag behind the data on disk (the lagging
  bytes are simply downloaded again), never the other way round.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from anker_client.services.downloads._engine_segments import RangeTriple

log = logging.getLogger(__name__)

SIDECAR_VERSION = 1
PART_SUFFIX = ".part"
SIDECAR_SUFFIX = ".part.json"
_TMP_SUFFIX = ".tmp"
_REPLACE_ATTEMPTS = 5


def part_path(dest_path: str) -> str:
    return dest_path + PART_SUFFIX


def sidecar_path(dest_path: str) -> str:
    return dest_path + SIDECAR_SUFFIX


def temp_path(sidecar: str) -> str:
    return sidecar + _TMP_SUFFIX


@dataclass(slots=True)
class Sidecar:
    url: str
    size: int | None
    etag: str = ""
    last_modified: str = ""
    segments: list[RangeTriple] = field(default_factory=list)

    @property
    def bytes_done(self) -> int:
        return sum(done for _start, _end, done in self.segments)

    @property
    def complete(self) -> bool:
        return self.size is not None and self.bytes_done == self.size

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SIDECAR_VERSION,
            "url": self.url,
            "size": self.size,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "segments": [{"start": s, "end": e, "done": d} for s, e, d in self.segments],
        }

    @classmethod
    def from_json(cls, data: Any) -> Sidecar:
        """Parse and validate; raises ``ValueError`` describing the first problem found."""
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        if data.get("version") != SIDECAR_VERSION:
            raise ValueError(f"unsupported version {data.get('version')!r}")
        size = data.get("size")
        if size is not None and not _is_int(size, minimum=0):
            raise ValueError(f"invalid size {size!r}")
        raw_segments = data.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("segments missing")
        segments = [_parse_segment(item) for item in raw_segments]
        _validate_layout(segments, size)
        return cls(
            url=str(data.get("url") or ""),
            size=size,
            etag=str(data.get("etag") or ""),
            last_modified=str(data.get("last_modified") or ""),
            segments=segments,
        )


def _is_int(value: Any, *, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _parse_segment(item: Any) -> RangeTriple:
    if not isinstance(item, dict):
        raise ValueError("segment is not an object")
    start, end, done = item.get("start"), item.get("end"), item.get("done")
    if not _is_int(start, minimum=0) or not _is_int(done, minimum=0):
        raise ValueError(f"invalid segment {item!r}")
    if end is not None and (not _is_int(end, minimum=start - 1) or done > end - start + 1):
        raise ValueError(f"invalid segment {item!r}")
    return start, end, done


def _validate_layout(segments: list[RangeTriple], size: int | None) -> None:
    if size is None:
        if len(segments) != 1 or segments[0][0] != 0 or segments[0][1] is not None:
            raise ValueError("unknown-size download must have exactly one open segment")
        return
    expected_start = 0
    for start, end, _done in segments:
        if end is None or start != expected_start:
            raise ValueError("segments are not contiguous")
        expected_start = end + 1
    if expected_start != size:
        raise ValueError(f"segments cover {expected_start} bytes, size is {size}")


def load(path: str) -> Sidecar | None:
    """The sidecar at ``path``, or None when missing or unusable (the reason is logged)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable download sidecar %s: %s", path, exc)
        return None
    try:
        return Sidecar.from_json(data)
    except ValueError as exc:
        log.warning("Ignoring invalid download sidecar %s: %s", path, exc)
        return None


def save(path: str, sidecar: Sidecar) -> None:
    """Atomically replace ``path`` with ``sidecar`` (raises ``OSError``)."""
    tmp = temp_path(path)
    payload = json.dumps(sidecar.to_json(), separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    replace_with_retry(tmp, path)


def replace_with_retry(src: str, dst: str, *, attempts: int = _REPLACE_ATTEMPTS) -> None:
    """``os.replace`` that rides out short-lived sharing violations (antivirus, indexer) on Windows."""
    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            threading.Event().wait(delay)
            delay *= 2


def remove_quietly(path: str) -> bool:
    """Delete ``path`` if it exists; returns False (and logs) when it could not be deleted."""
    delay = 0.05
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError as exc:
            if attempt == _REPLACE_ATTEMPTS:
                log.warning("Could not delete %s: %s", path, exc)
                return False
            threading.Event().wait(delay)
            delay *= 2
        except OSError as exc:
            log.warning("Could not delete %s: %s", path, exc)
            return False
    return False
