@echo off
setlocal enabledelayedexpansion
title TrioForge

rem ------------------------------------------------------------
rem TrioForge Windows launcher - finds the LATEST Python and runs.
rem
rem  1. `py -3`  -> runs the newest installed Python 3
rem  2. `python` -> fallback
rem  3. neither  -> install the latest Python via winget, then continue
rem  4. no winget-> open the Microsoft Store page and stop
rem
rem No manual downloading and no hardcoded version number.
rem ------------------------------------------------------------

cd /d "%~dp0"

set "PY_CMD="

rem 1) The `py` launcher selects the latest installed Python 3.
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
    goto :have_python
)

rem 2) Fallback: a `python` on PATH.
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=python"
    goto :have_python
)

rem 3) Neither - install the latest Python with winget.
echo.
echo [TrioForge] Python was not found. Installing the latest Python with winget...
echo.

where winget >nul 2>nul
if not %errorlevel%==0 (
    goto :no_winget
)

rem Install the newest Python 3 (unversioned id resolves to latest stable;
rem fall back to a known-good version if that id is unavailable).
winget install --id Python.Python.3 -e --source winget --accept-package-agreements --accept-source-agreements
if not %errorlevel%==0 (
    winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements
)
if not %errorlevel%==0 (
    echo.
    echo [TrioForge] winget install failed. Open the Microsoft Store, install
    echo            "Python", then run this launcher again.
    start "" "https://apps.microsoft.com/detail/9NRWMJP3717K"
    pause
    exit /b 1
)

echo.
echo [TrioForge] Python installed. Refreshing PATH for this window...
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MACHINE_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
set "PATH=%PATH%;%MACHINE_PATH%;%USER_PATH%"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY_CMD=python"
)

if "%PY_CMD%"=="" (
    echo.
    echo [TrioForge] Python was installed, but this window can't see it yet.
    echo [TrioForge] Close this window, open a NEW Command Prompt, and run
    echo            application.bat again.
    pause
    exit /b 1
)
goto :have_python

:no_winget
echo.
echo [TrioForge] winget was not found (older Windows).
echo [TrioForge] Install Python from the Microsoft Store (latest version), then
echo            run this launcher again.
start "" "https://apps.microsoft.com/detail/9NRWMJP3717K"
pause
exit /b 1

:have_python
echo.
echo [TrioForge] Using latest Python: %PY_CMD%
echo.
echo ============================================================
echo   TrioForge is ready. Press Launch (Enter) to start it.
echo ============================================================
pause
%PY_CMD% py\tools\launcher.py %*
if errorlevel 1 (
    echo.
    echo TrioForge failed to start. See the error above.
    pause
)
endlocal
