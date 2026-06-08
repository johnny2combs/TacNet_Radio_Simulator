# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_web.py'],
    pathex=[],
    binaries=[('C:\\Users\\johnn\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\site-packages\\libmgrs.cp312-win_amd64.pyd', '.')],
    datas=[('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_config.ini', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_admin.html', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_planner.html', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\planner_data.json', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\Milradio_aar_v2.html', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\tabs', 'tabs'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\help', 'help'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\offline_map_data', 'offline_map_data'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\opus.dll', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\libopus-0.dll', '.')],
    hiddenimports=['cryptography', 'scipy.signal', 'numpy', 'wave', 'radio_recorder'],
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
    [],
    exclude_binaries=True,
    name='TacNet-Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\server.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TacNet-Server',
)
