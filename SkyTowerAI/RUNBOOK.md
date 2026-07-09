# SkyTower-AI — Plan uruchomienia krok po kroku

Stan na 08.07.2026: 180 testów zielonych, EA skompilowany (0 err/0 warn),
dry-run E2E przeszedł. Tryb testowy FORCE_DECISION aktywny — **tylko konto DEMO**.

## Pliki, które biorą udział w uruchomieniu

| Rola | Ścieżka |
|------|---------|
| Serwer (Docker) | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\docker-compose.yml` |
| Klucz API | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\python\.env` (OPENROUTER_API_KEY) |
| EA — źródło | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.mq5` |
| EA — skompilowany | `...\mt5\SkyTowerAI_EA.ex5` → **już wgrany** do `%APPDATA%\MetaQuotes\Terminal\F225742ADC2EE896672C03839B31B81B\MQL5\Experts\` |
| Wskaźnik stref | `...\mt5\SkyTower_Zones.ex5` → **już wgrany** do `...\MQL5\Indicators\` |
| Logi/decyzje (host) | `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\python\logs\` (decision_history.jsonl, event_reactions.jsonl, server.log) |

## KROK 1 — Docker Desktop (jednorazowo)

1. Uruchom Docker Desktop.
2. Settings → General → zaznacz **"Start Docker Desktop when you sign in"**
   (największe ryzyko operacyjne to Docker nieżyjący w momencie eventu).

## KROK 2 — Start serwera

```powershell
cd "C:\Users\pietr\Documents\Sky tower\SkyTowerAI"
docker compose up -d --build
```

Weryfikacja:
```powershell
curl http://127.0.0.1:5555/health          # -> {"status":"ok",...}
docker compose logs --tail 20              # -> banner "FORCE_DECISION TEST MODE IS ACTIVE"
```

Serwer sam wstaje po restarcie komputera (`restart: unless-stopped`),
o ile Docker Desktop działa. Dashboard: http://127.0.0.1:5555/

## KROK 3 — MT5 (Purple Trading, konto DEMO)

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

## KROK 4 — Weryfikacja obiegu danych (po ~2 minutach)

```powershell
docker compose logs --since 3m | Select-String "Market data received"
```
Powinny pojawiać się wpisy dla każdej pary co ~60 s. To znaczy: EA → serwer działa.

## KROK 5 (opcjonalnie) — Próba generalna z fałszywym eventem

Pełny cykl (analiza → sygnał → wejście 15 s przed → zarządzanie → wyjście)
bez czekania na realny kalendarz. Na koncie demo z podpiętym EA (USDCAD):

```powershell
cd "C:\Users\pietr\Documents\Sky tower\SkyTowerAI"
docker compose down
docker run --rm -d --name skytower-dryrun -p 127.0.0.1:5555:5555 `
  --env-file python/.env -e SKYTOWER_HOST=0.0.0.0 -e TZ=UTC `
  -e SKYTOWER_FORCE_DECISION=true -e SKYTOWER_FAKE_EVENT_IN_SECONDS=240 `
  skytowerai-skytower
```
Obserwuj: log kontenera (`docker logs -f skytower-dryrun`) + zakładkę Experts w MT5.
EA powinien otworzyć pozycję ~15 s przed "eventem" i sam ją zamknąć.
Reakcje z fake eventów są oznaczane `test:true` i nie zaśmiecają historii.

Po próbie wróć do normalnego trybu:
```powershell
docker stop skytower-dryrun
docker compose up -d
```

## KROK 6 — Eksploatacja (codziennie 2 minuty)

- Dashboard: http://127.0.0.1:5555/ (eventy, decyzje, logi)
- Zdrowie źródeł danych: `curl http://127.0.0.1:5555/api/datasources/status`
- Decyzje: `python\logs\decision_history.jsonl` (pole `forced` odróżnia wymuszone)
- Reakcje na eventy: `curl http://127.0.0.1:5555/api/event-reactions`
- Błędy: `docker compose logs --since 24h | Select-String -Pattern "ERROR"`

## Zatrzymanie / restart

```powershell
docker compose stop      # zatrzymaj (EA przestanie dostawać sygnały = nie handluje)
docker compose start     # wznów
docker compose up -d --build   # po każdej zmianie w kodzie python/
```

## Po zmianach w EA (rekompilacja + wgranie)

```powershell
& "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe" /compile:"C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.mq5" /log
Copy-Item "C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.ex5" "C:\Users\pietr\AppData\Roaming\MetaQuotes\Terminal\F225742ADC2EE896672C03839B31B81B\MQL5\Experts\" -Force
```
Potem w MT5: prawym na wykres → Expert Advisors → usuń i podepnij ponownie
(albo restart terminala).

## Migracja na komputer 24/7

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
   & "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe" /compile:"<repo>\SkyTowerAI\mt5\SkyTowerAI_EA.mq5" /log
   & "C:\Program Files\Purple Trading MT5 Terminal\metaeditor64.exe" /compile:"<repo>\SkyTowerAI\mt5\SkyTower_Zones.mq5" /log
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

1. W `docker-compose.yml` usuń linię `SKYTOWER_FORCE_DECISION=true` (albo ustaw false)
   — LLM odzyska prawo do SKIP.
2. `docker compose up -d` (przeładowanie configu).
3. Sprawdź w logu startowym, że banner FORCE_DECISION **nie** występuje.
4. Przełącz MT5 na konto live z mikro lotami; guardraile: max strata $100/trade,
   $300/dzień, max 5 trade'ów/dzień, max 30 min w pozycji.
