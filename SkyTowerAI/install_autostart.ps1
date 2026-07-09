# SkyTower-AI — instalator autostartu dla maszyny 24/7 (bez Dockera)
# Uruchom raz (prawym -> Run with PowerShell). Tworzy skroty w folderze
# Autostart biezacego uzytkownika: serwer (petla watchdog) + terminal MT5.
# Do tego wylacza usypianie komputera.

$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell

# 1. Serwer z auto-restartem
$serverBat = Join-Path $PSScriptRoot 'start_server_24_7.bat'
$sc = $ws.CreateShortcut((Join-Path $startup 'SkyTower-Server.lnk'))
$sc.TargetPath = $serverBat
$sc.WorkingDirectory = $PSScriptRoot
$sc.Description = 'SkyTower-AI trading server (auto-restart loop)'
$sc.Save()
Write-Host "[OK] Autostart serwera: $startup\SkyTower-Server.lnk"

# 2. Terminal MT5 (szuka typowych instalacji)
$mt5Candidates = @(
    'C:\Program Files\Purple Trading MT5 Terminal\terminal64.exe',
    'C:\Program Files\Purple Trading MT5\terminal64.exe',
    'C:\Program Files\MetaTrader 5\terminal64.exe'
)
$mt5 = $mt5Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($mt5) {
    $sc2 = $ws.CreateShortcut((Join-Path $startup 'SkyTower-MT5.lnk'))
    $sc2.TargetPath = $mt5
    $sc2.WorkingDirectory = (Split-Path $mt5)
    $sc2.Save()
    Write-Host "[OK] Autostart MT5: $mt5"
} else {
    Write-Host "[!] Nie znalazlem terminal64.exe - dodaj skrot do MT5 w shell:startup recznie"
}

# 3. Zasilanie: nie usypiaj (monitor moze gasnac)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
Write-Host "[OK] Usypianie wylaczone (monitor gasnie po 10 min)"

Write-Host ""
Write-Host "Zostalo recznie:"
Write-Host " - auto-logon Windows: netplwiz -> odznacz wymaganie hasla (MT5 potrzebuje zalogowanej sesji po restarcie)"
Write-Host " - synchronizacja zegara: Task Scheduler -> codziennie 'w32tm /resync'"
Write-Host " - Windows Update: godziny aktywnosci tak, by restarty wypadaly w weekend"
