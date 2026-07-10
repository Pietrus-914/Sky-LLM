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

REM Sprawdzamy konkretny plik, nie sam folder - pusty/uszkodzony venv
REM (np. po nieudanej probie bez Pythona) jest kasowany i tworzony od nowa
if not exist "venv\Scripts\activate.bat" (
    echo Tworzenie srodowiska venv...
    if exist "venv" rmdir /s /q venv
    %PY_CMD% -m venv venv
)
if not exist "venv\Scripts\activate.bat" (
    echo ERROR: nie udalo sie utworzyc venv.
    echo Sprobuj recznie w folderze python: %PY_CMD% -m venv venv
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

REM Zaleznosci serwera (obowiazkowe). Celowo requirements.txt, NIE -windows:
REM pakiet MetaTrader5 bywa niedostepny dla najnowszych Pythonow, a pip przy
REM jednym bledzie porzuca cala liste - serwer zostawal bez flaska.
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo ERROR: instalacja zaleznosci nie powiodla sie. Ponawiam z pelnym logiem:
    pip install -r requirements.txt
    pause
    exit /b 1
)

REM MetaTrader5 jest opcjonalny (narzedzia dev, serwer go nie importuje)
pip install MetaTrader5 --quiet >nul 2>&1

REM Kontrola przed startem - lepszy jasny komunikat niz petla crashy
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo ERROR: zaleznosci wciaz niekompletne. Uruchom recznie w folderze python:
    echo   venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

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
