@echo off
title TrioForge Voice Agent
cd /d "%~dp0"

rem ---------------------------------------------------------------- Python ---
rem Prefer the project venv: that is where TrioForge installed the packages, and
rem the voice agent imports from the same place. Falling back to a bare `python`
rem was wrong on any machine whose Python is only reachable through the `py`
rem launcher - which is the normal case on Windows.
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :run

set "PY="
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
    echo.
    echo [TrioForge] No Python found. Run TrioForge.bat once first - it installs it,
    echo            creates the venv and installs what the voice agent needs.
    echo.
    if not defined CI if not defined TRIOFORGE_NO_PAUSE pause
    exit /b 1
)

:run
"%PY%" py\tools\voice_agent.py %*
if errorlevel 1 (
    echo.
    echo Voice agent failed to start. See the error above.
    if not defined CI if not defined TRIOFORGE_NO_PAUSE pause
)
