@echo off
REM ============================================================
REM  SkyTower-AI — uruchomienie JEDNYM kliknieciem (bez Dockera)
REM  Odpala serwer (z auto-restartem) i terminal MT5.
REM  Mozna klikac wielokrotnie - nie zdubluje procesow.
REM ============================================================
title SkyTower-AI Start

REM --- 0. Port serwera: domyslnie 5555, mozna nadpisac w python\.env ---
set "SRV_PORT=5555"
if exist "%~dp0python\.env" (
    for /f "tokens=2 delims==" %%a in ('findstr /b "SKYTOWER_PORT=" "%~dp0python\.env"') do set "SRV_PORT=%%a"
)

REM --- 1. Serwer (tylko jesli jeszcze nie dziala) ---
REM Sprawdzamy tresc odpowiedzi, nie samo polaczenie - na porcie moze
REM siedziec obcy program (np. adb / emulator Androida)
curl -s -m 2 http://127.0.0.1:%SRV_PORT%/health 2>nul | "%SystemRoot%\System32\find.exe" "SkyTower-AI" >nul
if not errorlevel 1 (
    echo Serwer juz dziala na porcie %SRV_PORT%.
    goto srvdone
)
netstat -ano | "%SystemRoot%\System32\find.exe" ":%SRV_PORT% " | "%SystemRoot%\System32\find.exe" "LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo UWAGA: port %SRV_PORT% jest zajety przez INNY program:
    netstat -ano | "%SystemRoot%\System32\find.exe" ":%SRV_PORT% " | "%SystemRoot%\System32\find.exe" "LISTENING"
    echo Sprawdz co to za proces komenda: tasklist /FI "PID eq NUMER_Z_OSTATNIEJ_KOLUMNY"
    echo Zamknij go, ALBO przenies SkyTower na inny port dopisujac w python\.env
    echo linie SKYTOWER_PORT=5556 i zmieniajac w MT5 allowlist oraz InpServerPort.
    echo.
    pause
    exit /b 1
)
echo Uruchamiam serwer SkyTower-AI na porcie %SRV_PORT%...
start "SkyTower-AI Server" /min "%~dp0start_server_24_7.bat"
:srvdone

REM --- 2. Terminal MT5 (tylko jesli jeszcze nie dziala) ---
REM pelna sciezka do find.exe - w PATH moze byc uniksowy find (Git Bash)
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | "%SystemRoot%\System32\find.exe" /I "terminal64.exe" >nul
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
echo Gotowe. Dashboard: http://127.0.0.1:%SRV_PORT%/
echo To okno zamknie sie za 10 sekund.
ping -n 11 127.0.0.1 >nul
