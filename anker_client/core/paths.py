"""Filesystem locations and Windows-safe naming."""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path

from anker_client.constants import APP_NAME

_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
_RESERVED_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_COVER_EXTRA_INVALID_CHARS = "".join(ch for ch in string.punctuation if ch not in "-_")


def sanitize_windows_name(
    name: str,
    fallback: str | None = "Game",
    *,
    extra_invalid_chars: str = "",
    max_length: int = 120,
) -> str:
    """Return a readable file or folder name that is valid on Windows.

    Control characters and reserved characters become spaces, whitespace is
    collapsed, leading/trailing dots and spaces are stripped, reserved device
    names are prefixed with ``_`` and the result is truncated to ``max_length``.
    """
    invalid_chars = _INVALID_FILENAME_CHARS | set(extra_invalid_chars)
    cleaned = "".join(" " if ord(ch) < 32 or ch in invalid_chars else ch for ch in str(name))
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return fallback or ""

    stem = cleaned.split(".", 1)[0].upper().strip()
    if stem in _RESERVED_DEVICE_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned[:max_length].rstrip(" .") or (fallback or "")


def legacy_cover_name(name: str) -> str:
    """Cover-cache file stem used by the pre-1.0 client (``covers/<stem>.png``)."""
    return sanitize_windows_name(name, fallback=None, extra_invalid_chars=_COVER_EXTRA_INVALID_CHARS)


def normalize_title(title: str) -> str:
    """Aggressive normalisation for fuzzy title matching (folder name ↔ catalog title)."""
    text = str(title).casefold()
    text = text.replace("&", " and ")
    keep = []
    for ch in text:
        if ch.isalnum():
            keep.append(ch)
        elif ch in "'’`":
            continue  # "Assassin's" and "Assassins" must normalise identically
        elif ch in " -_:.!?,+()[]{}/\\|;~#@$%^*=\"":
            keep.append(" ")
    return " ".join("".join(keep).split())


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when ``path`` is ``root`` or inside it (resolved, case-insensitive on Windows)."""
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except OSError:
        return False
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


def is_dangerous_delete_target(path: str | os.PathLike[str]) -> bool:
    """Refuse to recursively delete drive roots, user profile roots and system folders."""
    try:
        p = Path(path).resolve()
    except OSError:
        return True
    if p.parent == p:  # drive root
        return True
    home = Path.home().resolve()
    protected = {home, home.parent}
    for env in ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "APPDATA",
                "LOCALAPPDATA", "USERPROFILE"):
        value = os.environ.get(env)
        if value:
            try:
                protected.add(Path(value).resolve())
            except OSError:
                pass
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
        protected.add(home / name)
    return p in protected


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Every directory the application writes to.

    Layout (Windows defaults)::

        %APPDATA%\\AnkerClient\\            config_dir: config.json, anker.db, legacy files
        %LOCALAPPDATA%\\AnkerClient\\Cache  cache_dir: images/, http/
        %LOCALAPPDATA%\\AnkerClient\\Logs   logs_dir
        %LOCALAPPDATA%\\AnkerClient\\WebEngine  webengine_dir (verification browser profile)

    Setting ``ANKERCLIENT_HOME`` (tests, portable mode) puts everything under that folder.
    """

    config_dir: Path
    cache_dir: Path
    logs_dir: Path
    webengine_dir: Path

    @classmethod
    def default(cls) -> AppPaths:
        home = os.environ.get("ANKERCLIENT_HOME")
        if home:
            return cls.under(Path(home))
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return cls(
            config_dir=roaming / APP_NAME,
            cache_dir=local / APP_NAME / "Cache",
            logs_dir=local / APP_NAME / "Logs",
            webengine_dir=local / APP_NAME / "WebEngine",
        )

    @classmethod
    def under(cls, root: Path) -> AppPaths:
        root = Path(root)
        return cls(
            config_dir=root / "config",
            cache_dir=root / "cache",
            logs_dir=root / "logs",
            webengine_dir=root / "webengine",
        )

    # --- files -----------------------------------------------------------------
    @property
    def settings_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database_file(self) -> Path:
        return self.config_dir / "anker.db"

    @property
    def cookies_file(self) -> Path:
        """DPAPI-encrypted session cookies (see ``services.auth``)."""
        return self.config_dir / "session.bin"

    @property
    def images_dir(self) -> Path:
        return self.cache_dir / "images"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "ankerclient.log"

    # --- legacy (pre-1.0) client ---------------------------------------------------
    @property
    def legacy_settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def legacy_library_cache_file(self) -> Path:
        return self.config_dir / "library_cache.json"

    @property
    def legacy_covers_dir(self) -> Path:
        return self.config_dir / "covers"

    def ensure(self) -> AppPaths:
        for directory in (self.config_dir, self.cache_dir, self.logs_dir, self.webengine_dir, self.images_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def resource_path(name: str) -> Path:
    """Absolute path of a bundled resource (works from source and from PyInstaller)."""
    return Path(__file__).resolve().parent.parent / "resources" / name


def desktop_dir() -> Path:
    """The user's Desktop folder (honours OneDrive redirection when pywin32 is available)."""
    try:
        from win32com.shell import shell, shellcon  # type: ignore[import-not-found]

        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    except Exception:
        return Path.home() / "Desktop"


def start_menu_programs_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
