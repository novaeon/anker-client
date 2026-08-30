# anker_client/settings.py
import os
import copy
import json
import tempfile
import threading
from anker_client.config import GAMES_DIR as _DEFAULT_GAMES_DIR, SEVEN_ZIP as _DEFAULT_SEVEN_ZIP
from anker_client.core.paths import sanitize_cover_name

_SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", ""), "AnkerClient")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")
_LIBRARY_CACHE_FILE = os.path.join(_SETTINGS_DIR, "library_cache.json")
_COVERS_DIR = os.path.join(_SETTINGS_DIR, "covers")

_cache: dict | None = None
_library_cache: dict | None = None
_write_lock = threading.RLock()


def _atomic_write_json(path: str, data: dict) -> None:
    """Replace a JSON file atomically so a crash cannot truncate the cache."""

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".anker-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _save_settings(updates: dict) -> None:
    global _cache
    with _write_lock:
        data = _load().copy()
        data.update(updates)
        _atomic_write_json(_SETTINGS_FILE, data)
        _cache = data


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
    _save_settings({"theme": key})


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
    update_library_cache_many({name: data})


def update_library_cache_many(entries: dict[str, dict]) -> None:
    """Persist multiple metadata updates with a single atomic disk write."""

    if not entries:
        return
    with _write_lock:
        cache = get_library_cache()
        for name, data in entries.items():
            cache[name] = copy.deepcopy(data)
        _atomic_write_json(_LIBRARY_CACHE_FILE, cache)


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
    _save_settings({"close_behavior": key})


def save(games_dir: str, seven_zip: str) -> None:
    _save_settings({
        "games_dir": games_dir,
        "seven_zip": seven_zip,
    })
