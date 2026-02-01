@echo off
echo ============================================================
echo         Claude Code Chat API Server
echo ============================================================
echo.

REM Check if venv exists
if not exist "venv\Scripts\activate.bat" (
    echo [!] Virtual environment not found!
    echo     Run these commands first:
    echo.
    echo     python -m venv venv
    echo     venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Activate venv and run
call venv\Scripts\activate.bat

echo [*] Starting Flask API server...
echo.
python flask_server.py %*

pause
