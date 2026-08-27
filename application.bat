@echo off
title TrioForge
cd /d "%~dp0"
python py\tools\launcher.py %*
if errorlevel 1 (
    echo.
    echo TrioForge failed to start. See the error above.
    pause
)
