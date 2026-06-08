# tests/test_themes.py
import os
from anker_client.themes import THEMES, get_qss, get_wallpaper_path


def test_all_themes_include_combobox_styling():
    """Every theme's QSS must define QComboBox, its drop-down, and its item view."""
    for key in THEMES:
        qss = get_qss(key)
        assert "QComboBox" in qss, f"{key}: missing QComboBox rule"
        assert "QComboBox::drop-down" in qss, f"{key}: missing QComboBox::drop-down rule"
        assert "QComboBox QAbstractItemView" in qss, f"{key}: missing QComboBox QAbstractItemView rule"


def test_vaporwave_theme_has_gif_wallpaper():
    path = get_wallpaper_path("vaporwave")
    assert path is not None
    assert os.path.basename(path) == "vaporwave.gif"
    assert os.path.exists(path)
