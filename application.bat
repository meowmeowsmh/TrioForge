@echo off
title AI Chat Launcher

:: ── Change to the script's own folder ──────────────────────
cd /d "%~dp0"

:: ── Check if app.py exists ──────────────────────────────────
if not exist "app.py" (
    echo ❌ ERROR: app.py not found in this folder!
    echo    Current folder: %cd%
    echo    Please place this .bat file in the same folder as app.py
    pause
    exit /b 1
)

:: ── 1. Install dependencies in a NEW window (and wait for it) ──
if not exist ".deps_installed" (
    echo 📦 Installing dependencies... (new window opens)
    start "Installing Dependencies" cmd /c "pip install -r requirements.txt && type nul > .deps_installed && echo ✅ Done. Close this window." 
    echo Waiting for installation to finish...
    :wait_for_pip
    timeout /t 2 /nobreak >nul
    if not exist ".deps_installed" goto wait_for_pip
    echo ✅ Installation complete.
) else (
    echo ✅ Dependencies already installed (skipping pip install).
)

:: ── 2. Start Ollama in a new window (if not already running) ──
echo Checking if Ollama is already running...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo ✅ Ollama is already running (skip starting).
) else (
    echo 🚀 Starting Ollama server in a new window...
    start "Ollama Server" cmd /k "ollama serve"
)

:: ── 3. Start the Flask app in a new window ──────────────────
echo 🚀 Starting AI Chat App in a new window...
start "AI Chat App" cmd /k "python app.py"

echo.
echo ✅ All windows launched.
echo   - Dependencies: closed automatically after install (if it was opened).
echo   - Ollama Server: running in its own window.
echo   - AI Chat App: running in its own window.
echo.
echo Close this launcher window whenever you want.
pause