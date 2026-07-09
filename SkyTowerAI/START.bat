@echo off
REM ============================================================
REM  SkyTower-AI — uruchomienie JEDNYM kliknieciem (bez Dockera)
REM  Odpala serwer (z auto-restartem) i terminal MT5.
REM  Mozna klikac wielokrotnie - nie zdubluje procesow.
REM ============================================================
title SkyTower-AI Start

REM --- 1. Serwer (tylko jesli jeszcze nie dziala) ---
curl -s -m 2 http://127.0.0.1:5555/health >nul 2>&1
if %errorlevel% neq 0 (
    echo Uruchamiam serwer SkyTower-AI...
    start "SkyTower-AI Server" /min "%~dp0start_server_24_7.bat"
) else (
    echo Serwer juz dziala.
)

REM --- 2. Terminal MT5 (tylko jesli jeszcze nie dziala) ---
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
if %errorlevel% equ 0 goto mt5done

if exist "C:\Program Files\Purple Trading MT5 Terminal\terminal64.exe" (
    echo Uruchamiam MT5...
    start "" "C:\Program Files\Purple Trading MT5 Terminal\terminal64.exe"
    goto mt5done
)
if exist "C:\Program Files\Purple Trading MT5\terminal64.exe" (
    echo Uruchamiam MT5...
    start "" "C:\Program Files\Purple Trading MT5\terminal64.exe"
    goto mt5done
)
echo Nie znalazlem MT5 - uruchom terminal recznie.
:mt5done

echo.
echo Gotowe. Dashboard: http://127.0.0.1:5555/
echo To okno zamknie sie za 10 sekund.
timeout /t 10 >nul
