@echo off
title Claude Code Telegram Bot
cd /d "%~dp0"

echo ============================================================
echo         Claude Code Telegram Bot
echo ============================================================
echo.

REM Kill any existing bot processes
echo Stopping any existing bot instances...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting bot...
echo.

REM Activate virtual environment and run
call venv\Scripts\activate.bat
python run.py

REM Keep window open if there's an error
if errorlevel 1 (
    echo.
    echo Bot stopped with error. Press any key to close...
    pause >nul
)
