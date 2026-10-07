"""Find the game's main executable inside an install directory.

Scoring heuristic (higher is better), searching ``.exe`` files up to depth 3
(depth 0 = files directly in ``install_dir``; ``a/b/c/x.exe`` is depth 3).
Symlinks/junctions are not followed. Skipped folders: hidden dot-folders,
``__MACOSX``, prerequisite folders (``REDIST_DIRNAMES`` plus vcredist/prereq/
dotnetfx/thirdparty variants) and the Unreal ``Engine`` folder (an ``Engine``
dir with ``Binaries``/``Plugins``/``Extras``/``Content`` inside — only engine tools live there).

* blocklisted names (``constants.UTILITY_EXE_PATTERNS``, substring of the
  casefolded stem) are excluded entirely;
* name match — the stem is split on CamelCase/digits/separators, platform
  suffixes (``-Win64-Shipping``, ``_x64``, ``64``, ``_dx12``…) are dropped, then
  compared with ``normalize_title(title)``: +50 compact forms equal; +30 one
  contains the other (≥ 3 chars); +10 per shared significant word (≥ 3 chars,
  not a stop word);
* +25 Unity companion ``<stem>_Data`` dir; +25 Godot ``<stem>.pck``;
* Unreal: the launcher stub ``<Name>.exe`` next to an Unreal ``Engine`` folder
  gets +30; ``*-Win64-Shipping.exe`` gets +20 only when no such stub exists
  (the stub sets up the working directory and must be preferred);
* +15 at depth 0, +5 at depth 1, 0 deeper; +10 inside a ``bin``/``bin64``/``x64``/
  ``win64``/``binaries/win64`` dir;
* up to +5 for larger files (log-scaled: 0 at 100 KB, +5 from ~32 MB), −20 for files < 100 KB;
* −15 if the name contains launcher/config/settings/editor/server/benchmark/
  updater/patcher, unless that word is part of the title itself (">observer_",
  "Map Editor"); a lone candidate is still returned.

``find_executable`` returns the best candidate when it is unambiguous (single
candidate, or best score ≥ 40 and ≥ 15 ahead of the runner-up), else ``""``.
All returned paths are relative to ``install_dir`` with OS separators; ties are
ordered by depth, then path, so results are deterministic.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass

from anker_client.constants import REDIST_DIRNAMES, UTILITY_EXE_PATTERNS
from anker_client.core.paths import normalize_title
from anker_client.services.install._fsutil import long_path

log = logging.getLogger(__name__)

MAX_DEPTH = 3
MIN_CONFIDENT_SCORE = 40
MIN_LEAD = 15

_SKIP_DIRNAMES = REDIST_DIRNAMES | frozenset({
    "__macosx",
    "commonredist",
    "redistributable",
    "redists",
    "prereq",
    "prereqs",
    "prerequisite",
    "vcredist",
    "dotnetfx",
    "thirdparty",
})
_UNREAL_ENGINE_MARKERS = ("binaries", "plugins", "extras", "content")
_BIN_DIRNAMES = frozenset({"bin", "bin64", "x64", "win64"})
_PENALTY_WORDS = ("launcher", "config", "settings", "editor", "server", "benchmark", "updater", "patcher")
_PLATFORM_SUFFIXES = frozenset({
    "win64", "win32", "shipping", "x64", "x86", "64", "32", "64bit", "32bit", "dx11", "dx12", "d3d11",
    "d3d12", "vulkan", "vk", "opengl", "gl", "steam", "gog", "epic", "retail", "release", "final",
})
_STOP_WORDS = frozenset({"the", "and", "for", "edition", "game", "definitive", "complete", "deluxe", "goty"})
_SHIPPING_RE = re.compile(r"-win(?:64|32)-shipping$", re.IGNORECASE)
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SMALL_FILE = 100 * 1024


@dataclass(frozen=True, slots=True)
class _Exe:
    parts: tuple[str, ...]  # relative path components, file name last
    size: int
    sibling_names: frozenset[str]  # casefolded names in the same directory
    next_to_engine: bool

    @property
    def depth(self) -> int:
        return len(self.parts) - 1

    @property
    def stem(self) -> str:
        return os.path.splitext(self.parts[-1])[0]

    @property
    def relative(self) -> str:
        return os.path.join(*self.parts)


def _is_unreal_engine_dir(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            names = {entry.name.casefold() for entry in entries if entry.is_dir(follow_symlinks=False)}
    except OSError:
        return False
    return any(marker in names for marker in _UNREAL_ENGINE_MARKERS)


def _is_link(entry: os.DirEntry[str]) -> bool:
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def _skip_dir(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith(".") or folded in _SKIP_DIRNAMES or "redist" in folded or "prereq" in folded


def _collect(install_dir: str) -> list[_Exe]:
    found: list[_Exe] = []
    stack: list[tuple[str, tuple[str, ...]]] = [(long_path(install_dir), ())]
    while stack:
        directory, parts = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        siblings = frozenset(entry.name.casefold() for entry in entries)
        engine_here = False
        subdirs: list[os.DirEntry[str]] = []
        for entry in entries:
            try:
                if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name.casefold() == "engine" and _is_unreal_engine_dir(entry.path):
                engine_here = True
                continue
            if len(parts) < MAX_DEPTH and not _skip_dir(entry.name):
                subdirs.append(entry)
        for entry in entries:
            if not entry.name.casefold().endswith(".exe"):
                continue
            try:
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            found.append(_Exe((*parts, entry.name), size, siblings, engine_here))
        stack.extend((entry.path, (*parts, entry.name)) for entry in subdirs)
    return found


def _is_utility(stem: str) -> bool:
    folded = stem.casefold()
    return any(pattern in folded for pattern in UTILITY_EXE_PATTERNS)


def _stem_words(stem: str) -> list[str]:
    spaced = _CAMEL_RE.sub(" ", _SHIPPING_RE.sub("", stem))
    words = normalize_title(spaced).split()
    while len(words) > 1 and words[-1] in _PLATFORM_SUFFIXES:
        words.pop()
    return words


def _significant(words: list[str]) -> set[str]:
    return {word for word in words if len(word) >= 3 and word not in _STOP_WORDS}


def _name_score(stem: str, title: str) -> int:
    stem_words = _stem_words(stem)
    title_words = normalize_title(title).split()
    if not stem_words or not title_words:
        return 0
    compact_stem, compact_title = "".join(stem_words), "".join(title_words)
    score = 0
    if compact_stem == compact_title:
        score += 50
    elif min(len(compact_stem), len(compact_title)) >= 3 and (
        compact_stem in compact_title or compact_title in compact_stem
    ):
        score += 30
    score += 10 * len(_significant(stem_words) & _significant(title_words))
    return score


def _size_score(size: int) -> int:
    if size < _SMALL_FILE:
        return -20
    return max(0, min(5, int((math.log10(size) - 5) * 2.5)))


def _in_bin_dir(parts: tuple[str, ...]) -> bool:
    # "binaries/win64" is covered by "win64".
    return any(part.casefold() in _BIN_DIRNAMES for part in parts[:-1])


def _score(exe: _Exe, title: str, *, unreal_stub_present: bool) -> int:
    stem = exe.stem
    folded = stem.casefold()
    score = _name_score(stem, title)
    if f"{folded}_data" in exe.sibling_names:
        score += 25
    if f"{folded}.pck" in exe.sibling_names:
        score += 25
    if exe.next_to_engine:
        score += 30
    elif _SHIPPING_RE.search(stem) and not unreal_stub_present:
        score += 20
    score += {0: 15, 1: 5}.get(exe.depth, 0)
    if _in_bin_dir(exe.parts):
        score += 10
    score += _size_score(exe.size)
    compact_title = normalize_title(title).replace(" ", "")
    if any(word in folded and word not in compact_title for word in _PENALTY_WORDS):
        score -= 15
    return score


def executable_candidates(install_dir: str, title: str) -> list[tuple[str, int]]:
    """``[(relative_path, score)]`` sorted best first."""
    if not install_dir or not os.path.isdir(install_dir):
        return []
    exes = [exe for exe in _collect(install_dir) if not _is_utility(exe.stem)]
    stub_present = any(exe.next_to_engine for exe in exes)
    scored = [(exe, _score(exe, title, unreal_stub_present=stub_present)) for exe in exes]
    scored.sort(key=lambda item: (-item[1], item[0].depth, item[0].relative.casefold()))
    return [(exe.relative, score) for exe, score in scored]


def _pick_best(candidates: list[tuple[str, int]]) -> str:
    """The unambiguous winner of an ``executable_candidates`` result, else ``""``."""
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0][0]
    (best, best_score), (_, runner_up) = candidates[0], candidates[1]
    if best_score >= MIN_CONFIDENT_SCORE and best_score - runner_up >= MIN_LEAD:
        return best
    return ""


def find_executable(install_dir: str, title: str) -> str:
    candidates = executable_candidates(install_dir, title)
    choice = _pick_best(candidates)
    log.debug("Executable candidates for %r in %s: %s → %r", title, install_dir, candidates[:5], choice)
    return choice
