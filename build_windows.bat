@echo off
:: ============================================================
::  iAmped -- Build Script (Windows)
::  Produces a portable, self-contained app: dist\iAmped.exe
::
::  Saved settings (Plex URL + token) live in an "iAmped-data" folder
::  created next to the .exe on first run, so the whole thing is portable.
:: ============================================================
setlocal
cd /d "%~dp0"

echo.
echo  ========================================
echo   iAmped Build Script -- Windows
echo  ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install from https://python.org
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo  Using: %%i

echo.
echo  [1/3] Installing packages (app deps + PyInstaller)...
python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
    echo  [ERROR] pip install failed.
    pause & exit /b 1
)
echo  OK.

echo.
echo  [2/3] Cleaning old build artefacts...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo  [3/3] Building iAmped.exe (single self-contained file)...
python -m PyInstaller --noconfirm build_tools\iamped.spec
if errorlevel 1 (
    echo  [ERROR] PyInstaller failed.
    pause & exit /b 1
)

echo.
echo  ========================================
echo   Build complete!  dist\iAmped.exe
echo  ========================================
echo.
echo  Single self-contained file. An "iAmped-data" folder is created next to
echo  it on first launch to hold your Plex settings, library cache and audio.
echo.
pause
