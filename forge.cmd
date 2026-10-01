@echo off
rem ============================================================
rem  forge.cmd - Windows launcher for the TrioForge terminal client.
rem
rem      forge                        interactive session
rem      forge what is a GGUF?        one-shot question
rem      forge --echo                 offline demo, no model needed
rem      forge --version              print the version
rem      forge --help                 all options
rem
rem  Same program as ./forge on Linux/macOS. Run install.ps1 once to get
rem  `forge` and `trioforge` on your PATH.
rem ============================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem Tell the CLI which name the user typed, so --version and --help print that
rem name - the same thing ./forge does with basename "$0". The install.ps1 shims
rem set it to the name they were called as (trioforge.cmd reports "trioforge");
rem running forge.cmd directly falls back to "forge".
if not defined FORGE_PROG set "FORGE_PROG=forge"

rem The project venv, once it exists, is the interpreter that has the deps.
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY="
if not defined PY for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
    echo forge: no Python found. Install Python 3, or run TrioForge.bat once. 1>&2
    exit /b 1
)

rem The tui package lives under py\, alongside app.py and providers\.
set "PYTHONPATH=%ROOT%\py;%PYTHONPATH%"

"%PY%" -m tui %*
exit /b %ERRORLEVEL%
