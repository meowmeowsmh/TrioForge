@echo off
title TrioForge Voice Agent
cd /d "%~dp0"
python py\tools\voice_agent.py %*
if errorlevel 1 (
    echo.
    echo Voice agent failed to start. See the error above.
    pause
)
