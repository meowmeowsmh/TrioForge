@echo off
title TrioForge Launcher
setlocal enabledelayedexpansion

:: ------------------------------------------------------------
:: 1. Check if we are already in the correct folder
:: ------------------------------------------------------------
if exist "%cd%\app.py" if exist "%cd%\cork_board.py" if exist "%cd%\notes.py" if exist "%cd%\zoompicleftandright.py" if exist "%cd%\llm_providers.py" (
    set "PROJECT_DIR=%cd%"
    goto :run
)

:: ------------------------------------------------------------
:: 2. Check environment variable (optional override)
:: ------------------------------------------------------------
if defined TRIOFORGE_HOME (
    if exist "%TRIOFORGE_HOME%\app.py" if exist "%TRIOFORGE_HOME%\cork_board.py" if exist "%TRIOFORGE_HOME%\notes.py" if exist "%TRIOFORGE_HOME%\zoompicleftandright.py" if exist "%TRIOFORGE_HOME%\llm_providers.py" (
        set "PROJECT_DIR=%TRIOFORGE_HOME%"
        goto :run
    )
)

:: ------------------------------------------------------------
:: 3. Search all fixed drives (C:, D:, E:, ...)
:: ------------------------------------------------------------
echo 🔍 Searching for TrioForge project on all drives...
echo This may take a minute. Please wait...

:: Get list of fixed drives
set "drives="
for /f "skip=1 tokens=1" %%a in ('wmic logicaldisk where drivetype=3 get name') do (
    set "drives=!drives! %%a"
)

:: Search each drive
for %%d in (!drives!) do (
    echo Scanning %%d...
    for /f "delims=" %%f in ('dir /s /b "%%d\app.py" 2^>nul') do (
        set "folder=%%~dpf"
        set "folder=!folder:~0,-1!"
        if exist "!folder!\cork_board.py" if exist "!folder!\notes.py" if exist "!folder!\zoompicleftandright.py" if exist "!folder!\llm_providers.py" (
            echo ✅ Found project at: !folder!
            set "PROJECT_DIR=!folder!"
            goto :found
        )
    )
)

:found
if not defined PROJECT_DIR (
    echo ❌ ERROR: Could not find a folder containing all required files.
    echo Make sure the following files are together: app.py, cork_board.py, notes.py, zoompicleftandright.py, llm_providers.py
    pause
    exit /b 1
)

:: ------------------------------------------------------------
:: 4. Run the app
:: ------------------------------------------------------------
:run
cd /d "%PROJECT_DIR%"
echo 📁 Project folder: %cd%

:: Install dependencies only once
if not exist ".deps_installed" (
    echo 📦 Installing dependencies from requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ❌ Failed to install dependencies. Check your internet and Python.
        pause
        exit /b 1
    )
    type nul > .deps_installed
    echo ✅ Dependencies installed.
)

echo ✅ app.py is running...
python app.py
pause