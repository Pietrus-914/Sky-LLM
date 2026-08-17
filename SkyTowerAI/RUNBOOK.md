# SkyTower-AI — Plan uruchomienia krok po kroku

Stan na 30.07.2026: 696 testów zielonych, EA skompilowany (0 err/0 warn),
learning loop F0-F5 wdrożony (rejestrator ścieżek, learned stats w prompcie,
echo decision_id przez EA, ledger kalibracji per-model, panel modeli ensemble,
epizody + refleksje). Branch gpt_review: Stage 1-2 zacommitowane (26.07).
Tryb testowy FORCE_DECISION aktywny — **tylko konto DEMO**.
**Tryb podstawowy: NATYWNY Python (START.bat). Docker = wariant zapasowy.**

## NAJPROŚCIEJ (po jednorazowej instalacji): kliknij START.bat

`SkyTowerAI\START.bat` — odpala serwer (z auto-restartem po awarii) i MT5
jednym kliknięciem; można klikać wielokrotnie, nie zdubluje procesów.
Docker NIE jest potrzebny — serwer działa natywnie w Pythonie.
Wymagania jednorazowe: Python 3.10+ w PATH, plik `python\.env`
(OPENROUTER_API_KEY + SKYTOWER_FORCE_DECISION=true), MT5 skonfigurowany
(allowlist WebRequest, EA na wykresach — KROK 2 poniżej).
Autostart po restarcie komputera: uruchom raz `install_autostart.ps1`.

## Pliki, które biorą udział w uruchomieniu

| Rola | Ścieżka |
|------|---------|
| Serwer (natywnie) | `START.bat` / `start_server.bat` (Docker legacy: `docker-compose.yml`) |
| Klucz API | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\python\.env` (OPENROUTER_API_KEY) |
| EA — źródło | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.mq5` |
| EA — skompilowany | `...\mt5\SkyTowerAI_EA.ex5` → **już wgrany** do `%APPDATA%\MetaQuotes\Terminal\F225742ADC2EE896672C03839B31B81B\MQL5\Experts\` |
| Wskaźnik stref | `...\mt5\SkyTower_Zones.ex5` → **już wgrany** do `...\MQL5\Indicators\` |
| Logi/decyzje (host) | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\python\logs\` (decision_history.jsonl, event_reactions.jsonl, event_paths.jsonl, trade_history.jsonl, currency_regimes.json, decision_context/, server.log) |

## KROK 1 — Start serwera (natywnie, tryb podstawowy)

Kliknij `START.bat` (albo `start_server.bat` dla samego serwera).

Weryfikacja:
```powershell
curl http://127.0.0.1:5555/health          # -> {"status":"ok",...}
Get-Content python\logs\server.log -Tail 20  # -> banner "FORCE_DECISION TEST MODE IS ACTIVE"
```
Dashboard: http://127.0.0.1:5555/  Autostart po restarcie: `install_autostart.ps1`.

### Wariant zapasowy: Docker (LEGACY — od 10.07.2026 nieużywany)

```powershell
cd "C:\Users\pietr\Documents\Sky tower\SkyTowerAI"
docker compose up -d --build   # wymaga Docker Desktop z autostartem
```

## KROK 2 — MT5 (Purple Trading, konto DEMO)

1. Uruchom terminal: `C:\Program Files\Purple Trading MT5 Terminal\terminal64.exe`
   i zaloguj na konto **demo**.
2. **WebRequest allowlist** (jednorazowo): Tools → Options → Expert Advisors →
   zaznacz "Allow WebRequest for listed URL" i dodaj: `http://127.0.0.1:5555`
3. Włącz przycisk **Algo Trading** (górny pasek, ma być zielony).
4. Otwórz 4 wykresy: **NZDUSD, USDCAD, AUDUSD, GBPUSD** (sufiks brokera typu
   `.pro` nie przeszkadza — obsługiwany automatycznie). Timeframe dowolny (M5 ok).
5. Na każdy wykres przeciągnij z Navigator → Expert Advisors → **SkyTowerAI_EA**.
   Inputy zostaw domyślne — w szczególności:
   - `InpServerHost=127.0.0.1`, `InpServerPort=5555`
   - `InpDefaultMode=MODE_LLM_AUTO`
   - `InpPushMarketData=true`, `InpReportReactions=true`
   - `InpMinConfidence=0.5` może zostać — sygnały `forced:true` same omijają bramkę
