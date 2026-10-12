"""Parsers for the achievement files written by Steam emulators. Pure functions, no I/O.

Every parser takes the raw bytes of one file and returns ``{api_name: UnlockState}``
keyed by Steam's internal achievement name. They are tolerant: unknown keys are
ignored, entries they cannot read are skipped, and text may be UTF-8 (with or
without BOM), UTF-16 (with BOM) or the ANSI code page. A file that is not valid
at all (e.g. caught half-written) raises ``ValueError`` so the caller keeps the
previous state and reads it again on the next poll.

Formats (verified against real files):

* **Goldberg / gbe_fork** ``achievements.json`` (saves) — an object keyed by API
  name: ``{"ACH_X": {"earned": true, "earned_time": 1780897608}}``; gbe_fork may
  add ``"progress"`` / ``"max_progress"``.
* **CODEX / RUNE** ``achievements.ini`` — one section per API name with
  ``Achieved=0|1``, ``UnlockTime=<unix>``, ``CurProgress``, ``MaxProgress``; an
  optional ``[SteamAchievements]`` index section is ignored.
* **OnlineFix** ``Stats/Achievements.ini`` — one section per API name with
  ``achieved=true|false`` and ``timestamp=<unix>``.
* **gbe_fork schema** ``steam_settings/achievements.json`` (shipped with the
  game, not a save) — a list of ``{"name", "displayName", "description",
  "hidden", "icon", "icongray"}``; display texts may be strings or
  ``{language: text}`` objects. Used for names when Steam cannot be reached.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anker_client.core.models import Achievement

_TRUE = frozenset({"1", "true", "yes", "on"})
_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]*)\]\s*$")
_IGNORED_SECTIONS = frozenset({"steamachievements"})


@dataclass(frozen=True, slots=True)
class UnlockState:
    unlocked: bool
    unlocked_at: int = 0  # unix seconds; 0 = unknown
    progress: int | None = None
    max_progress: int | None = None


Parser = Callable[[bytes], dict[str, UnlockState]]


# --- text helpers ---------------------------------------------------------------------


def decode_text(data: bytes) -> str:
    """Decode a config/save file: BOM-marked UTF-16/UTF-8, else UTF-8, else the ANSI code page."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Minimal, tolerant INI reader: ``{section: {lower-case key: value}}``.

    Unlike ``configparser`` it accepts duplicate sections/keys (last wins), any
    characters in section names, ``;``/``#`` comments and keys before the first
    section (collected under ``""``).
    """
    sections: dict[str, dict[str, str]] = {}
    current = sections.setdefault("", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        match = _SECTION_RE.match(line)
        if match:
            current = sections.setdefault(match["name"].strip(), {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key.strip().lower()] = value.strip().strip('"')
    return sections


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _progress(current: Any, maximum: Any) -> tuple[int | None, int | None]:
    high = _int(maximum)
    if not high or high <= 0:
        return None, None
    return max(0, _int(current) or 0), high


# --- save files -----------------------------------------------------------------------


def parse_goldberg_json(data: bytes) -> dict[str, UnlockState]:
    try:
        document = json.loads(decode_text(data))
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("expected a JSON object keyed by achievement name")
    states: dict[str, UnlockState] = {}
    for name, entry in document.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        progress, high = _progress(entry.get("progress"), entry.get("max_progress"))
        unlocked = _flag(entry.get("earned", False))
        states[name] = UnlockState(
            unlocked=unlocked,
            unlocked_at=max(0, _int(entry.get("earned_time")) or 0) if unlocked else 0,
            progress=progress,
            max_progress=high,
        )
    return states


def parse_codex_ini(data: bytes) -> dict[str, UnlockState]:
    states: dict[str, UnlockState] = {}
    for name, values in parse_ini(decode_text(data)).items():
        if not name or name.lower() in _IGNORED_SECTIONS or "achieved" not in values:
            continue
        unlocked = _flag(values.get("achieved"))
        progress, high = _progress(values.get("curprogress"), values.get("maxprogress"))
        states[name] = UnlockState(
            unlocked=unlocked,
            unlocked_at=max(0, _int(values.get("unlocktime")) or 0) if unlocked else 0,
            progress=progress,
            max_progress=high,
        )
    return states


def parse_onlinefix_ini(data: bytes) -> dict[str, UnlockState]:
    states: dict[str, UnlockState] = {}
    for name, values in parse_ini(decode_text(data)).items():
        if not name or "achieved" not in values:
            continue
        unlocked = _flag(values.get("achieved"))
        states[name] = UnlockState(
            unlocked=unlocked,
            unlocked_at=max(0, _int(values.get("timestamp")) or 0) if unlocked else 0,
        )
    return states


# --- shipped schema -------------------------------------------------------------------


def _localized(value: Any, language: str) -> str:
    if isinstance(value, dict):
        for key in (language, "english"):
            if isinstance(value.get(key), str):
                return value[key]
        return next((v for v in value.values() if isinstance(v, str)), "")
    return value if isinstance(value, str) else ""


def parse_goldberg_schema(data: bytes, language: str = "english") -> list[Achievement]:
    """gbe_fork's ``steam_settings/achievements.json`` → definitions (no icons: those are local files)."""
    try:
        document = json.loads(decode_text(data))
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(document, list):
        raise ValueError("expected a JSON list of achievement definitions")
    result: list[Achievement] = []
    for entry in document:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            continue
        result.append(Achievement(
            api_name=entry["name"],
            name=_localized(entry.get("displayName"), language),
            description=_localized(entry.get("description"), language),
            hidden=_flag(entry.get("hidden", False)),
        ))
    return result
