# tests/test_assets.py
from pathlib import Path

from anker_client.assets import get_icon_path


def test_app_icon_asset_exists():
    path = get_icon_path()
    assert path is not None
    assert Path(path).is_file()