6. W zakładce Experts (Toolbox) sprawdź log EA: powinno być połączenie z serwerem
   bez błędu 4014 (jeśli jest 4014 → wróć do punktu 2).

### Instrumenty nie-FX (od 17.08.2026): 5. wykres XAUUSD + routing eventów

Ta sama teza newsowa, inny instrument: eventy USD można kierować na **złoto
(XAUUSD)** — reaguje w % tak samo jak pary FX, a spread to 2-8% ruchu zamiast
40-100%. Mechanika: `python/instrument_profiles.py` (profil = jednostka pipsa
i klampy per instrument; **XAUUSD: 1 pip = $0.10**), routing w panelu
(Event Config → *Instrument Routing*, np. `USD:XAUUSD`) albo env
`SKYTOWER_INSTRUMENT_ROUTING`. Serwer bierze pierwszy instrument z listy,
którego wykres pcha świeże dane (≤30 min); brak danych = dotychczasowa para FX.

1. Otwórz wykres **XAUUSD** i podepnij **SkyTowerAI_EA** (wymaga binarki
   ≥ 17.08.2026 — patrz „Po zmianach w EA"). Inputy TYLKO na tym wykresie:
   - `InpPipSizeOverride = 0.10` (**obowiązkowe** — musi równać się `pip_size`
     profilu; bez tego EA liczy spread w centach i blokuje każde wejście)
   - `InpMaxSpreadPips = 15` ($1.50; twardy limit EA to <15 pipsów w tej jednostce)
   - `InpEmergencySpreadPips = 40` ($4.00)
   - `InpSlippage = 30` (punkty = $0.30)
   - `InpMaxMarginUsePercent = 50` (złoto u Purple ma dźwignię 1:100 — bez capu
     lot wyliczony z SL może przekroczyć wolny margin → retcode 10019)
   - strefy (`InpUseZoneIndicator`/`InpUseZoneBiasForDirection`) OFF
   Na wykresach FX inputy zostają domyślne (`InpPipSizeOverride=0`,
   `InpMaxMarginUsePercent=0`) — zachowanie bit w bit jak wcześniej.
2. Po podpięciu odczytaj w logu Experts linię **`SkyTower SPEC:`** — digits,
   point, efektywny pip, contract size, margin za 1 lot, spread. Jeśli
   `pip=` nie równa się 0.10 → popraw input. Wartości contract size / margin
   wpisz do notatek profilu, jeśli różnią się od założeń.
3. Włącz routing dopiero po tym: panel → *Instrument Routing* → `USD:XAUUSD`
   → Zapisz. Karta pokazuje na żywo, czy XAUUSD ma świeże dane z EA.
   Bez EA na wykresie XAUUSD routing jest bezpiecznym no-op.
4. Dry-run: `SKYTOWER_FAKE_EVENT_IN_SECONDS=240` (event USD) — sygnał trafi na
   `?pair=XAUUSD`, wykres FX dostanie „Not selected". Po teście USUŃ linię z .env.

Statystyki dla złota już są w `knowledge/learned_stats.json` (bloki `XAUUSD`
dla eventów USD 2023-26, 4 721 ścieżek z HistData) — prompt dostaje sekcję
INSTRUMENT (jednostki, zakresy SL/TP w pipsach 0.10 $, semantyka kierunku:
niespodzianka pro-USD ⇒ SELL XAUUSD).

## KROK 3 — Weryfikacja obiegu danych (po ~2 minutach)

```powershell
Get-Content python\logs\server.log -Tail 200 | Select-String "Market data received"
```
Powinny pojawiać się wpisy dla każdej pary co ~60 s. To znaczy: EA → serwer działa.

## KROK 4 (opcjonalnie) — Próba generalna z fałszywym eventem

Pełny cykl (analiza → sygnał → wejście 15 s przed → zarządzanie → wyjście)
bez czekania na realny kalendarz. Na koncie demo z podpiętym EA (USDCAD):

1. Dopisz w `python\.env` linię: `SKYTOWER_FAKE_EVENT_IN_SECONDS=240`
2. Zrestartuj serwer (zamknij okno i kliknij START.bat).
3. Obserwuj `python\logs\server.log` + zakładkę Experts w MT5.
   EA powinien otworzyć pozycję ~15 s przed "eventem" i sam ją zamknąć.
   Reakcje z fake eventów są oznaczane `test:true` i nie zaśmiecają historii.
4. **Po próbie USUŃ tę linię z .env** i zrestartuj serwer ponownie.

## KROK 5 — Eksploatacja (codziennie 2 minuty)

- Dashboard: http://127.0.0.1:5555/ (eventy, decyzje, logi)
- Zdrowie źródeł danych: `curl http://127.0.0.1:5555/api/datasources/status`
- Decyzje: `python\logs\decision_history.jsonl` (pole `forced` odróżnia wymuszone)
- Reakcje na eventy: `curl http://127.0.0.1:5555/api/event-reactions`
- Błędy: `Get-Content python\logs\server.log -Tail 2000 | Select-String "ERROR|WARNING"`
- Kalibracja decyzji: karta Calibration (tab AI) albo `curl http://127.0.0.1:5555/api/calibration`
- Reżimy walut: karta Currency Regimes albo `curl http://127.0.0.1:5555/api/regimes`

## Zatrzymanie / restart

Natywnie: zamknij okno serwera (EA przestanie dostawać sygnały = nie handluje);
START.bat wznawia. Po każdej zmianie w kodzie `python/` — restart okna serwera.
(Docker legacy: `docker compose stop/start`, `up -d --build` po zmianach.)

## Po zmianach w EA (rekompilacja + wgranie)

**UWAGA — metaeditor64 to aplikacja GUI: uruchomiona przez `&` NIE blokuje
konsoli.** Stary log zostaje wtedy na dysku i czytasz wynik POPRZEDNIEJ
kompilacji („0 errors" z zeszłego tygodnia). Zawsze: skasuj log, użyj
`Start-Process -Wait`, potem SPRAWDŹ datę/rozmiar `.ex5`.

```powershell
$me  = "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe"
$src = "C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.mq5"
$log = "C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.log"
if (Test-Path $log) { Remove-Item $log -Force }
Start-Process -FilePath $me -ArgumentList "/compile:`"$src`"","/log:`"$log`"" -Wait
Get-Content $log -Encoding Unicode | Select-String "Result"     # 0 errors, 0 warnings
Get-Item ($src -replace '\.mq5$','.ex5') | Select-Object Name, Length, LastWriteTime  # data MUSI być dzisiejsza!

Copy-Item "C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.ex5" "C:\Users\pietr\AppData\Roaming\MetaQuotes\Terminal\F225742ADC2EE896672C03839B31B81B\MQL5\Experts\" -Force
```
Potem w MT5: prawym na wykres → Expert Advisors → usuń i podepnij ponownie
(albo restart terminala).

**Uwaga (F2, 17.07.2026):** EA echo'uje `decision_id` w raportach — na maszynie
24/7 trzeba wgrać ZREKOMPILOWANY `.ex5` (stary EA działa, ale bez pełnego
spięcia decyzja→trade→reakcja przy restarcie serwera w trakcie pozycji).

## Learning loop (F0-F4) — operacje

System sam zbiera dane do nauki; nic nie trzeba klikać. Co gdzie jest:

- **Ścieżki cenowe WSZYSTKICH eventów** (traded/skipped): `python\logs\event_paths.jsonl`,
  podgląd `GET /api/event-paths?limit=50`. Mierzone serwerowo z M1 pushowanych przez EA.
- **Reżimy walut (auto)**: `GET/POST /api/regimes` + karta na dashboardzie
  (override ręczny działa do następnej decyzji banku). EUR/JPY/CHF bez wykresów = tylko seed/manual.
- **Wiedza w prompcie**: `python\knowledge\event_playbooks.json` (kuratorska, edytowalna)
  + `python\knowledge\learned_stats.json` (MASZYNOWA — nie edytować ręcznie!).
  Regeneracja statystyk (np. po miesiącach live danych):
  ```powershell
  cd "C:\Users\pietr\Documents\Sky tower\SkyTowerAI\python"
  python tools/build_learned_stats.py    # NIE pipe'ować przez head! Hot-reload bez restartu.
  ```
- **Kalibracja decyzji**: `GET /api/calibration` + karta Calibration (tab AI);
  linia w prompcie pojawia się automatycznie od n>=50 zmierzonych decyzji (bez forced).
- **Ensemble K (opcjonalny)**: `SKYTOWER_ENSEMBLE_K=3` w `python\.env` + restart.
  K równoległych calli LLM na decyzję: jednomyślność = trade, rozjazd = SKIP.
  KOSZT: K x cena modelu wejściowego per event. Default 1 (wyłączony).
- **Lineage**: decision_id spina decyzję → sygnał → trade → reakcję
  (pełny kontekst decyzji: `python\logs\decision_context\<id>.json`, cap 2000 plików).

## Migracja na komputer 24/7 — wariant BEZ DOCKERA (słabszy sprzęt)

Serwer działa natywnie w Pythonie — Docker nie jest wymagany. Ten wariant jest
lżejszy (bez WSL2) i zalecany na słabszych maszynach.

### Instalacja (raz)
1. Zainstaluj **Python 3.10+** z https://python.org — przy instalacji zaznacz
   **"Add Python to PATH"**. Zainstaluj też Git i Purple Trading MT5.
2. `git clone https://github.com/Pietrus-914/Sky-LLM.git`
3. Skopiuj ze starego komputera plik `.env` do `<repo>\SkyTowerAI\python\.env`.
   **Tylko na czas fazy demo** dopisz w nim linię (FORCE_DECISION = tryb
   testowy, wymusza BUY/SELL — NIGDY na koncie live, patrz "Przejście na LIVE"):
   ```
   SKYTOWER_FORCE_DECISION=true
   ```
4. Pierwszy start serwera (utworzy venv i zainstaluje zależności, potrwa parę minut):
   ```
   <repo>\SkyTowerAI\start_server_24_7.bat
   ```
   Ten launcher ma pętlę watchdog — po każdej awarii serwer wstaje sam po 10 s
   (restarty zapisują się w `python\logs\watchdog.log`).
5. Weryfikacja: `curl http://127.0.0.1:5555/health` + w oknie serwera banner
   FORCE_DECISION.
6. MT5: jak w KROKU 2 głównej instrukcji (allowlist WebRequest
   `http://127.0.0.1:5555`, Algo Trading ON, 4 wykresy + EA). Skompilowane
   `.ex5` przenieś ręcznie albo skompiluj na miejscu (sekcja B6 niżej).
7. **Autostart + zasilanie** — uruchom raz (prawym → Run with PowerShell):
   ```
   <repo>\SkyTowerAI\install_autostart.ps1
   ```
   Tworzy skróty autostartu (serwer + MT5) i wyłącza usypianie. Zostają dwa
   kroki ręczne, które skrypt wypisze na końcu: auto-logon (`netplwiz`)
   i synchronizacja zegara (`w32tm /resync` w Task Schedulerze).
8. Restart komputera na próbę: po zalogowaniu mają same wstać serwer
   (okno "SkyTower-AI Server (24/7)") i MT5 z wykresami.

### Aktualizacja kodu na maszynie 24/7
```powershell
cd <repo>; git pull
# zrestartuj okno serwera (zamknij i odpal start_server_24_7.bat, albo restart komputera)
```

## Migracja na komputer 24/7 — wariant z Dockerem

Wymaganie: docelowa maszyna to **Windows** (MT5 działa natywnie; w Dockerze jest
tylko serwer Python).

### A. Na starym komputerze
1. Commit + push wszystkich zmian do GitHub (`Pietrus-914/Sky-LLM`).
2. Skopiuj ręcznie (pendrive/dysk sieciowy) — te pliki NIE są w repo (.gitignore):
   - `SkyTowerAI\python\.env` (klucz OpenRouter)
   - `SkyTowerAI\mt5\SkyTowerAI_EA.ex5` i `SkyTower_Zones.ex5` (skompilowane
     binarki; `*.ex5` jest ignorowane) — ALBO pomiń i skompiluj na nowym
     komputerze z zainstalowanego tam MetaEditora (komenda w pkt. B6)
   - opcjonalnie `SkyTowerAI\python\logs\*.jsonl` (historia decyzji/reakcji —
     jeśli chcesz zachować ciągłość datasetu)
3. **Zanim nowy komputer zacznie handlować: wyłącz stary** —
   `docker compose down` + zdejmij EA z wykresów. Dwie instancje na tym samym
   koncie demo = podwójne pozycje.

### B. Na nowym komputerze — instalacja
1. Zainstaluj: Git, Docker Desktop (z WSL2), Purple Trading MT5.
2. `git clone https://github.com/Pietrus-914/Sky-LLM.git`
3. Wklej `.env` do `<repo>\SkyTowerAI\python\.env`.
4. `cd <repo>\SkyTowerAI` → `docker compose up -d --build` → `curl http://127.0.0.1:5555/health`.
5. MT5: zaloguj na demo → WebRequest allowlist `http://127.0.0.1:5555` →
   Algo Trading ON.
6. EA: użyj przeniesionych `.ex5` (pkt. A2) albo skompiluj ze źródeł z repo
   (MetaEditor instaluje się razem z MT5; dostosuj ścieżkę instalacji):
   ```powershell
   # patrz sekcja "Po zmianach w EA" — Start-Process -Wait + kasowanie logu,
   # inaczej odczytasz wynik POPRZEDNIEJ kompilacji
   $me = "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe"
   foreach ($f in "SkyTowerAI_EA","SkyTower_Zones") {
     $src = "<repo>\SkyTowerAI\mt5\$f.mq5"; $log = "<repo>\SkyTowerAI\mt5\$f.log"
     if (Test-Path $log) { Remove-Item $log -Force }
     Start-Process -FilePath $me -ArgumentList "/compile:`"$src`"","/log:`"$log`"" -Wait
     Get-Content $log -Encoding Unicode | Select-String "Result"
   }
   ```
   Potem w MT5: File → **Open Data Folder** (ścieżka datafolderu będzie INNA
   niż na starym komputerze!) → skopiuj `SkyTowerAI_EA.ex5` do `MQL5\Experts\`,
   `SkyTower_Zones.ex5` do `MQL5\Indicators\` → restart terminala.
7. Wykresy NZDUSD/USDCAD/AUDUSD/GBPUSD + EA (inputy domyślne, jak w KROKU 3).

### C. Ustawienia maszyny 24/7 (kluczowe!)
1. **Zasilanie — wyłącz usypianie** (MT5 i Docker nie działają we śnie):
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change monitor-timeout-ac 10
   powercfg /hibernate off
   ```
2. **Auto-logon Windows** — MT5 to aplikacja okienkowa, wymaga zalogowanej
   sesji po każdym restarcie (np. po Windows Update): `netplwiz` → odznacz
   "Users must enter a user name and password" (albo Sysinternals Autologon).
3. **Autostart po zalogowaniu**: Docker Desktop → "Start when you sign in";
   skrót do `terminal64.exe` w folderze `shell:startup` (Win+R → shell:startup).
   Kontener wstaje sam (`restart: unless-stopped`), MT5 zapamiętuje wykresy
   i podpięte EA.
4. **Windows Update**: ustaw godziny aktywności tak, by restarty wypadały poza
   sesjami (np. weekend); dzięki pkt. 2-3 system i tak sam wróci do pracy.
5. **Zegar** (timing eventów co do sekundy): Task Scheduler → codziennie
   `w32tm /resync`. Przy długim uptime WSL2 potrafi dryfować — jeśli w logach
   pojawią się przesunięcia, pomaga `wsl --shutdown` (Docker Desktop go wznowi).
6. Po instalacji: **próba generalna** z fake eventem (KROK 5) zanim zostawisz
   maszynę bez nadzoru.

## Przejście na LIVE (dopiero po udanej fazie demo!)

UWAGA: serwer działa NATYWNIE — FORCE_DECISION steruje wyłącznie plik
`python\.env` (docker-compose od commitu 1731a93 niczego tu nie ustawia,
a kroki dockerowe NIE dotykają natywnego serwera).

1. W `python\.env` **usuń linię** `SKYTOWER_FORCE_DECISION=true` (albo ustaw
   `false`) — LLM odzyska prawo do SKIP.
2. Zamknij okno serwera i kliknij `START.bat` (restart z nowym .env).
3. Weryfikacja (OBIE muszą przejść):
   - `Get-Content python\logs\server.log -Tail 30` — banner
     "FORCE_DECISION TEST MODE" **nie może** wystąpić po restarcie;
   - `curl http://127.0.0.1:5555/api/decision` — w odpowiedzi
     `"forced": false` (albo brak aktywnej decyzji).
4. Przełącz MT5 na konto live z mikro lotami; guardraile: max strata $100/trade,
   $300/dzień, max 5 trade'ów/dzień, max 30 min w pozycji (karta Risk & Daily
   Limits w panelu — wartości jadą do EA z każdym sygnałem).
5. Usuń też `SKYTOWER_FAKE_EVENT_IN_SECONDS`, jeśli został po testach.
