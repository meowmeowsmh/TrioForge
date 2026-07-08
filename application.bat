:; # 2> /dev/null ; exec /bin/bash "$0" "$@" ; exit
@echo off
title TrioForge Launcher
setlocal enabledelayedexpansion
goto :windows_main

# ============================================================
# Shell (Linux/macOS) code starts here
# ============================================================
#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check current directory
check_project() {
    local dir="$1"
    [[ -f "$dir/app.py" && -f "$dir/cork_board.py" && -f "$dir/notes.py" && -f "$dir/zoompicleftandright.py" && -f "$dir/llm_providers.py" ]] && echo "$dir" && return 0
    return 1
}

if PROJECT_DIR=$(check_project "$PWD"); then
    echo -e "${GREEN}✅ Found project in current directory${NC}"
    cd "$PROJECT_DIR"
    goto_run
fi

# Check environment variable
if [[ -n "$TRIOFORGE_HOME" ]]; then
    if PROJECT_DIR=$(check_project "$TRIOFORGE_HOME"); then
        echo -e "${GREEN}✅ Found project at TRIOFORGE_HOME${NC}"
        cd "$PROJECT_DIR"
        goto_run
    fi
fi

# Search common mount points
echo -e "${YELLOW}🔍 Searching for TrioForge project... (may take a minute)${NC}"
search_roots=("/" "$HOME" "/mnt" "/media")
found=""
for root in "${search_roots[@]}"; do
    [[ ! -d "$root" ]] && continue
    echo "  Scanning $root..."
    while IFS= read -r dir; do
        if check_project "$dir" >/dev/null; then
            found="$dir"
            break 2
        fi
    done < <(find "$root" -maxdepth 6 -type f -name "app.py" 2>/dev/null | while read -r f; do dirname "$f"; done | sort -u)
done

if [[ -z "$found" ]]; then
    echo -e "${YELLOW}Full system scan (may take longer)...${NC}"
    while IFS= read -r dir; do
        if check_project "$dir" >/dev/null; then
            found="$dir"
            break
        fi
    done < <(find / -type f -name "app.py" 2>/dev/null | while read -r f; do dirname "$f"; done | sort -u)
fi

if [[ -z "$found" ]]; then
    echo -e "${RED}❌ ERROR: Could not find project.${NC}"
    echo "Required files: app.py, cork_board.py, notes.py, zoompicleftandright.py, llm_providers.py"
    echo "Tip: Set TRIOFORGE_HOME environment variable."
    exit 1
fi

echo -e "${GREEN}✅ Found project at: $found${NC}"
cd "$found"

# Run the app (shell)
goto_run() {
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

goto_run
exit 0

# ============================================================
# Windows code starts here
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