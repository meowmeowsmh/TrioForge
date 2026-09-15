@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  TrioForge - start it hidden, open it in its own window. No console, no
rem  browser, no .exe. The server lives silently in the background; the app
rem  opens in a WebView2 window (TrioForge in the title bar, no tabs/address bar).
rem ---------------------------------------------------------------------------

rem Project folder without the trailing backslash (it would escape the quote).
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem Console-less Python. Prefer TrioForge.exe (the launcher creates it on first run)
rem so even this short-lived process shows as TrioForge in Task Manager instead of
rem "Python"; fall back to the venv pythonw, then the system pythonw.
rem On a first run the launcher creates the venv (and installs dependencies) itself.
set "PY=%ROOT%\.venv\Scripts\TrioForge.exe"
if not exist "%PY%" set "PY=%ROOT%\.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
    where pyw >nul 2>&1 && (set "PY=pyw -3") || (set "PY=pythonw")
)

rem pythonw is a GUI-subsystem program, so cmd launches it and returns at once;
rem the launcher keeps hosting in the background. --window opens TrioForge's own
rem WebView2 window (it waits for the server itself); --no-browser stops the app
rem from opening a browser tab; --background-update means the app opens straight
rem away and any update check happens quietly afterwards, with no git window.
%PY% "%ROOT%\py\tools\launcher.py" "%ROOT%" --no-banner --no-browser --window --detach --background-update

endlocal
