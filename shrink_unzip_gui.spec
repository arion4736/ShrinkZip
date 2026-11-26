# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build configuration for ShrinkZip (one-file, windowed).
#
#   pip install -r requirements.txt pyinstaller
#   pyinstaller --clean --noconfirm shrink_unzip_gui.spec      ->  dist/ShrinkZip.exe
#
# Optional files next to this spec are bundled when present:
#   app_icon.ico   window / exe icon
#   7z.exe 7z.dll  native 7-Zip engine (needed for RAR; ship 7-Zip's license with releases)
#
# The CI self-test build reuses this spec with three environment variables, so the
# tested bundle uses exactly the same dependency collection as the shipped exe:
#   SHRINKZIP_ENTRY, SHRINKZIP_NAME, SHRINKZIP_CONSOLE
import os
from PyInstaller.utils.hooks import collect_all

ENTRY = os.environ.get('SHRINKZIP_ENTRY', 'shrink_unzip_gui.py')
NAME = os.environ.get('SHRINKZIP_NAME', 'ShrinkZip')
CONSOLE = os.environ.get('SHRINKZIP_CONSOLE', '0') == '1'

datas, binaries, hiddenimports = [], [], ['shrinkzip_engine']
for pkg in ('customtkinter', 'py7zr', 'rarfile'):      # GUI assets + all 7z back-ends
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as exc:                            # rarfile is optional
        print(f'[spec] {pkg} not collected: {exc}')

icon = 'app_icon.ico' if os.path.exists('app_icon.ico') else None
if icon:
    datas.append(('app_icon.ico', '.'))
for f in ('7z.exe', '7z.dll'):
    if os.path.exists(f):
        binaries.append((f, '.'))

a = Analysis([ENTRY], pathex=['.'], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name=NAME, debug=False, strip=False,
          upx=False,                # UPX-packed exes trigger far more antivirus false positives
          console=CONSOLE, icon=icon)
