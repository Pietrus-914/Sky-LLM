# deploy_ea.ps1 — jedyna wspierana droga wgrywania EA po zmianach w mt5/.
#
# Dlaczego ten skrypt istnieje: źródła EA żyją w KILKU miejscach (repo +
# datafoldery każdego terminala MetaQuotes), a MetaEditor otwierany z MT5
# pokazuje DATAFOLDER, nie repo. Dwukrotnie (18.07.2026, lokalnie i na
# serwerze) kompilacja starej kopii z datafolderu dała błąd "Init(...10
# argumentów...)" mimo poprawnego repo. Do tego metaeditor64 to aplikacja
# GUI — wywołany bez -Wait zostawia STARY log i czyta się wynik poprzedniej
# kompilacji. Ten skrypt eliminuje obie pułapki:
#
#   1. kompiluje źródła Z REPO (kasuje log, czeka na proces, sprawdza wynik
#      ORAZ świeżość .ex5),
#   2. nadpisuje świeżymi źródłami każdą kopię mq5/mqh znalezioną w
#      datafolderach terminali (żeby MetaEditor GUI też widział aktualne),
#   3. wgrywa świeże binarki .ex5 wszędzie tam, gdzie terminale ich szukają.
#
# Użycie (prawy klik -> Run with PowerShell, albo):
#   powershell -ExecutionPolicy Bypass -File .\deploy_ea.ps1
#   powershell ... -File .\deploy_ea.ps1 -MetaEditor "C:\Program Files\XXX\metaeditor64.exe"
#
# Po skrypcie: zrestartuj terminal MT5 (albo zdejmij i podepnij EA na
# wykresach) — dopiero to laduje nowa binarke.

param(
    [string]$MetaEditor = ""
)

$ErrorActionPreference = "Stop"
$repoMt5 = Join-Path $PSScriptRoot "mt5"
if (-not (Test-Path (Join-Path $repoMt5 "SkyTowerAI_EA.mq5"))) {
    Write-Host "BLAD: nie znaleziono mt5\SkyTowerAI_EA.mq5 obok skryptu" -ForegroundColor Red
    exit 1
}

# --- 1. Znajdz metaeditor64.exe -----------------------------------------
if (-not $MetaEditor) {
    $candidates = @(
        "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe"
    )
    $candidates += Get-ChildItem "C:\Program Files*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "metaeditor64.exe" }
    $MetaEditor = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $MetaEditor -or -not (Test-Path $MetaEditor)) {
    Write-Host "BLAD: nie znaleziono metaeditor64.exe — podaj -MetaEditor <sciezka>" -ForegroundColor Red
    exit 1
}
Write-Host "MetaEditor: $MetaEditor"

# --- 2. Kompilacja Z REPO (z weryfikacja swiezosci) ---------------------
$compiled = @{}
foreach ($name in @("SkyTowerAI_EA", "SkyTower_Zones")) {
    $src = Join-Path $repoMt5 "$name.mq5"
    $log = Join-Path $repoMt5 "$name.log"
    $ex5 = Join-Path $repoMt5 "$name.ex5"
    if (Test-Path $log) { Remove-Item $log -Force }
    $before = Get-Date
    Start-Process -FilePath $MetaEditor -ArgumentList "/compile:`"$src`"", "/log:`"$log`"" -Wait
    Start-Sleep -Milliseconds 500
    if (-not (Test-Path $log)) {
        Write-Host "BLAD: brak logu kompilacji dla $name" -ForegroundColor Red
        exit 1
    }
    $result = (Get-Content $log -Encoding Unicode | Select-String "Result").Line
    Write-Host "$name -> $result"
    if ($result -notmatch "Result: 0 errors") {
        Write-Host "BLAD KOMPILACJI — szczegoly:" -ForegroundColor Red
        Get-Content $log -Encoding Unicode | Select-String "error" | Select-Object -First 10
        exit 1
    }
    $bin = Get-Item $ex5
    if ($bin.LastWriteTime -lt $before) {
        Write-Host "BLAD: $name.ex5 jest STARY ($($bin.LastWriteTime)) — kompilacja nie zapisala binarki" -ForegroundColor Red
        exit 1
    }
    $compiled[$name] = $ex5
}

# --- 3. Synchronizacja datafolderow wszystkich terminali ----------------
$sources = @("SkyTowerAI_EA.mq5", "SkyTowerAI_Zones.mqh", "SkyTowerAI_Panel.mqh",
             "SkyTower_Zones.mq5")
$terminals = Get-ChildItem "$env:APPDATA\MetaQuotes\Terminal" -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "MQL5") }
if (-not $terminals) {
    Write-Host "UWAGA: nie znaleziono zadnych datafolderow terminali MetaQuotes" -ForegroundColor Yellow
}

foreach ($term in $terminals) {
    $mql5 = Join-Path $term.FullName "MQL5"
    Write-Host "`nTerminal $($term.Name):"

    # 3a. Nadpisz KAZDA istniejaca kopie zrodel (stare kopie = zrodlo bledow)
    foreach ($srcName in $sources) {
        $copies = Get-ChildItem $mql5 -Recurse -Filter $srcName -ErrorAction SilentlyContinue
        foreach ($copy in $copies) {
            Copy-Item (Join-Path $repoMt5 $srcName) $copy.FullName -Force
            Write-Host "  zrodlo zsynchronizowane: $($copy.FullName.Substring($mql5.Length))"
        }
    }

    # 3b. Wgraj swieze binarki tam, gdzie terminal ich uzywa
    foreach ($name in $compiled.Keys) {
        $binName = "$name.ex5"
        $targets = Get-ChildItem $mql5 -Recurse -Filter $binName -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName }
        if (-not $targets) {
            # domyslne lokalizacje przy pierwszym deployu na te maszyne
            $targets = if ($name -eq "SkyTower_Zones") {
                @((Join-Path $mql5 "Indicators\$binName"))
            } else {
                @((Join-Path $mql5 "Experts\$binName"))
            }
        }
        foreach ($t in $targets) {
            New-Item -ItemType Directory -Force (Split-Path $t) | Out-Null
            Copy-Item $compiled[$name] $t -Force
            $stamp = (Get-Item $t).LastWriteTime
            Write-Host "  binarka wgrana:          $($t.Substring($mql5.Length))  ($stamp)"
        }
    }
}

Write-Host "`nGOTOWE. Zrestartuj terminal MT5 (albo zdejmij i podepnij EA na wykresach)," -ForegroundColor Green
Write-Host "zeby zaladowal nowa binarke." -ForegroundColor Green
