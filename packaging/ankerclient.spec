# PyInstaller spec for AnkerClient (one-folder build).
#
# One-folder (not one-file) because QtWebEngine ships a helper process
# (QtWebEngineProcess.exe) plus ~100 MB of resources that must live next to the
# executable; a one-file build would unpack all of that on every start.
#
# Build:  python -m PyInstaller packaging/ankerclient.spec --noconfirm --clean
# Output: dist/AnkerClient/AnkerClient.exe

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (SPECPATH is injected by PyInstaller)
VERSION_FILE = ROOT / "build" / "version_info.txt"

hiddenimports = [
    *collect_submodules("anker_client"),
    "keyring.backends.Windows",
    "win32timezone",
    "win32com.shell",
    "win32crypt",
    "pythoncom",
    "pywintypes",
    "PyQt6.sip",
    "PyQt6.QtSvg",
    "PyQt6.QtNetwork",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "lxml.etree",
    "lxml._elementpath",
]

a = Analysis(
    [str(ROOT / "anker_client" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "anker_client" / "resources"), "anker_client/resources")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "pytestqt", "IPython", "matplotlib", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AnkerClient",
    icon=str(ROOT / "anker_client" / "resources" / "icon.ico"),
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-compressed Qt DLLs trigger antivirus false positives and slow startup
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AnkerClient",
)
