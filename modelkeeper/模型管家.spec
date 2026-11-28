# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/WorkSpace/模型管家_v1.0.17/main.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/WorkSpace/模型管家_v1.0.17/modelkeeper/assets/keeper.ico', 'assets'), ('D:/WorkSpace/模型管家_v1.0.17/modelkeeper/assets/prices_preset.json', 'assets')],
    hiddenimports=['tomllib', 'yaml'],
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
    name='模型管家',
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
    version='D:/WorkSpace/模型管家_v1.0.17/version_info.txt',
    icon=['D:/WorkSpace/模型管家_v1.0.17/modelkeeper/assets/keeper.ico'],
)
