"""Typed, persisted user settings.

``SettingsStore`` owns one :class:`Settings` instance. Reads return a deep copy;
writes go through :meth:`SettingsStore.update`, which validates, persists
atomically and publishes :class:`~anker_client.core.events.SettingsChanged`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from anker_client.core.events import EventBus, SettingsChanged
from anker_client.core.models import SortOrder

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

CLOSE_ASK = "ask"
CLOSE_TRAY = "tray"
CLOSE_QUIT = "quit"


def default_library_dir() -> str:
    # The legacy client defaulted to C:\Games; keep it so existing installs are found.
    return r"C:\Games" if os.name == "nt" else str(Path.home() / "Games")


@dataclass(slots=True)
class Settings:
    schema_version: int = SCHEMA_VERSION

    # --- library ---------------------------------------------------------------
    library_dirs: list[str] = field(default_factory=lambda: [default_library_dir()])
    default_library: str = field(default_factory=default_library_dir)
    create_desktop_shortcut: bool = True
    create_start_menu_shortcut: bool = True
    library_view: str = "grid"  # grid | list
    library_sort: str = "title"  # title | last_played | playtime | installed | size
    show_hidden_games: bool = False
    minimize_on_game_launch: bool = False

    # --- downloads -------------------------------------------------------------
    download_dir: str = ""  # "" → <library>\.ankerclient\downloads
    max_concurrent_downloads: int = 1  # 1..5
    connections_per_download: int = 4  # 1..16
    speed_limit_kbps: int = 0  # 0 = unlimited
    auto_install: bool = True
    delete_archive_after_install: bool = True
    verify_archive_before_install: bool = False
    auto_resume_downloads: bool = True
    verification_timeout_seconds: int = 180

    # --- tools -----------------------------------------------------------------
    seven_zip_path: str = ""  # "" → auto-detect

    # --- updates & sync ----------------------------------------------------------
    check_game_updates: bool = True
    game_update_interval_hours: int = 6
    check_app_updates: bool = True
    catalog_sync_interval_hours: int = 24

    # --- store -------------------------------------------------------------------
    store_sort: str = SortOrder.NEWEST.value
    show_nsfw: bool = False

    # --- application -------------------------------------------------------------
    theme: str = "midnight"
    close_behavior: str = CLOSE_ASK  # ask | tray | quit
    start_minimized: bool = False
    launch_on_startup: bool = False
    notifications_enabled: bool = True
    remember_login: bool = True
    log_level: str = "INFO"
    first_run_completed: bool = False
    window_geometry: str = ""  # base64 QByteArray
    window_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        names = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in (data or {}).items() if k in names})
        return settings.validated()

    def validated(self) -> Settings:
        """Clamp/repair values so a hand-edited or old file can never crash the app."""
        s = copy.deepcopy(self)
        defaults = Settings()

        def clamp(value: Any, lo: int, hi: int, default: int) -> int:
            try:
                return max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                return default

        dirs = [str(d) for d in (s.library_dirs or []) if isinstance(d, str) and d.strip()]
        seen: set[str] = set()
        s.library_dirs = [d for d in dirs if not (os.path.normcase(d) in seen or seen.add(os.path.normcase(d)))]
        if not s.library_dirs:
            s.library_dirs = [default_library_dir()]
        if not s.default_library or os.path.normcase(s.default_library) not in {
            os.path.normcase(d) for d in s.library_dirs
        }:
            s.default_library = s.library_dirs[0]
        s.max_concurrent_downloads = clamp(s.max_concurrent_downloads, 1, 5, defaults.max_concurrent_downloads)
        s.connections_per_download = clamp(s.connections_per_download, 1, 16, defaults.connections_per_download)
        s.speed_limit_kbps = clamp(s.speed_limit_kbps, 0, 10_000_000, 0)
        s.verification_timeout_seconds = clamp(s.verification_timeout_seconds, 30, 900, 180)
        s.game_update_interval_hours = clamp(s.game_update_interval_hours, 1, 168, 6)
        s.catalog_sync_interval_hours = clamp(s.catalog_sync_interval_hours, 1, 720, 24)
        if s.close_behavior not in (CLOSE_ASK, CLOSE_TRAY, CLOSE_QUIT):
            s.close_behavior = CLOSE_ASK
        if s.library_view not in ("grid", "list"):
            s.library_view = "grid"
        if s.library_sort not in ("title", "last_played", "playtime", "installed", "size"):
            s.library_sort = "title"
        if s.store_sort not in {o.value for o in SortOrder}:
            s.store_sort = SortOrder.NEWEST.value
        if s.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            s.log_level = "INFO"
        s.schema_version = SCHEMA_VERSION
        return s

    @property
    def speed_limit_bps(self) -> int:
        return self.speed_limit_kbps * 1024


class SettingsStore:
    """Load/save :class:`Settings` as JSON and broadcast changes."""

    def __init__(self, path: Path, events: EventBus | None = None, *, legacy_path: Path | None = None) -> None:
        self._path = Path(path)
        self._legacy_path = Path(legacy_path) if legacy_path else None
        self._events = events
        self._lock = threading.RLock()
        self._settings = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> Settings:
        """A private copy of the current settings."""
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, **changes: Any) -> Settings:
        """Apply ``changes`` (field=value), persist, publish ``SettingsChanged``; returns the new copy."""
        names = {f.name for f in fields(Settings)}
        unknown = set(changes) - names
        if unknown:
            raise KeyError(f"Unknown settings: {sorted(unknown)}")
        with self._lock:
            before = self._settings.to_dict()
            merged = {**before, **changes}
            self._settings = Settings.from_dict(merged)
            after = self._settings.to_dict()
            changed = frozenset(k for k in after if after[k] != before.get(k))
            if changed:
                self._save_locked()
            result = copy.deepcopy(self._settings)
        if changed and self._events:
            self._events.publish(SettingsChanged(keys=changed))
        return result

    def replace(self, settings: Settings) -> Settings:
        return self.update(**settings.to_dict())

    # --- persistence --------------------------------------------------------------
    def _load(self) -> Settings:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return Settings.from_dict(data if isinstance(data, dict) else {})
            except (OSError, ValueError) as exc:
                log.warning("Settings file unreadable (%s); backing it up and using defaults", exc)
                try:
                    self._path.replace(self._path.with_suffix(".corrupt.json"))
                except OSError:
                    pass
                return Settings()
        settings = self._from_legacy() or Settings()
        try:
            self._settings = settings
            self._save_locked()
        except OSError:
            log.warning("Could not write initial settings file", exc_info=True)
        return settings

    def _from_legacy(self) -> Settings | None:
        """Import the pre-1.0 flat settings.json ({games_dir, seven_zip, theme, close_behavior})."""
        if not self._legacy_path or not self._legacy_path.exists():
            return None
        try:
            data = json.loads(self._legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        s = Settings()
        games_dir = data.get("games_dir")
        if isinstance(games_dir, str) and games_dir.strip():
            s.library_dirs = [games_dir]
            s.default_library = games_dir
        seven_zip = data.get("seven_zip")
        if isinstance(seven_zip, str) and seven_zip.strip():
            s.seven_zip_path = seven_zip
        legacy_theme = data.get("theme")
        if isinstance(legacy_theme, str) and legacy_theme:
            s.theme = {"default": "midnight"}.get(legacy_theme, legacy_theme)
        if data.get("close_behavior") in (CLOSE_TRAY, CLOSE_QUIT):
            s.close_behavior = data["close_behavior"]
        s.first_run_completed = True  # the user already configured the legacy client
        log.info("Imported legacy settings from %s", self._legacy_path)
        return s.validated()

    def _save_locked(self) -> None:
        atomic_write_text(self._path, json.dumps(self._settings.to_dict(), indent=2))


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace`` (crash-safe)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
