# tests/test_assets.py
from pathlib import Path

from PyQt6.QtGui import QImage

from anker_client.assets import get_icon_path


def test_app_icon_asset_exists():
    path = get_icon_path()
    assert path is not None
    assert Path(path).is_file()


def test_icon_png_has_transparent_background():
    path = get_icon_path()
    assert path is not None

    png_path = Path(path).with_name("icon.png")
    image = QImage(str(png_path))

    assert not image.isNull()
    assert image.pixelColor(0, 0).alpha() == 0
