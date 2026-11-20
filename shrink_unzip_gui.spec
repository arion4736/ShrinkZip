# -*- mode: python ; coding: utf-8 -*-
# Build:  pip install pyinstaller customtkinter pillow py7zr
#         pyinstaller shrink_unzip_gui.spec
# Optional files next to this spec are bundled when present:
#   app_icon.ico   - window / exe icon
#   7z.exe, 7z.dll - native 7-Zip engine (needed for RAR; see 7-Zip's license)
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all('customtkinter')
try:                                   # py7zr and its compression back-ends
    d, b, h = collect_all('py7zr')
    datas += d; binaries += b; hiddenimports += h
except Exception:
    pass
icon = 'app_icon.ico' if os.path.exists('app_icon.ico') else None
if icon:
    datas.append(('app_icon.ico', '.'))
for f in ('7z.exe', '7z.dll'):
    if os.path.exists(f):
        binaries.append((f, '.'))

a = Analysis(['shrink_unzip_gui.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports + ['shrinkzip_engine'], hookspath=[], runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='ShrinkUnzip', debug=False, strip=False,
          upx=False,            # UPX-packed exes trigger far more antivirus false positives
          console=False, icon=icon)
