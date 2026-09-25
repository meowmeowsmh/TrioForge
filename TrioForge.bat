@echo off
rem ===========================================================================
rem  TrioForge - double-click THIS file. It is the only launcher there is.
rem
rem  There used to be five files that all meant "start TrioForge" - start.bat,
rem  start.vbs, start-web.bat, start-web.vbs and application.bat - and no way to
rem  tell which one to run. They are now one file and a flag.
rem
rem      TrioForge.bat             start it in your browser (the default; the
rem                                server runs hidden behind it)
rem      TrioForge.bat --window    start it in its own window
rem      TrioForge.bat --status    what is installed and what is running
rem      TrioForge.bat --update    check for updates now
rem      TrioForge.bat --help      show this text
rem
rem  Anything else after the file name goes to py\tools\launcher.py untouched,
rem  so every launcher option still works from here:
rem
rem      TrioForge.bat --install-shortcut    put the desktop icons back
rem      TrioForge.bat --remove-shortcut     take them away again
rem      TrioForge.bat --install-autostart   start the server at login
rem      TrioForge.bat --remove-autostart    stop starting it at login
rem      TrioForge.bat --verify              check the app's own files
rem      TrioForge.bat --menu                the interactive menu
rem
rem  Nothing has to be installed first: with no Python this installs one via
rem  winget, then creates the venv and installs the dependencies by itself.
rem ===========================================================================

setlocal EnableExtensions
cd /d "%~dp0"

rem Project folder without the trailing backslash (it would escape a quote).
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem A keypress prompt is right after a double-click and wrong for a scripted run:
rem in CI (or any unattended run) the window would sit there until the job times
rem out. So it is opt-out, and CI opts out automatically.
set "NOPAUSE="
if defined CI set "NOPAUSE=1"
if defined TRIOFORGE_NO_PAUSE set "NOPAUSE=1"

rem ------------------------------------------------------------- arguments ---
rem Two flags are ours because they are the everyday choice. Everything else
rem belongs to launcher.py and is passed through untouched.
set "MODE=browser"
set "EXTRA="

:parse
if "%~1"=="" goto :parsed
set "A=%~1"
if /i "%A%"=="--help" goto :usage
if /i "%A%"=="-h" goto :usage
if /i "%A%"=="/?" goto :usage
if /i "%A%"=="--window" (set "MODE=window" & shift & goto :parse)
if /i "%A%"=="--browser" (set "MODE=browser" & shift & goto :parse)
if defined EXTRA (set "EXTRA=%EXTRA% %A") else (set "EXTRA=%A")
shift
goto :parse
:parsed

set "MODEFLAGS=--no-banner --detach --background-update"
if /i "%MODE%"=="window" set "MODEFLAGS=--no-banner --no-browser --window --detach --background-update"

rem ---------------------------------------------------------------- Python ---
rem The project venv, once it exists, is the interpreter that has the deps.
set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYW%" set "PYW="
if not exist "%PY%" set "PY="
if defined PY goto :ready

rem No venv: this is the first run. Use whatever Python is already installed and
rem let launcher.py create the venv and install everything. `where` is resolved
rem to a FULL PATH so that every later use can be quoted - a username with a
rem space in it (C:\Users\John Smith\...) breaks an unquoted `py -3`.
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY set "PY=%%P"
if defined PY goto :ready
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if defined PY goto :ready

echo.
echo [TrioForge] Python was not found. Installing the latest Python with winget...
echo.
where winget >nul 2>nul
if errorlevel 1 goto :no_winget

winget install --id Python.Python.3 -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :no_winget

echo.
echo [TrioForge] Installed. Re-reading PATH so this window can see it...
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MACHINE_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
set "PATH=%PATH%;%MACHINE_PATH%;%USER_PATH%"
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY goto :path_stale
goto :ready

:no_winget
echo.
echo [TrioForge] That did not work. Install "Python" from the Microsoft Store,
echo            then run TrioForge.bat again.
start "" "https://apps.microsoft.com/detail/9NRWMJP3717K"
echo.
if not defined NOPAUSE pause
exit /b 1

:path_stale
echo.
echo [TrioForge] Python is installed, but this window cannot see it yet.
echo            Close this window and run TrioForge.bat again.
echo.
if not defined NOPAUSE pause
exit /b 1

rem ---------------------------------------------------------------- launch ---
:ready

rem A maintenance flag keeps the console: its whole point is the output.
if defined EXTRA goto :run_visible

rem The first run keeps it too - installing the dependencies takes minutes and
rem has to be watchable.
if not defined PYW goto :run_visible

rem Already relaunched by our own hidden copy: do the work and stop.
if defined TRIOFORGE_HIDDEN goto :run_hidden

rem A .bat that Explorer opens always shows a console, whatever it contains. If
rem this was a double-click there is nothing left to watch, so hand over to a
rem hidden copy and let this window close. From a terminal the console is the
rem whole point, so leave it alone.
echo "%cmdcmdline%" | find /i "%~nx0" >nul 2>nul
if errorlevel 1 goto :run_visible
set "TRIOFORGE_HIDDEN=1"
rem The mode has to be handed to the relaunched copy: --window is OURS, so it never
rem reaches EXTRA, and a "TrioForge (window)" shortcut would otherwise come back up
rem in the browser. Always pass one of the two explicitly - an empty -ArgumentList
rem would be one more thing to get wrong.
set "RELAUNCH=--browser"
if /i "%MODE%"=="window" set "RELAUNCH=--window"
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -ArgumentList '%RELAUNCH%' -WindowStyle Hidden" >nul 2>nul
if not errorlevel 1 exit /b 0
rem No usable PowerShell: fall through and start in this window instead.

:run_visible
if defined EXTRA (
    "%PY%" "%ROOT%\py\tools\launcher.py" "%ROOT%" %EXTRA%
) else (
    "%PY%" "%ROOT%\py\tools\launcher.py" "%ROOT%" %MODEFLAGS%
)
if errorlevel 1 (
    echo.
    echo TrioForge failed to start - the reason is above.
    if not defined NOPAUSE pause
)
exit /b 0

:run_hidden
"%PYW%" "%ROOT%\py\tools\launcher.py" "%ROOT%" %MODEFLAGS%
exit /b 0

rem ----------------------------------------------------------------- usage ---
:usage
echo.
echo   TrioForge - the one launcher
echo.
echo   TrioForge.bat             start it in your browser (default)
echo   TrioForge.bat --window    start it in its own window
echo   TrioForge.bat --status    what is installed and what is running
echo   TrioForge.bat --update    check for updates now
echo   TrioForge.bat --help      this text
echo.
echo   Every py\tools\launcher.py option works from here too, for example:
echo     --install-shortcut   --remove-shortcut
echo     --install-autostart  --remove-autostart
echo     --verify             --verify-baseline      --menu
echo.
exit /b 0
