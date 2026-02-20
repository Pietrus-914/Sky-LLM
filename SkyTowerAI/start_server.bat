@echo off
title SkyTower-AI Server
echo ================================================
echo          SkyTower-AI Trading Server
echo ================================================
echo.

cd /d "%~dp0python"

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

REM Check if virtual environment exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Install dependencies
echo Checking dependencies...
pip install -r requirements.txt --quiet

REM Create logs directory
if not exist "logs" mkdir logs

REM Check for .env file
if not exist ".env" (
    echo.
    echo WARNING: .env file not found!
    echo Creating template .env file...
    (
        echo # SkyTower-AI Configuration
        echo # Add your API keys here
        echo.
        echo # For LLM decisions (optional - will use rule-based without it^)
        echo ANTHROPIC_API_KEY=
        echo # or
        echo OPENAI_API_KEY=
        echo.
        echo # Optional: Finnhub API for additional calendar data
        echo FINNHUB_API_KEY=
        echo.
        echo # MT5 Settings (for direct MT5 integration^)
        echo MT5_LOGIN=
        echo MT5_PASSWORD=
        echo MT5_SERVER=PurpleTrading-MT5
    ) > .env
    echo.
    echo Please edit .env file with your API keys
    echo System will work without LLM keys (using rule-based decisions^)
    echo.
)

echo.
echo Starting SkyTower-AI Server...
echo ================================================
python server.py

pause
