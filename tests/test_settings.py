# tests/test_settings.py
import os
import json
import anker_client.settings as settings


def test_get_cover_path_returns_png_in_covers_dir():
    path = settings.get_cover_path("My Game")
    assert os.path.basename(path) == "My Game.png"
    assert os.path.basename(os.path.dirname(path)) == "covers"


def test_get_cover_path_sanitizes_unsafe_chars():
    path = settings.get_cover_path("Game: Ultimate Edition!")
    name = os.path.basename(path)
    assert ":" not in name
    assert "!" not in name
    assert name.endswith(".png")


def test_save_and_get_close_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    assert settings.get_close_behavior() is None
    settings.save_close_behavior("tray")
    assert settings.get_close_behavior() == "tray"
    monkeypatch.setattr(settings, "_cache", None)  # clear cache to force re-read
    assert settings.get_close_behavior() == "tray"
    settings.save_close_behavior("quit")
    assert settings.get_close_behavior() == "quit"


def test_get_cover_path_raises_for_empty_name():
    import pytest
    with pytest.raises(ValueError):
        settings.get_cover_path("!!!")


def test_library_cache_batch_update_is_atomic_and_preserves_entries(
    tmp_path,
    monkeypatch,
):
    cache_file = tmp_path / "library_cache.json"
    monkeypatch.setattr(settings, "_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_LIBRARY_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(settings, "_library_cache", None)

    settings.update_library_cache_many({
        "Hades": {"slug": "hades"},
        "Iron Lung": {"slug": "iron-lung"},
    })
    settings.update_library_cache("Hades", {"slug": "hades", "size_gb": "10"})

    persisted = json.loads(cache_file.read_text(encoding="utf-8"))
    assert persisted["Hades"]["size_gb"] == "10"
    assert persisted["Iron Lung"]["slug"] == "iron-lung"
    assert list(tmp_path.glob("*.tmp")) == []
