@echo off
title SkyTower-AI System Test
echo ================================================
echo          SkyTower-AI System Test
echo ================================================
echo.

cd /d "%~dp0python"

REM Activate virtual environment
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo Virtual environment not found. Run start_server.bat first.
    pause
    exit /b 1
)

echo.
echo [1/4] Testing Calendar Fetcher...
echo ------------------------------------------------
python -c "from calendar_fetcher import CalendarAggregator; c = CalendarAggregator(); events = c.get_upcoming_events(hours_ahead=168); print(f'Found {len(events)} events')"
if %errorlevel% neq 0 (
    echo FAILED: Calendar Fetcher
) else (
    echo PASSED: Calendar Fetcher
)

echo.
echo [2/4] Testing COT Analyzer...
echo ------------------------------------------------
python -c "from cot_analyzer import COTAnalyzer; c = COTAnalyzer(); r = c.analyze_currency('CAD'); print(f'CAD Signal: {r.get(\"signal\", \"N/A\")}')"
if %errorlevel% neq 0 (
    echo FAILED: COT Analyzer
) else (
    echo PASSED: COT Analyzer
)

echo.
echo [3/4] Testing Sentiment Analyzer...
echo ------------------------------------------------
python -c "from sentiment_analyzer import SentimentAggregator; s = SentimentAggregator(); r = s.get_currency_sentiment('NZD'); print(f'NZD Signal: {r.get(\"signal\", \"N/A\")}')"
if %errorlevel% neq 0 (
    echo FAILED: Sentiment Analyzer
) else (
    echo PASSED: Sentiment Analyzer
)

echo.
echo [4/4] Testing Decision Engine...
echo ------------------------------------------------
python -c "from llm_decision_engine import LLMDecisionEngine; e = LLMDecisionEngine(); d = e.get_next_trade_recommendation(); print(f'Decision: {d.direction if d else \"No events\"}' if d else 'No tradeable events found')"
if %errorlevel% neq 0 (
    echo FAILED: Decision Engine
) else (
    echo PASSED: Decision Engine
)

echo.
echo ================================================
echo Test Complete!
echo ================================================
pause
