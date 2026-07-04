@echo off
title AI Chat Launcher

:: ── Install Python dependencies ────────────────────────────
echo Installing/updating required packages from requirements.txt...
pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo ❌ Failed to install dependencies. Please check your internet connection and Python/pip setup.
    pause
    exit /b 1
)
echo ✅ Dependencies installed.

:: ── Check if Ollama is already running ─────────────────────
echo Checking if Ollama is already running...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo ✅ Ollama is already running (detected via system tray / process).
) else (
    echo ⚙️  Ollama not running. Starting Ollama serve in a new window...
    start "Ollama Server" cmd /k "ollama serve & exit"
    :: Wait for Ollama to initialize
    echo Waiting for Ollama to be ready...
    timeout /t 5 /nobreak >nul
)

:: ── Start the AI Chat App in a new window ──────────────────
echo Starting AI Chat App...
start "AI Chat App" cmd /k "python app.py & exit"

echo.
echo Both services are starting. Close this window when you're done.
echo.
pause