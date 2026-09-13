@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  TrioForge - the light way: server hidden, app opens in your own browser.
rem  The memory difference is real: a browser tab reuses an engine that is already
rem  loaded (~150-300 MB), while TrioForge's own window loads a private WebView2
rem  engine (~600 MB). Everything else is identical - same server, same data.
rem  Use start.vbs instead when you want TrioForge in a window of its own.
rem ---------------------------------------------------------------------------

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY=%ROOT%\.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
    where pyw >nul 2>&1 && (set "PY=pyw -3") || (set "PY=pythonw")
)

rem No --window: nothing embedded is loaded. No --no-browser either, so the app
rem opens in your default browser as soon as it is up. --background-update keeps the
rem update check out of the way and hidden.
%PY% "%ROOT%\py\tools\launcher.py" "%ROOT%" --no-banner --detach --background-update

endlocal
