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
    echo ✅ Ollama is already running.
) else (
    echo ⚙️  Ollama not running. Starting Ollama serve in a new window...
    start "Ollama Server" cmd /k "ollama serve & exit"
    
    :: Wait until Ollama is ready (ping its API)
    echo Waiting for Ollama to be ready...
    :wait_ollama
    timeout /t 2 /nobreak >nul
    curl -s -o nul -w "%%{http_code}" http://localhost:11434/api/tags | find "200" >nul
    if %ERRORLEVEL% neq 0 goto wait_ollama
    echo ✅ Ollama is ready.
)

:: ── Start the AI Chat App (Flask) in a new window ──────────
echo Starting AI Chat App...
start "AI Chat App" cmd /k "python app.py & exit"

echo.
echo All services are running. Close this window when you're done.
echo.
pause