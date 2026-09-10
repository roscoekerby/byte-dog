# -*- mode: python ; coding: utf-8 -*-
# Build: pyinstaller ByteDog.spec  (or run build.bat). Output: dist\ByteDog.exe

a = Analysis(
    ['bytedog.py'],
    pathex=[],
    binaries=[],
    datas=[('ByteDog_256.ico', '.')],
    hiddenimports=['psutil', 'pynvml'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ByteDog',
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
    icon=['ByteDog_256.ico'],
    version='version_info.txt',
    # No uac_admin manifest on purpose: bytedog.main() relaunches this exe
    # via runas (so the UAC prompt names ByteDog), and declining the prompt
    # keeps a non-elevated instance running instead of no ByteDog at all.
)
