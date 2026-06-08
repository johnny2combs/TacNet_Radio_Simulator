# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_config.ini', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\client.png', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\opus.dll', '.'), ('C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\libopus-0.dll', '.')]
binaries = [('C:\\Users\\johnn\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\site-packages\\libmgrs.cp312-win_amd64.pyd', '.')]
hiddenimports = ['PySide6', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'cryptography', 'scipy.signal', 'numpy', 'PySide6', 'pynput', 'pynput.keyboard', 'pynput.mouse', 'pyaudio']
tmp_ret = collect_all('mgrs')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\radio_client.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='TacNet-Client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\johnn\\Desktop\\Archive\\mil_radiov3\\mil_radiov3\\client.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TacNet-Client',
)
