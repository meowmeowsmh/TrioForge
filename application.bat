@echo off
title TrioForge Launcher
setlocal enabledelayedexpansion

:: ============================================================
:: MENU LOOP
:: ============================================================
:menu
cls
echo ========================================
echo   TrioForge Launcher
echo ========================================
echo.
echo Select your operating system:
echo   1) Windows (run natively)
echo   2) Linux / macOS (requires bash / Git Bash / WSL)
echo   3) Auto-detect
echo.
choice /C 123 /N /M "Enter your choice (1,2,3): "
set "CHOICE=%errorlevel%"

:: Choice 1 – Windows
if "%CHOICE%"=="1" goto :windows_main

:: Choice 2 – Linux/macOS (check bash)
if "%CHOICE%"=="2" goto :try_bash

:: Choice 3 – Auto-detect
if "%CHOICE%"=="3" goto :auto_detect

:: Invalid choice
echo ❌ Invalid choice. Please enter 1, 2, or 3.
pause
goto :menu

:try_bash
echo.
echo ⚠️  You selected Linux/macOS but you are running on Windows.
echo This mode requires bash (Git Bash, WSL, or Cygwin).
where bash >nul 2>nul
if errorlevel 1 (
    echo.
    echo ❌ ERROR: bash not found.
    echo Please install Git Bash (git-scm.com) or enable WSL.
    echo Or choose option 1 for Windows.
    pause
    goto :menu
)
echo ✅ bash found.
echo.
choice /C YN /N /M "Continue with Linux/macOS mode? (Y/N): "
if errorlevel 2 (
    echo Operation cancelled.
    pause
    goto :menu
)
:: Restart the same script with bash, passing "linux" as argument
echo 🚀 Restarting with bash...
bash "%~f0" linux
exit /b 0

:auto_detect
:: Since we are running in a batch file, we are on Windows.
:: But we can check if bash exists and ask the user.
echo 🔍 Auto-detecting...
where bash >nul 2>nul
if errorlevel 1 (
    echo Detected: Windows (no bash found)
    echo Running Windows mode...
    goto :windows_main
) else (
    echo Detected: Windows with bash available.
    echo.
    choice /C WN /N /M "Run in Windows mode (W) or Linux/macOS mode via bash (N)? (W/N): "
    if errorlevel 2 (
        echo Running Linux/macOS mode via bash...
        bash "%~f0" linux
        exit /b 0
    ) else (
        echo Running Windows mode...
        goto :windows_main
    )
)

# ============================================================
# This part is ignored by Windows – it's only for bash
# ============================================================
#!/bin/bash
# If the script is called with "linux", run the Unix part
if [[ "$1" == "linux" ]]; then
    goto_unix
fi
# Otherwise exit
exit 0

