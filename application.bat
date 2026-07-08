@echo off
title AI Chat Launcher
cd /d "%~dp0"

:: Check if dependencies are installed
if not exist ".deps_installed" (
    echo 📦 Installing dependencies from requirements.txt...
    pip install -r requirements.txt
    if %errorlevel% equ 0 (
        type nul > .deps_installed
        echo ✅ Dependencies installed.
    ) else (
        echo ❌ Failed to install dependencies. Check your internet connection and Python setup.
        pause
        exit /b 1
    )
)

echo ✅ app.py is running...
python app.py
pause