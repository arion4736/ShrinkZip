@echo off
REM Builds dist\ShrinkZip.exe on Windows. Requires Python 3.10+ from python.org.
setlocal
python -m pip install --upgrade pip || goto :fail
python -m pip install customtkinter pillow py7zr pyinstaller pytest || goto :fail

echo Running tests...
python -m pytest -q || goto :fail

REM Bundle 7-Zip (for .7z / .rar) if it is installed
if exist "C:\Program Files\7-Zip\7z.exe" (
  copy /Y "C:\Program Files\7-Zip\7z.exe" . >nul
  copy /Y "C:\Program Files\7-Zip\7z.dll" . >nul
) else (
  echo NOTE: 7-Zip not found - the exe will not be able to open .7z / .rar files.
)

python -m PyInstaller --clean shrink_unzip_gui.spec || goto :fail
echo.
echo Done: dist\ShrinkZip.exe
goto :eof
:fail
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
