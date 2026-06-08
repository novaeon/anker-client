# anker_client/settings.py
import os
import json
from anker_client.config import GAMES_DIR as _DEFAULT_GAMES_DIR, SEVEN_ZIP as _DEFAULT_SEVEN_ZIP
from anker_client.core.paths import sanitize_cover_name

_SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", ""), "AnkerClient")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")
_LIBRARY_CACHE_FILE = os.path.join(_SETTINGS_DIR, "library_cache.json")
_COVERS_DIR = os.path.join(_SETTINGS_DIR, "covers")

_cache: dict | None = None
_library_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def get_games_dir() -> str:
    return _load().get("games_dir") or _DEFAULT_GAMES_DIR


def get_seven_zip() -> str:
    return _load().get("seven_zip") or _DEFAULT_SEVEN_ZIP


def get_theme() -> str:
    from anker_client.themes import DEFAULT_THEME
    return _load().get("theme") or DEFAULT_THEME


def save_theme(key: str) -> None:
    global _cache
    data = _load().copy()
    data["theme"] = key
    os.makedirs(_SETTINGS_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _cache = data


def get_library_cache() -> dict:
    """Return the in-memory library cache, loading from disk on first call."""
    global _library_cache
    if _library_cache is None:
        try:
            with open(_LIBRARY_CACHE_FILE, encoding="utf-8") as f:
                _library_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _library_cache = {}
    return _library_cache


def update_library_cache(name: str, data: dict) -> None:
    """Persist one game's metadata into the library cache."""
    cache = get_library_cache()
    cache[name] = data
    os.makedirs(_SETTINGS_DIR, exist_ok=True)
    with open(_LIBRARY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def get_cover_path(name: str) -> str:
    """Return the absolute path for a game's cached cover image (may not exist yet)."""
    safe = sanitize_cover_name(name)
    if not safe:
        raise ValueError(f"Game name {name!r} produces an empty filename after sanitisation")
    return os.path.join(_COVERS_DIR, f"{safe}.png")


def get_close_behavior() -> str | None:
    """Return saved close behaviour: 'tray', 'quit', or None if never set."""
    return _load().get("close_behavior")


def save_close_behavior(key: str) -> None:
    """Persist the close behaviour setting ('tray' or 'quit')."""
    global _cache
    data = _load().copy()
    data["close_behavior"] = key
    os.makedirs(_SETTINGS_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _cache = data


def save(games_dir: str, seven_zip: str) -> None:
    global _cache
    data = _load().copy()
    data["games_dir"] = games_dir
    data["seven_zip"] = seven_zip
    os.makedirs(_SETTINGS_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _cache = data
