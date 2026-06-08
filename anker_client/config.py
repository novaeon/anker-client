# anker_client/config.py
BASE_URL = "https://ankergames.net"
GAMES_DIR = r"C:\Games"
SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"
KEYRING_SERVICE = "AnkerClient"
KEYRING_USERNAME_KEY = "email"
KEYRING_PASSWORD_KEY = "password"

JUNK_FILENAMES = {"read me.txt", "run me!.bat"}
JUNK_EXTENSIONS = {".url"}

UTILITY_EXE_PATTERNS = [
    "unitycrashandler",
    "crashhandler",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "directx",
    "dotnetfx",
    "dotnet",
]
