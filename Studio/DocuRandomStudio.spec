# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:/donutbrowser-main/Studio/assets/docurandom.ico', 'assets'), ('D:/donutbrowser-main/Studio/flow-extension', 'flow-extension'), ('D:/donutbrowser-main/Studio/donut-extension', 'donut-extension'), ('D:/donutbrowser-main/Studio/third_party/docurandom-chat-bridge', 'chat-bridge')]
binaries = [('D:/donutbrowser-main/Studio/third_party/docurandom-codex/codex.exe', 'codex-cli'), ('D:/donutbrowser-main/portable-dev/Data/tools/ffmpeg.exe', 'ffmpeg'), ('D:/donutbrowser-main/portable-dev/Data/tools/ffprobe.exe', 'ffmpeg')]
datas += [('D:/donutbrowser-main/Studio/assets/research', 'assets/research')]
hiddenimports = ['PIL._tkinter_finder']

tmp_ret = collect_all('rapidocr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('websockets')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('ctranslate2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tokenizers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/donutbrowser-main/Studio/app.py'],
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
    name='DocuRandomStudio',
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
    version='D:/donutbrowser-main/Studio/build/version-info/DocuRandomStudio.txt',
    icon=['D:/donutbrowser-main/Studio/assets/docurandom.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DocuRandomStudio',
)
