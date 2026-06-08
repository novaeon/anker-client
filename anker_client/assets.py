# anker_client/assets.py
from pathlib import Path

from PyQt6.QtGui import QIcon

_RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
_ICON_FILENAMES = ("icon.ico", "icon.png", "logo.svg")


def get_icon_path() -> str | None:
    """Return the best available application icon asset."""
    for filename in _ICON_FILENAMES:
        path = _RESOURCES_DIR / filename
        if path.exists():
            return str(path)
    return None


def get_app_icon() -> QIcon:
    """Return the application icon, or an empty QIcon if the asset is missing."""
    path = get_icon_path()
    return QIcon(path) if path else QIcon()
