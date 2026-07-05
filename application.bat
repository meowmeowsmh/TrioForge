@echo off
title AI Chat Launcher

:: Go to the folder where this .bat is located
cd /d "%~dp0"

:: ---------------------------------------------------------
:: Show current folder and check for app.py
echo Current folder: %cd%
if exist app.py (
    echo ✅ app.py found.
) else (
    echo ❌ ERROR: app.py not found here.
    pause
    exit /b 1
)

:: ---------------------------------------------------------
:: Check Python
where python >nul 2>nul
if %errorlevel% equ 0 (
    echo ✅ Python found.
) else (
    echo ❌ Python not in PATH. Please install Python.
    pause
    exit /b 1
)

:: ---------------------------------------------------------
:: Check Ollama
where ollama >nul 2>nul
if %errorlevel% equ 0 (
    echo ✅ Ollama found.
) else (
    echo ⚠️  Ollama not found in PATH. It may not be installed.
)

:: ---------------------------------------------------------
:: 1. Install dependencies (if not already done)
if not exist ".deps_installed" (
    echo 📦 Installing dependencies in a new window...
    :: FIX: Added --user to avoid admin permissions and save space!
    start "Installing Dependencies" cmd /k "pip install --user -r requirements.txt && type nul > .deps_installed && echo ✅ Done. Close this window."
) else (
    echo ✅ Dependencies already installed.
)

:: ---------------------------------------------------------
:: 2. Start Ollama (if not running)
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe" >NUL
if %errorlevel% equ 0 (
    echo ✅ Ollama is already running.
) else (
    echo 🚀 Starting Ollama in a new window...
    start "Ollama Server" cmd /k "ollama serve"
)

:: ---------------------------------------------------------
:: 3. Start the Flask app
echo 🚀 Starting AI Chat App in a new window...
start "AI Chat App" cmd /k "python app.py"

:: ---------------------------------------------------------
echo.
echo ✅ All windows have been launched.
echo    - Dependencies window (only if first run)
echo    - Ollama Server (if not already running)
echo    - AI Chat App (Flask)
echo.
echo If any window closes immediately, the command inside it failed.
echo To see the error, run the command manually from the same folder.
pause