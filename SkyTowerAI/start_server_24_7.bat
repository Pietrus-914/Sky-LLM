@echo off
REM SkyTower-AI — launcher 24/7 (bez Dockera)
REM Restartuje serwer automatycznie po kazdej awarii (petla watchdog).
REM Autostart: uruchom install_autostart.ps1 albo wrzuc skrot do shell:startup
REM UWAGA dla edytujacych: zadnych nawiasow w liniach echo wewnatrz blokow if!
title SkyTower-AI Server (24/7)

cd /d "%~dp0python"

REM Znajdz Pythona: najpierw "python" z PATH, potem launcher "py -3"
REM (py dziala nawet gdy przy instalacji nie zaznaczono "Add to PATH")
set "PY_CMD=python"
python --version >nul 2>&1
if not errorlevel 1 goto pyfound
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto pyfound
)
echo ERROR: Nie znalazlem Pythona. Zainstaluj Python 3.10+ z https://python.org
echo najlepiej zaznaczajac "Add python.exe to PATH" przy instalacji.
pause
exit /b 1
:pyfound

if not exist "venv" (
    echo Tworzenie srodowiska venv...
    %PY_CMD% -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements-windows.txt --quiet

if not exist "logs" mkdir logs

if not exist ".env" (
    echo.
    echo UWAGA: brak pliku .env! Skopiuj go ze starego komputera do:
    echo   %~dp0python\.env
    echo Plik powinien zawierac klucz OPENROUTER_API_KEY oraz na czas testow
    echo linie SKYTOWER_FORCE_DECISION=true
    echo.
    pause
    exit /b 1
)

:loop
echo [%date% %time%] Start serwera >> logs\watchdog.log
python server.py
echo [%date% %time%] Serwer zakonczyl dzialanie, kod %errorlevel% - restart za 10s >> logs\watchdog.log
REM ping zamiast timeout: dziala tez bez interaktywnej konsoli (Task Scheduler)
ping -n 11 127.0.0.1 >nul
goto loop
