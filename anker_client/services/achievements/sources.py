"""Where Steam emulators keep achievement unlocks, and which emulator a game folder uses.

Save locations (``KNOWN_LOCATIONS``): each is a root folder whose sub-folders are
Steam app ids, with the unlock file at a fixed relative path inside::

    %APPDATA%\\GSE Saves\\<appid>\\achievements.json                gbe_fork (Goldberg fork)
    %APPDATA%\\Goldberg SteamEmu Saves\\<appid>\\achievements.json  Goldberg (original)
    %PUBLIC%\\Documents\\Steam\\RUNE\\<appid>\\achievements.ini      RUNE
    %PUBLIC%\\Documents\\Steam\\CODEX\\<appid>\\achievements.ini     CODEX
    %APPDATA%\\Steam\\CODEX\\<appid>\\achievements.ini               CODEX (older releases)
    %PUBLIC%\\Documents\\OnlineFix\\<appid>\\Stats\\Achievements.ini  OnlineFix

``detect_game(folder)`` looks inside an installed game (depth-limited) for the
emulator's config and returns its Steam app id plus save files that live
outside the standard roots:

* ``OnlineFix.ini`` → ``RealAppId`` (``FakeAppId`` is usually 480, Spacewar).
* ``steam_emu.ini`` (CODEX/RUNE) → ``[Settings] AppId``; the release group is
  read from the banner comment ("RUNE"/"CODEX").
* ``steam_settings/`` (gbe_fork/Goldberg) → ``steam_appid.txt`` inside it, the
  shipped ``achievements.json`` schema, and custom save locations:
  ``configs.user.ini`` ``[user::saves] local_save_path`` (relative to the DLL
  folder) and ``saves_folder_name`` (replaces "GSE Saves"), or the original
  Goldberg's ``local_save.txt``.
* a plain ``steam_appid.txt`` as the last resort.

App id 480 (Spacewar, the shared test app) never identifies a game.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from anker_client.services.achievements import formats
from anker_client.services.achievements.formats import Parser

log = logging.getLogger(__name__)

SPACEWAR_APP_ID = 480
_ENV_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_DETECT_MAX_DEPTH = 8  # Unreal games keep steam_api 7 folders deep (Engine/Binaries/ThirdParty/...)


@dataclass(frozen=True, slots=True)
class SaveLocation:
    """A folder of ``<appid>`` sub-folders, each with the unlock file at ``relative``."""

    key: str
    label: str
    root: str  # may contain %ENV% variables
    relative: str
    parse: Parser


KNOWN_LOCATIONS: tuple[SaveLocation, ...] = (
    SaveLocation("gbe_fork", "Goldberg (gbe_fork)", r"%APPDATA%\GSE Saves", "achievements.json",
                 formats.parse_goldberg_json),
    SaveLocation("goldberg", "Goldberg", r"%APPDATA%\Goldberg SteamEmu Saves", "achievements.json",
                 formats.parse_goldberg_json),
    SaveLocation("rune", "RUNE", r"%PUBLIC%\Documents\Steam\RUNE", "achievements.ini", formats.parse_codex_ini),
    SaveLocation("codex", "CODEX", r"%PUBLIC%\Documents\Steam\CODEX", "achievements.ini", formats.parse_codex_ini),
    SaveLocation("codex", "CODEX", r"%APPDATA%\Steam\CODEX", "achievements.ini", formats.parse_codex_ini),
    SaveLocation("onlinefix", "OnlineFix", r"%PUBLIC%\Documents\OnlineFix", r"Stats\Achievements.ini",
                 formats.parse_onlinefix_ini),
)

FORMATS: dict[str, tuple[str, Parser]] = {
    "goldberg_json": ("Goldberg (gbe_fork)", formats.parse_goldberg_json),
    "codex_ini": ("CODEX", formats.parse_codex_ini),
    "onlinefix_ini": ("OnlineFix", formats.parse_onlinefix_ini),
}


@dataclass(frozen=True, slots=True)
class SaveFile:
    """One unlock file the watcher reads."""

    path: str
    app_id: int
    label: str  # emulator shown to the user
    parse: Parser


@dataclass(frozen=True, slots=True)
class GameEmulator:
    """What ``detect_game`` found in a game folder."""

    app_id: int
    emulators: tuple[str, ...] = ()  # labels, e.g. ("RUNE",)
    extra_saves: tuple[SaveFile, ...] = ()  # unlock files outside the standard roots
    schema_file: str = ""  # gbe_fork steam_settings/achievements.json shipped with the game
    extra_roots: tuple[SaveLocation, ...] = field(default=())  # e.g. a renamed "GSE Saves" folder


def expand(path: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Expand ``%VAR%``; ``None`` when a variable is not set (the location does not apply)."""
    env = os.environ if environ is None else environ
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        value = env.get(match.group(1)) or env.get(match.group(1).upper())
        if not value:
            missing = True
            return match.group(0)
        return value

    expanded = _ENV_RE.sub(replace, path)
    return None if missing else os.path.normpath(expanded)


def app_id_from(value: str | None) -> int | None:
    """A valid Steam app id from text (``"892970"``), else ``None``. Spacewar (480) is rejected."""
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    app_id = int(match.group(0))
    return app_id if app_id > 0 and app_id != SPACEWAR_APP_ID else None


