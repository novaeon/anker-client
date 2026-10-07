"""Human-readable formatting and parsing of sizes, speeds, durations and versions."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

_UNITS = ["B", "KB", "MB", "GB", "TB"]
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\b", re.IGNORECASE)
_MULTIPLIERS = {
    "b": 1,
    "kb": 1024, "kib": 1024,
    "mb": 1024**2, "mib": 1024**2,
    "gb": 1024**3, "gib": 1024**3,
    "tb": 1024**4, "tib": 1024**4,
}


def format_bytes(size: float | int | None, *, unknown: str = "?") -> str:
    """``1536`` → ``"1.5 KB"``; binary units, labelled the way Windows Explorer does."""
    if size is None or size < 0:
        return unknown
    if size < 1024:
        return f"{int(size)} B"
    exponent = min(int(math.log(size, 1024)), len(_UNITS) - 1)
    value = size / 1024**exponent
    if value >= 100:
        return f"{value:.0f} {_UNITS[exponent]}"
    if value >= 10:
        return f"{value:.1f} {_UNITS[exponent]}"
    return f"{value:.2f} {_UNITS[exponent]}"


def format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | None, *, unknown: str = "—") -> str:
    """Compact duration: ``"1h 05m"``, ``"4m 09s"``, ``"12s"``."""
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return unknown
    seconds = round(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_playtime(seconds: int | None) -> str:
    """``"Never played"``, ``"12 minutes"``, ``"3.4 hours"``."""
    if not seconds:
        return "Never played"
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    return f"{hours:.1f} hours"


def format_relative_time(iso: str | None, *, now: datetime | None = None) -> str:
    """``"just now"``, ``"5 minutes ago"``, ``"yesterday"``, ``"3 days ago"``, or the date."""
    if not iso:
        return "never"
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    delta = (now - when).total_seconds()
    if delta < 0:
        return when.astimezone().strftime("%d %b %Y")
    if delta < 60:
        return "just now"
    if delta < 3600:
        minutes = int(delta // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if delta < 86400:
        hours = int(delta // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(delta // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.astimezone().strftime("%d %b %Y")


def parse_size(text: str | None) -> int | None:
    """``"104.60 GB"`` → bytes; ``None`` when no size is present."""
    if not text:
        return None
    match = _SIZE_RE.search(str(text))
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return int(number * _MULTIPLIERS[match.group(2).lower()])


_VERSION_PREFIX_RE = re.compile(r"^\s*(?:version|ver\.?|v)\s*", re.IGNORECASE)


def normalize_version(version: str | None) -> str:
    """``"V 1.5.12620"`` / ``"v1.5.12620"`` → ``"1.5.12620"``; strips whitespace and build noise."""
    if not version:
        return ""
    text = _VERSION_PREFIX_RE.sub("", str(version).strip())
    return " ".join(text.split()).strip().lower()


def versions_differ(installed: str | None, latest: str | None) -> bool:
    """True only when both versions are known and they differ after normalisation."""
    a, b = normalize_version(installed), normalize_version(latest)
    return bool(a and b and a != b)


def compare_versions(a: str | None, b: str | None) -> int:
    """Best-effort ordering of dotted versions: -1 if a<b, 0 equal/unknown, 1 if a>b."""
    na, nb = normalize_version(a), normalize_version(b)
    if not na or not nb or na == nb:
        return 0

    def key(v: str) -> list[tuple[int, int | str]]:
        parts = re.split(r"[.\-_ ]+", v)
        return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p]

    ka, kb = key(na), key(nb)
    try:
        return (ka > kb) - (ka < kb)
    except TypeError:
        return 0


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"
