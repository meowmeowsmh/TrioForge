@echo off
title TrioForge Launcher
cd /d "%~dp0"
python tools\launcher.py --menu %*
if errorlevel 1 (
    echo.
    echo TrioForge failed to start. See the error above.
    pause
)
