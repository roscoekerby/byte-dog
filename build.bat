@echo off
:: Builds dist\ByteDog.exe (onefile, windowed, self-elevating). See README "Build the EXE".
cd /d "%~dp0"
title ByteDog Builder

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    exit /b 1
)

echo Installing build dependencies...
pip install --quiet --upgrade pyinstaller psutil nvidia-ml-py pillow
if errorlevel 1 (
    echo [ERROR] pip install failed.
    exit /b 1
)

echo Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo Building ByteDog.exe ...
pyinstaller ByteDog.spec
if errorlevel 1 (
    echo [ERROR] Build failed. See output above.
    exit /b 1
)

echo.
echo Build complete: dist\ByteDog.exe