def save_files_in(location: SaveLocation, environ: Mapping[str, str] | None = None) -> list[SaveFile]:
    """Every ``<root>/<appid>/<relative>`` path of a location (existing app folders only; files may not exist yet)."""
    root = expand(location.root, environ)
    if root is None:
        return []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    result = []
    for name in names:
        app_id = app_id_from(name) if name.isdigit() else None
        if app_id is not None:
            result.append(SaveFile(os.path.join(root, name, location.relative), app_id, location.label, location.parse))
    return result


# --- game folder detection ---------------------------------------------------------------


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return formats.decode_text(handle.read(256 * 1024))
    except OSError:
        return ""


def _walk(folder: str, max_depth: int) -> list[tuple[str, list[str], list[str]]]:
    result = []
    base_depth = folder.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(folder):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # e.g. .ankerclient staging
        result.append((dirpath, dirnames, filenames))
    return result


def _gbe_saves(settings_dir: str, app_id: int, environ: Mapping[str, str] | None) -> tuple[
        list[SaveFile], list[SaveLocation]]:
    """Custom save folders configured in a gbe_fork/Goldberg ``steam_settings`` folder."""
    files: list[SaveFile] = []
    roots: list[SaveLocation] = []
    dll_dir = os.path.dirname(settings_dir)
    user = formats.parse_ini(_read_text(os.path.join(settings_dir, "configs.user.ini")))
    saves = {k.lower(): v for k, v in user.get("user::saves", {}).items()}
    local = saves.get("local_save_path", "")
    legacy = _read_text(os.path.join(settings_dir, "local_save.txt")).strip().splitlines()
    if not local and legacy:
        local = legacy[0].strip()
    if local:
        base = os.path.normpath(os.path.join(dll_dir, local))
        # gbe_fork keeps the per-app folder below the local path; older builds wrote directly into it.
        candidates = (os.path.join(base, str(app_id), "achievements.json"), os.path.join(base, "achievements.json"))
        for candidate in candidates:
            files.append(SaveFile(candidate, app_id, "Goldberg (gbe_fork)", formats.parse_goldberg_json))
    folder_name = saves.get("saves_folder_name", "").strip().strip("\\/")
    if folder_name and folder_name.lower() != "gse saves":
        roots.append(SaveLocation("gbe_fork", "Goldberg (gbe_fork)", "%APPDATA%\\" + folder_name,
                                  "achievements.json", formats.parse_goldberg_json))
    return files, roots


def detect_game(folder: str, environ: Mapping[str, str] | None = None,
                *, max_depth: int = _DETECT_MAX_DEPTH) -> GameEmulator | None:
    """Find the Steam emulator a game folder ships and the app id it emulates (``None``: none found)."""
    if not os.path.isdir(folder):
        return None
    onlinefix_id: int | None = None
    codex_id: int | None = None
    gbe_id: int | None = None
    plain_id: int | None = None
    emulators: list[str] = []
    settings_dirs: list[str] = []
    schema_file = ""
    for dirpath, _dirnames, filenames in _walk(folder, max_depth):
        lower = {name.lower(): name for name in filenames}
        if "onlinefix.ini" in lower:
            ini = formats.parse_ini(_read_text(os.path.join(dirpath, lower["onlinefix.ini"])))
            values = {k: v for section in ini.values() for k, v in section.items()}
            onlinefix_id = onlinefix_id or app_id_from(values.get("realappid"))
            emulators.append("OnlineFix")
        if "steam_emu.ini" in lower:
            text = _read_text(os.path.join(dirpath, lower["steam_emu.ini"]))
            ini = formats.parse_ini(text)
            settings = {k: v for section in ini.values() for k, v in section.items()}
            codex_id = codex_id or app_id_from(settings.get("appid"))
            emulators.append("RUNE" if re.search(r"\bRUNE\b", text, re.IGNORECASE) else "CODEX")
        if os.path.basename(dirpath).lower() == "steam_settings":
            settings_dirs.append(dirpath)
            gbe_id = gbe_id or app_id_from(_read_text(os.path.join(dirpath, lower.get("steam_appid.txt", "-"))))
            if "achievements.json" in lower and not schema_file:
                schema_file = os.path.join(dirpath, lower["achievements.json"])
            emulators.append("Goldberg (gbe_fork)")
        elif "steam_appid.txt" in lower and plain_id is None:
            plain_id = app_id_from(_read_text(os.path.join(dirpath, lower["steam_appid.txt"])))
    app_id = onlinefix_id or codex_id or gbe_id or plain_id
    if app_id is None:
        return None
    extra_saves: list[SaveFile] = []
    extra_roots: list[SaveLocation] = []
    for settings_dir in settings_dirs:
        files, roots = _gbe_saves(settings_dir, app_id, environ)
        extra_saves.extend(files)
        extra_roots.extend(roots)
    return GameEmulator(
        app_id=app_id,
        emulators=tuple(dict.fromkeys(emulators)),
        extra_saves=tuple(extra_saves),
        schema_file=schema_file,
        extra_roots=tuple(extra_roots),
    )