# ------------------------------------------------------------
# Unix code (only runs in bash)
# ------------------------------------------------------------
goto_unix() {
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'

    check_project() {
        [[ -f "$1/app.py" && -f "$1/cork_board.py" && -f "$1/notes.py" && -f "$1/zoompicleftandright.py" && -f "$1/llm_providers.py" ]] && echo "$1" && return 0
        return 1
    }

    if PROJECT_DIR=$(check_project "$PWD"); then
        echo -e "${GREEN}✅ Found project in current directory${NC}"
        cd "$PROJECT_DIR"
        run_app
    fi

    if [[ -n "$TRIOFORGE_HOME" ]]; then
        if PROJECT_DIR=$(check_project "$TRIOFORGE_HOME"); then
            echo -e "${GREEN}✅ Found project at TRIOFORGE_HOME${NC}"
            cd "$PROJECT_DIR"
            run_app
        fi
    fi

    echo -e "${YELLOW}🔍 Searching for TrioForge project... (may take a minute)${NC}"
    search_roots=("/" "$HOME" "/mnt" "/media")
    found=""
    for root in "${search_roots[@]}"; do
        [[ ! -d "$root" ]] && continue
        while IFS= read -r dir; do
            if check_project "$dir" >/dev/null; then
                found="$dir"
                break 2
            fi
        done < <(find "$root" -maxdepth 6 -type f -name "app.py" 2>/dev/null | while read -r f; do dirname "$f"; done | sort -u)
    done

    if [[ -z "$found" ]]; then
        echo -e "${RED}❌ ERROR: Could not find project.${NC}"
        echo "Required: app.py, cork_board.py, notes.py, zoompicleftandright.py, llm_providers.py"
        echo "Tip: Set TRIOFORGE_HOME environment variable."
        exit 1
    fi

    echo -e "${GREEN}✅ Found project at: $found${NC}"
    cd "$found"

    run_app() {
        echo -e "${GREEN}📁 Project folder: $(pwd)${NC}"
        if [[ ! -f ".deps_installed" ]]; then
            echo -e "${YELLOW}📦 Installing dependencies...${NC}"
            pip install -r requirements.txt
            if [[ $? -ne 0 ]]; then
                echo -e "${RED}❌ Failed to install dependencies.${NC}"
                exit 1
            fi
            touch .deps_installed
            echo -e "${GREEN}✅ Dependencies installed.${NC}"
        fi
        echo -e "${GREEN}✅ app.py is running...${NC}"
        echo "🌐 Open your browser at https://localhost:5001 or http://localhost:5001"
        echo
        python app.py
        read -p "Press Enter to exit..."
    }
    run_app
}

# ============================================================
# Windows code (main logic)
# ============================================================
:windows_main
:: ============================================================
:: 1. Check current folder
:: ============================================================
if exist "%cd%\app.py" if exist "%cd%\cork_board.py" if exist "%cd%\notes.py" if exist "%cd%\zoompicleftandright.py" if exist "%cd%\llm_providers.py" (
    set "PROJECT_DIR=%cd%"
    goto :run
)

:: ============================================================
:: 2. Check environment variable (TRIOFORGE_HOME)
:: ============================================================
if defined TRIOFORGE_HOME (
    if exist "%TRIOFORGE_HOME%\app.py" if exist "%TRIOFORGE_HOME%\cork_board.py" if exist "%TRIOFORGE_HOME%\notes.py" if exist "%TRIOFORGE_HOME%\zoompicleftandright.py" if exist "%TRIOFORGE_HOME%\llm_providers.py" (
        set "PROJECT_DIR=%TRIOFORGE_HOME%"
        goto :run
    )
)

:: ============================================================
:: 3. Find all fixed drives (C:\, D:\, etc.)
:: ============================================================
echo 🔍 Searching for TrioForge project on all drives...
echo This may take a minute. Please wait...

set "drives="
for /f "tokens=2*" %%a in ('fsutil fsinfo drives ^| find "Drives"') do set "drive_list=%%b"

:: ============================================================
:: 4. Search each drive for the project
:: ============================================================
for %%d in (!drive_list!) do (
    echo Scanning %%d
    for /f "delims=" %%f in ('where /r %%d app.py 2^>nul') do (
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
    echo Make sure the following files are together:
    echo   app.py, cork_board.py, notes.py, zoompicleftandright.py, llm_providers.py
    echo.
    echo Tip: Set TRIOFORGE_HOME environment variable to your project path.
    pause
    exit /b 1
)

:: ============================================================
:: 5. Run the app
:: ============================================================
:run
cd /d "%PROJECT_DIR%"
echo 📁 Project folder: %cd%

if not exist ".deps_installed" (
    echo 📦 Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ❌ Failed to install dependencies.
        pause
        exit /b 1
    )
    type nul > .deps_installed
    echo ✅ Dependencies installed.
)

echo ✅ app.py is running...
echo 🌐 Open your browser at https://localhost:5001 or http://localhost:5001
echo.
python app.py
pause