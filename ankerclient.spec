# ankerclient.spec
a = Analysis(
    ['anker_client/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('anker_client/resources', 'anker_client/resources'),
    ],
    hiddenimports=[
        'keyring.backends.Windows',
        'keyring.backends.fail',
        'win32api',
        'win32con',
        'win32gui',
        'pywintypes',
        'PyQt6.sip',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest',
        '_pytest',
        'unittest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AnkerClient',
    icon='anker_client/resources/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
