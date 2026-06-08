# tests/test_config.py
from anker_client.config import GAMES_DIR, SEVEN_ZIP, BASE_URL, KEYRING_SERVICE

def test_games_dir_is_string():
    assert isinstance(GAMES_DIR, str)

def test_base_url():
    assert BASE_URL == "https://ankergames.net"

def test_seven_zip_is_string():
    assert isinstance(SEVEN_ZIP, str)

def test_keyring_service():
    assert KEYRING_SERVICE == "AnkerClient"
