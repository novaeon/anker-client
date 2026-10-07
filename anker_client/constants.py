"""Application-wide constants. Nothing in here may import Qt or touch the network."""

from __future__ import annotations

APP_NAME = "AnkerClient"
APP_ID = "AnkerClient"  # used for single-instance server, keyring service, AppUserModelID
ORG_NAME = "AnkerClient"
GITHUB_REPO = "novaeon/anker-client"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ISSUES_URL = f"https://github.com/{GITHUB_REPO}/issues"

BASE_URL = "https://ankergames.net"
SITE_HOST = "ankergames.net"

# Fallback desktop Chrome UA. At runtime the UI replaces it with the embedded
# Chromium's real UA (minus the QtWebEngine token) so that requests made by
# ``requests`` and by the verification browser look identical to the site.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# Keyring layout (kept identical to the legacy client so remembered sign-ins survive).
KEYRING_SERVICE = "AnkerClient"
KEYRING_EMAIL_KEY = "email"
KEYRING_PASSWORD_KEY = "password"

# Name of the per-game manifest written at the root of every managed install.
MANIFEST_FILENAME = ".ankerclient.json"
# Folder inside every library root that holds staging/downloads; never treated as a game.
LIBRARY_WORK_DIRNAME = ".ankerclient"
# Folders inside a library root that are never treated as games.
LIBRARY_IGNORED_DIRNAMES = frozenset({LIBRARY_WORK_DIRNAME, "_temp", "$RECYCLE.BIN", "System Volume Information"})

# Start-menu sub-folder for shortcuts created by this version.
START_MENU_FOLDER = "AnkerClient Games"

# Archive files the site serves are typically ~= installed size; extraction needs
# archive + extracted bytes on the same volume. Used for pre-flight disk checks.
DISK_SPACE_FACTOR = 2.1
DISK_SPACE_HEADROOM_BYTES = 512 * 1024 * 1024

# Executable names that are never the game itself.
UTILITY_EXE_PATTERNS = (
    "unitycrashhandler",
    "crashhandler",
    "crashreport",
    "crashpad",
    "bugsplat",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "dxwebsetup",
    "directx",
    "dotnetfx",
    "dotnet",
    "ndp4",
    "physx",
    "oalinst",
    "ue4prereq",
    "ueprereq",
    "uninstall",
    "unins000",
    "setup",
    "installer",
    "redist",
    "easyanticheat_setup",
    "battleye_installer",
    "notification_helper",
    "cefprocess",
    "zfgamebrowser",
    "quicksfv",
)

# Files that the site bundles into archives and that should be removed after extraction.
JUNK_FILENAMES = frozenset({"read me.txt", "readme.txt", "run me!.bat", "ankergames.url", "ankergames.net.url"})
JUNK_EXTENSIONS = frozenset({".url"})

# Folders that contain prerequisite installers.
REDIST_DIRNAMES = frozenset({"_commonredist", "redist", "redistributables", "_redist", "directx", "prerequisites"})
