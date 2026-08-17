# Dziennik wiki

Append-only. Nowe wpisy NA KOŃCU pliku, format nagłówka:
`## YYYY-MM-DD INGEST|QUERY|LINT | tytuł` (patrz [schema.md](schema.md)).

## 2026-07-27 INGEST | Bootstrap wiki (wzorzec LLM-wiki Karpathy'ego)

- Przeczytano: gist https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f,
  root `CLAUDE.md`, root `AGENTS.md`, `instruction.md`, `SkyTowerAI/CLAUDE.md`,
  `DOCUMENTATION.md` (nagłówki), `RUNBOOK.md`, nagłówki: `README.md`, `INSTALL.md`,
  `DEPLOYMENT_PLAN.md`, `SESSION_KNOWLEDGE.md`, `SESSION_STATE.md`, `CONTEXT_V5.md`,
  `GPT_REVIEW_PLAN.md`, `CALIBRATION_ANALYSIS.md`, `research/*.md`.
- Utworzono: `schema.md`, `index.md`, strony `system-overview.md`,
  `documentation-map.md`, `learning-loop.md`.
- Dopisano sekcję o wiki do root `CLAUDE.md` i root `AGENTS.md`.

## 2026-07-27 LINT | Pierwszy przegląd spójności istniejącej dokumentacji

Znaleziska (poprawki źródeł = zadania dla operatora, szczegóły w
[documentation-map.md](pages/documentation-map.md)):

- `SkyTowerAI/CLAUDE.md`: Docker opisany jako „primary run mode" — sprzeczne z
  `RUNBOOK.md` (natywnie od 10.07.2026, Docker legacy); liczby linii i drzewo
  modułów mocno nieaktualne; „Anthropic Claude / OpenAI" zamiast OpenRouter.
- `SkyTowerAI/DOCUMENTATION.md`: przykład `.env` z `ANTHROPIC_API_KEY`/
  `OPENAI_API_KEY` — system używa `OPENROUTER_API_KEY`; lista modułów niekompletna.
- `SkyTowerAI/RUNBOOK.md`: nagłówek „379 testów, stan 18.07" — od 24.07 jest 478.
- root `AGENTS.md` (niezacommitowany): artefakty podmiany Claude→Codex —
  nieistniejący katalog `.Codex/`, „Anthropic Codex", odwołanie do nieistniejącego
  `SkyTowerAI/AGENTS.md` (skille dla Codexa są w `.agents/skills/`).
- Martwe zrzuty styczniowe: `SESSION_KNOWLEDGE.md`, `SESSION_STATE.md`,
  `CONTEXT_V5.md`, `INSTALL.md`, `DEPLOYMENT_PLAN.md` — opisują usunięte elementy
  (smart exit TP1/TP2, `/api/zones`, `claude-opus-4`); kandydaci do
  `docs/archive/` lub kasacji (historia gita je zachowa) — decyzja operatora.

## 2026-07-27 INGEST | Porządki dokumentacji — jedna obowiązująca wersja

Realizacja znalezisk LINT z tego samego dnia (polecenie operatora: „zrób
porządek, żeby nie było staroci i trzymamy się najnowszej wersji"):

- Zweryfikowano stan faktyczny: pełny test suite **588 passed** (~12 s, venv),
  realna lista modułów `python/` (26 plików — pamięć projektu była niepełna:
  `mt5_data_exporter.py` i `signal_validator.py` ISTNIEJĄ; są też
  `calibration.py`, `episode_retrieval.py`, `reflections.py`,
  `playbook_distiller.py`, `position_store.py`, `trading_units.py`, `llm_util.py`).
- `SkyTowerAI/CLAUDE.md` przepisany w całości: natywny START.bat (Docker legacy),
  OpenRouter + panel modeli, ryzyko w panelu + `max_loss_usd`, realne drzewo
  plików, pełna tabela API (position/report, calibration, config/risk…),
  learning loop F0–F5, sekcja workflow z pułapką metaeditora.
- `DOCUMENTATION.md`: nagłówek 4.1 + banner stanu, `.env` → `OPENROUTER_API_KEY`,
  moduły uzupełnione skrótem, notka „LLM primary / rule-based fallback",
  limity dzienne → panel, wskaźnik na pełne API w CLAUDE.md, changelog 4.1,
  wersja w przykładzie `/health` 4.1.0.
- `README.md`: 4.1, START.bat, OpenRouter, ForexFactory jako główne źródło
  kalendarza, pytest zamiast test_system.bat, changelog 4.1.
- `RUNBOOK.md`: nagłówek → 588 testów / F0–F5 / Stage 1–2 (27.07).
- root `CLAUDE.md`: RUNBOOK w drzewie, OpenRouter zamiast „Claude/GPT".
- root `AGENTS.md`: naprawione artefakty podmiany (`.Codex/` → `.agents/skills/`,
  `SkyTowerAI/AGENTS.md` → `SkyTowerAI/CLAUDE.md`, sekcja Common Operations
  zamiast slash-commands, OpenRouter).
- Styczniowe zrzuty (`SESSION_KNOWLEDGE`, `SESSION_STATE`, `CONTEXT_V5`,
  `INSTALL`, `DEPLOYMENT_PLAN`) → `SkyTowerAI/docs/archive/` + README archiwum.
  Kasacja (`Remove-Item`/`git rm`) zablokowana przez klasyfikator uprawnień —
  operator może dokończyć usunięcie sam.
- Strony wiki zaktualizowane: `documentation-map.md` (nowe statusy),
  `system-overview.md` (588 testów).

## 2026-07-29 INGEST | Bug: decyzja FOMC niewidoczna dla selekcji eventów

- Zgłoszenie operatora: panel pokazywał „Advance GDP q/q" (30.07) jako next
  event, mimo że 29.07 20:00 CEST była decyzja FOMC (Federal Funds Rate, HIGH).
- Przyczyna: feed ForexFactory nazywa decyzje stóp per bank centralny, a
  whitelist TIER1 w `config.py` znała tylko „Interest Rate Decision" /
  „Cash Rate" / „Official Cash Rate" (pokrywały RBA/RBNZ, nie FOMC/BoE/BOC).
  Przy `TRADE_ALL_EVENTS=false` event nie przechodził `_event_is_tradeable`.
- Fix: do TIER1 dodane „Federal Funds Rate" (USD), „Official Bank Rate" (GBP),
  „Overnight Rate" (CAD) + komentarz o nazewnictwie FF; 3 testy regresyjne w
  `tests/unit/test_calendar_cache.py` (klasa `TestRateDecisionNamesTradeable`).
  591 testów zielonych. „FOMC Statement" i „FOMC Press Conference" celowo poza
  whitelistą (brak twardej liczby; decyzja o tej samej minucie jest handlowana).
- Zaktualizowano: `pages/system-overview.md` (notka o nazewnictwie FF).

## 2026-07-30 INGEST | Runda po incydencie FOMC: bloki B i C planu naprawczego

Kontekst: po zagraniu FOMC 29.07 (panel 2/3 głosów → remis → tie-break regułowy
→ USDCAD BUY 2.01 lota → −$360, zamknięte przez exit-LLM po ~5 min) operator
zlecił wieloagentowy review i wdrożenie znalezisk. Review: 6 finderów + 9
adwersaryjnych weryfikacji + weryfikacja ręczna. **661 testów zielonych, EA
0 errors / 0 warnings.**

### Blok B (blokujące przed live)
- `config.py` + `server.py`: nowa `risk_limit_conflicts()` — per-trade
  `max_loss_usd` NIE MOŻE przekraczać `max_daily_loss_usd` (wcześniej zakres
  5..100 000 bez żadnej relacji między polami, więc jeden trade mógł wydać cały
  dzienny budżet; limit dzienny blokuje tylko NASTĘPNE wejście). Panel odrzuca
  taki zapis (400), import clampuje i zapisuje notkę, a start serwera loguje
  **EFEKTYWNE limity ryzyka** (wartości z panelu nadpisują `.env` na stałe i do
  tej pory nie zostawiały śladu w logu).
- `position_manager.py`: wywołanie exit-LLM zeszło z wątku requestu na worker,
  a komenda jest **kolejkowana w `pending_command`** i wydawana przy następnym
  raporcie EA. Wcześniej komenda (także CLOSE) jechała TYLKO w odpowiedzi HTTP,
  której EA porzucał po 10 s przy 30 s timeoucie modelu — cała warstwa
  „wyjściem steruje serwer" mogła nie dostarczyć nic, a `WebRequest` blokował
  wątek EA (tickowe guardraile stały). Silnik regułowy (bez `client`) dalej
  odpowiada synchronicznie. Flagi `partial_closed`/`sl_moved_to_be` ustawiane są
  teraz przy DOSTARCZENIU komendy, nie przy jej wyliczeniu.
- `llm_decision_engine.py`: panel dostał **kworum** (`ENSEMBLE_MIN_QUORUM=2`),
  **jeden retry** nieudanego głosu (gdy >90 s do publikacji), **logowanie każdego
  odrzuconego głosu z nazwą modelu i powodem** (HTTP 200 z pustą treścią nie
  zostawiał wcześniej ŻADNEGO śladu) oraz marker `degraded` + „PANEL DEGRADED
  n/k" w reasoningu i `ensemble.failures`. W trybie force pojedynczy ocalały głos
  nie jest już opisywany jako „majority … agreement 1.00", a jako „NO QUORUM".
- `config.py`: `"votes"` w `NON_DATA_EVENT_MARKERS` — „MPC Official Bank Rate
  Votes" zawiera podciąg TIER1 „Official Bank Rate", więc był handlowalny i mógł
  przesłonić prawdziwą decyzję BoE z tej samej minuty (`regime_tracker` wykluczał
  „votes" od dawna — brakowało symetrii w selekcji do handlu).
- `SkyTowerAI_EA.mq5`: `ClosePosition` eskaluje deviation 1× → 3× → 6×
  `InpSlippage` przy odrzuceniach cenowych (REQUOTE/PRICE_OFF/PRICE_CHANGED) i
  przywraca wartość wejściową; wcześniej awaryjne zamknięcie ponawiało się
  dopiero na następnym ticku z tym samym 5-pipsowym oknem.

### Blok C
- `exit_decision_engine.py`: zasada #10 promptu wyjścia **nie zamyka już po
  samym czasie** — wymaga istotnej straty (~1/4 budżetu ryzyka) ORAZ braku
  odbicia; prompt dostał sekcje **RISK BUDGET** (ile budżetu zjada obecne P/L) i
  **RECENT TRAJECTORY** (próbki z raportów EA + jawna ocena „recovering" /
  „no recovery yet"). Wcześniej instrukcja była ostrzejsza niż własny fallback
  regułowy tego pliku (10 min I strata >$20). Fallback liczy teraz wszystkie
  progi na **całościowym P/L** (floating + realized), zgodnie z guardrailami.
- `position_manager.py`: `pnl_samples` (cap 12) zbierane z raportów i
  odtwarzane ze snapshotu; realizacja częściowego zamknięcia bierze **dokładny
  `realized_usd` z historii dealów EA** zamiast szacunku ze starego floatingu
  (zero traktowane jako „historia jeszcze nie nadążyła" → szacunek + re-sync).
- `forced` przeżywa restart: EA trzyma `g_tradeForced`, zapisuje go w metadanych
  recovery (format v2, kolumna dopisana na końcu — v1 nadal się czyta) i wysyła
  w raporcie; serwer ma `resolve_forced()` z fallbackiem do `decision_history`
  po `decision_id`. Wcześniej rekoncyliacja rejestrowała demowy rzut monetą jako
  PRAWDZIWY trade, obchodząc wszystkie filtry `forced`.
- `is_test_event_name()` w `event_reaction_history.py` jako jedno źródło prawdy;
  trade'y i refleksje z eventów FAKE TEST są filtrowane (`normalize_event_name`
  obcina nawiasy, więc „CPI m/m (FAKE TEST EVENT)" trafiał do promptu jako
  exact-match anegdota przed każdym prawdziwym CPI).
- Panel ma **wall-clock deadline** (`_seconds_until − 20 s`, podłoga 10 s) — jeden
  zawieszony vendor nie przesuwa już decyzji za publikację; klienci OpenAI mają
  jawne `max_retries` (wejście 0, wyjście 1) zamiast domyślnych 2, które
  potrajały timeout.
- Updater **nie zabija eventu na pierwszym wyjątku**: zapis do `decision_history`
  ma własny try/except (błąd audytu nie może wyrzucić opłaconej decyzji), a
  ponowienia idą przez `_should_retry_analysis` (max 3 próby, min 60 s okna).
- Awaryjny spread wymaga **potwierdzenia** (serwer: 2 kolejne raporty, EA: 3
  kolejne ticki; ≥2× progu zamyka natychmiast) — próg wejścia i próg likwidacji
  były praktycznie równe, więc rutynowy skok spreadu na publikacji zamykał
  pozycję po najgorszej cenie sesji.
- Linia CALIBRATION w prompcie jest **per (model, prompt_version)** — mówi „you
  have been OVERCONFIDENT", a liczyła się z historii innych modeli; przy zmianie
  modelu milczy do własnych n≥50 (karta na dashboardzie zostaje globalna).

### Odrzucone przy weryfikacji
- „Nieudany `/api/position/opened` zostawia pozycję niezarządzaną i pozwala na
  drugą równoległą" — `ReportAndGetCommand` NIE jest bramkowany
  `g_aiManagementActive`, a każdy raport okresowy nosi `reconcile:true`, więc
  serwer rejestruje pozycję w ciągu jednego interwału (5-15 s). Zostaje znany,
  świadomie zachowany efekt: trade policzony dwukrotnie do dziennego limitu
  (kierunek konserwatywny).
- „Tryb force łamie governance panelu" — SKIP jest tam wyłączony z definicji
  (kontrakt trybu demo); realną luką było tylko traktowanie padniętego
  wywołania jak głosu SKIP.

## 2026-07-30 INGEST | Niezależny review commita 8795b71 i naprawa 13 usterek

Operator zlecił niezależny review poprzedniej rundy („chcę mieć pewność, że
zadziała"). 6 recenzentów po wymiarach diffu + adwersaryjna weryfikacja
każdego znaleziska (29 agentów): **13 potwierdzonych, 10 odrzuconych, 0
niepewnych**. Wszystkie 13 naprawione. **679 testów zielonych, EA 0/0**,
suite przechodzi też izolowanie per plik i przy odwróconej kolejności.

### Najpoważniejsze (dostarczanie komend i flagi)
- Komenda była zdejmowana z kolejki w momencie WPISANIA do odpowiedzi HTTP,
  bez żadnego potwierdzenia — dostarczanie było „at-most-once", wbrew temu, co
  twierdził komentarz. Teraz **potwierdź-albo-ponów**: serwer trzyma ostatnio
  wysłaną komendę, aż raport brokera pokaże jej skutek (spadek wolumenu / SL na
  żądanej cenie / dalsze raporty = CLOSE się nie wykonał), ponawia raz, a potem
  odpuszcza. **PARTIAL_CLOSE nigdy nie jest ponawiany** (`REDELIVERABLE_ACTIONS`)
  — gdyby pierwszy się wykonał, a my go jeszcze nie widzieli, drugi zamknąłby
  kolejny kawałek (dwie połówki = 75% pozycji). Natychmiastowe ponowne pytanie
  modelu tylko po nieodebranym CLOSE — inaczej odrzucany przez brokera SL
  generowałby płatne wywołanie na każdym raporcie.
- `partial_closed` i `sl_moved_to_be` ustawiane były na skutek WYSŁANIA komendy.
  Teraz wynikają z obserwacji (`_sync_flags_from_broker`, detekcja spadku
  wolumenu). Wcześniej zgubiona lub odrzucona komenda zostawiała flagę, która
  kłamała, i blokowała regułę BE/partial do końca trade'u.
- Reguła 1 fallbacku (SL na break-even) po przejściu na całościowe P/L mogła
  wystawić stop **po złej stronie rynku**: zysk zrealizowany z partiala spełniał
  próg, gdy otwarta noga była pod wodą. Dodane `pos.profit_usd > 0` oraz
  `_stop_is_below_market` (także dla trailingu).

### Wiring `forced` (3 znaleziska, jeden rdzeń)
`forced` dodano tylko do raportu okresowego, a EA po restarcie re-adoptuje
pozycję przez **`/api/position/opened`** — ten payload pola nie miał, a handler
liczył flagę wyłącznie z `_last_served_signal`/`next_decision` (po restarcie
serwera puste). Po rejestracji `needs_reconcile` jest już False, więc uczciwe
`"forced":true` z kolejnych raportów nigdy nie było czytane. Teraz: pole jedzie
też w `NotifyPositionOpened`, endpoint woła `resolve_forced`, a samo
`resolve_forced` jest **sticky OR** — EA z metadanymi v1 raportuje uczciwe
`false`, ale `decision_history` zna prawdę i wygrywa.

### Pozostałe
- `g_spreadBreachTicks` zerowany tylko w ścieżce sukcesu `ExecuteEventTrade` →
  pozycja adoptowana po niejednoznacznym fillu dziedziczyła licznik i mogła
  zostać zlikwidowana na pierwszym szerokim ticku. Reset dodany w ścieżce
  adopcji i we wszystkich zamknięciach; po stronie serwera analogicznie w
  gałęzi reconcile.
- `_llm_inflight` był globalny dla managera i nie zwalniał się przy zmianie
  pozycji → worker wiszący dla ZAMKNIĘTEJ pozycji blokował pierwszą konsultację
  następnej (pierwsza minuta = faza szczytowa). Wprowadzona **generacja
  dispatchu**: `_abandon_exit_worker` zwalnia slot, a spóźniony worker nie
  wyczyści flagi należącej do nowszego wywołania. Nieudany `Thread.start()`
  też zwalnia slot (wcześniej wyłączałby wyjścia LLM na cały proces).
- Okno retry updatera liczone było z `secs_until` sprzed próby, więc „min 60 s"
  nigdy nie obowiązywało — mierzone teraz od bieżącego czasu.
- `_analysis_failures` wyciekało dla eventów, którym okno się zamknęło bez
  wpisu do `analyzed_events` — wpisy mają timestamp i starzeją się po 24 h.
- Sprzeczność w prompcie wyjścia: reguła #10 zabraniała cięcia po samym czasie,
  a DECISION FRAMEWORK dwa akapity niżej nadal kazał zamykać w fazie fade/late
  „jeśli nie ma znaczącego zysku". Framework przepisany na opis OKAZJI.
- Dwa testy były vacuous (`test_llm_hold_returns_hold`,
  `test_llm_error_falls_through_to_hold` — sprawdzały tylko odpowiedź
  dispatchu, która zawsze jest HOLD) i dwa kolejne w
  `TestCalibrationLineIsPerModel` (wiersze fixture'u nie mogły wyprodukować
  linii ani przed, ani po poprawce — brak kontroli pozytywnej). Przepisane,
  z jawną kontrolą pozytywną.

### Higiena testów (znalezione przy weryfikacji, nie w diffie)
`ensure_services()` woła się WEWNĄTRZ handlera i odbudowuje
`server.position_manager`, jeśli serwisy nie były zainicjalizowane — mój nowy
test przez to **zapisał zmyśloną pozycję (ticket 555) do prawdziwego
`logs/active_position.json`**, a kolejny przebieg ją odtworzył. Na maszynie
operatora taki snapshot blokuje nowe wejścia do rekoncyliacji. Plik usunięty
(kopia w scratchpadzie), a w `conftest.py` doszła autouse-bariera
`_no_production_position_state` kierująca `ACTIVE_POSITION_FILE` i
`TRADE_HISTORY_FILE` na tmp — ta sama klasa wypadku co blokada refleksji z
18.07 (87 fałszywych wpisów w produkcyjnym logu).

### Odrzucone (10) — najważniejsze
Deadlock w workerze (lock nie jest brany ponownie na ścieżce persist),
niepoprawny kształt krotki po deadlinie, `max_retries=0` psujące inne wywołania
(klient wejściowy nie jest dzielony), oraz teza o wyciekaniu szerokiego
deviation do wejścia (jest przywracany bezwarunkowo przed każdym returnem).

## 2026-07-30 INGEST | Budżet myślenia dla modeli reasoningowych + rozdzielenie kanałów OpenRoutera

Kontekst: pierwszy event po wdrożeniu (Advance GDP q/q, 12:30 UTC) zakończył
się zyskiem **+$57.28** i potwierdził działanie zmian z 8795b71/11beb1b —
dashboard pokazał „PANEL DEGRADED 2/3 (no vote from
google/gemini-3.1-pro-preview)", a model wyjściowy trzymał pozycję z
uzasadnieniem „drawdown is small (7% of risk budget) and not material enough to
cut" (sekcja RISK BUDGET) i zamknął ją dopiero na realnym spadku od szczytu,
cytując trend z ostatnich 2 minut (sekcja RECENT TRAJECTORY). **Stary kod uciąłby
ten trade po 5 minutach.**

### Diagnoza brakującego głosu (korekta wcześniejszej hipotezy)
Pierwsza teza — „model wycofany z OpenRoutera" — **była BŁĘDNA**; wynikła ze
streszczenia ogromnego JSON-a katalogu, które pominęło wpis. Zapytanie o endpoint
modelu wprost potwierdza dostępność (6 endpointów Vertex/AI Studio,
`max_completion_tokens` 65 536). Log Generations operatora pokazuje wywołania
Gemini **rozliczone**, w tym DWA wejściowe pod rząd (02:27 i 02:28) — czyli retry
z poprzedniej rundy zadziałał i obie próby wróciły z odpowiedzią, której parser
nie umiał użyć.

Przyczyna: prompt wejściowy ~7,4k tokenów przy `max_tokens=1500`, a
gemini-3.1-pro ma **wymuszony reasoning**, którego tokeny Google wlicza do
`maxOutputTokens`. Myślenie zjadało budżet zanim padł pierwszy znak JSON-a →
HTTP 200 z pustą/urwaną treścią → głos odrzucony. Wyjścia działały bez zarzutu,
bo ich prompt ma 1,4–2,2k, limit 40 słów i mieści się w 900. Ten model
prawdopodobnie **nigdy nie oddał udanego głosu wejściowego**.

### Zmiany
- `llm_util.reasoning_body()` — wspólny fragment `{"reasoning": {"effort": …}}`
  (zagnieżdżone pole OpenRoutera; płaskie `reasoning_effort` to pisownia OpenAI,
  ignorowana). Modele bez reasoningu degradują to po cichu, więc jest bezpieczne
  dla całego panelu. Nieznana wartość cofa się do domyślnej dostawcy — literówka
  w `.env` nie wysadzi calla na T-150s.
- Domyślnie `low` na wejściu i wyjściu (`SKYTOWER_ENTRY_REASONING_EFFORT`,
  `SKYTOWER_EXIT_REASONING_EFFORT`).
- Budżety wyjścia: wejście 1500 → **4000**, wyjście 900 → **2000**
  (`SKYTOWER_ENTRY_MAX_TOKENS`, `SKYTOWER_EXIT_MAX_TOKENS`). Niewykorzystany
  zapas nic nie kosztuje.
- `llm_util.openrouter_headers()` — **osobny `HTTP-Referer` per kanał**
  (`/entry`, `/exit`, `/aux`). OpenRouter grupuje wiersze „App" po refererze, nie
  po `X-Title`, więc jeden wspólny referer sklejał wszystkie kanały pod tytułem
  zobaczonym jako pierwszy: głosy panelu wejściowego figurowały jako „Exit
  Manager", co uniemożliwiało czytanie kosztu i latencji per kanał.
- 12 nowych testów (`test_reasoning_effort.py`). **691 testów zielonych.**

### Otwarte obserwacje
- `GPT-5.6 Sol Pro` raportuje ~26 000 tokenów wejścia przy tym samym promptcie,
  w którym pozostała dwójka ma ~7 400 — 3,5× i dominuje koszt panelu. Nie do
  rozstrzygnięcia z tej strony; do sprawdzenia w szczegółach generacji.
- Rozstrzygające dane są teraz w logach maszyny 24/7:
  `grep "Ensemble vote DROPPED" logs/server.log` podaje powód i pierwsze 200
  znaków nieparsowalnej odpowiedzi; pełne surowe odpowiedzi per model są w
  `logs/decision_context/<decision_id>.json`.

## 2026-07-31 INGEST | Ślad zarządzania pozycją gubił własne zakończenie

Zgłoszenie operatora: „brakuje mi informacji o zamknięciu po 15 wierszach" —
podejrzenie limitu na liście AI POSITION MANAGEMENT.

**Limitu nie ma.** Nagłówek „(15)" to długość listy, a dashboard iteruje
wszystkie wpisy bez cięcia (`dashboard.html`, pętla po `trade.ai_decisions`);
close record niesie `list(position.ai_decisions)` w całości. Jedyne obcięcie w
kodzie to `pos.ai_decisions[-5:]` w `exit_decision_engine._build_prompt` — ile
decyzji widzi MODEL, nie ile się zapisuje.

**Prawdziwa przyczyna:** komendy guardraili były serwowane **bez zapisu do
śladu**. `_check_guardrails()` zwracał komendę, `update_position` ją wysyłał i
nic nie trafiało do `ai_decisions` — więc ślad kończył się ostatnim HOLD-em
modelu, mimo że sam trade miał `reason: "Safety: profit dropped 74% from peak"`.
Dotyczyło to wszystkich bezpieczników: max loss, max hold, spread awaryjny i
ochrona zysku — czyli **akcji, które najczęściej KOŃCZĄ zagranie**. Ten sam
ucięty ślad szedł do `trade_history.jsonl` i dalej do refleksji po trade'zie,
które „widziały" zagrania bez zakończenia.

Fix: wspólne `PositionManager._record_management_action(cmd, source=...)` dla
obu ścieżek (model i guardrail), pole `source` w każdym wpisie, zwijanie
IDENTYCZNYCH kolejnych wpisów guardraila (bezpiecznik re-ewaluuje się na każdym
raporcie i strzelałby co 5-15 s), decyzje modelu nigdy nie zwijane — dwa
identyczne HOLD-y co 30 s to dwie realne konsultacje. Dashboard oznacza wiersze
bezpieczników `[SAFETY]`; stare wpisy bez `source` renderują się jak dotąd.
6 nowych testów, **696 zielonych**.

## INGEST 2026-08-05: postmortem NZD Employment — przebudowa profit-protection, statystyczny TP, TP brokera w zleceniu

Trade 04.08 22:45 UTC (SELL NZDUSD 1.57 lot, Employment Change q/q): kierunek
trafny, ale guardrail profit-protection zamknął pozycję w sekundy po publikacji
z wynikiem −12.57 $ ("Safety: profit dropped 168% from peak ($34.54 → −$23.55)").
Postmortem wykazał trzy niezależne wady i wszystkie trzy zostały naprawione,
każda ze swoim niezależnym code review + testami (711 zielonych):

1. **Profit-protection** (position_manager): stary płaski próg uzbrojenia 20 $
   to przy 1.57 lota ~1.3 pipsa — uzbrajał się na szumie spreadu; brak debounce
   i karencji. Teraz: próg = 30% budżetu `max_loss_usd` (min 10 $), karencja
   120 s po otwarciu, potwierdzenie w 2 kolejnych raportach (wzór spreadu
   awaryjnego; oddanie ≥90% szczytu ≥2× progu zamyka od razu), nigdy nie
   zamyka na minusie netto (poduszka prowizji ~7 $/lot; czas „pod wodą"
   ZERUJE licznik — odbicie musi potwierdzić się dwoma zielonymi raportami).
   Trzy nowe parametry w panelu Risk & Daily Limits + env z clampem zakresu
   (`_env_ranged_*`) + w logu startowym EFFECTIVE RISK LIMITS. Conftest pinuje
   klucze ryzyka (panel operatora nie może psuć testów).
2. **Statystyczny TP** (build_learned_stats + llm_decision_engine): nowe staty
   `favorable_run_5min/30min` (ekskursja Z kierunkiem; mediana/p75/p80/p90) z
   ~44k ścieżek; prompt każe mieścić `take_profit_pips` między medianą a p80
   dla okna wyjścia (30-min = sufit, minus spread — statystyka to travel
   bidu); clamp TP obniżony 30→8 (dla NZD jobs p75 ruchu 30-min = 20.3 pipsa,
   TP 48 był nieosiągalny z konstrukcji). `ENTRY_PROMPT_VERSION = 2026-08-05.1`.
3. **TP brokera w zleceniu** (EA): `trade.Buy/Sell` dostawały TP=0 na sztywno —
   `take_profit_pips` było parsowane i WYRZUCANE (dlatego deal miał pustą
   kolumnę T/P). Teraz TP jedzie w zleceniu (zaokrąglenie KU wejściu, walidacja
   stops-level po konserwatywnej stronie, degradacja do 0 zamiast blokady
   wejścia, retry bez TP tylko na retcode 10016, korekcyjny ModifyPositionTP
   po otwarciu). Exit-LLM widzi TP w prompcie i ma udokumentowaną akcję
   MODIFY_TP (confirm-then-retire porównuje raportowane `tp`). Kompilacja
   0/0. Bonus: `InpUseZoneTargets` to od dawna martwy input (nie pobiera
   /api/targets) — CLAUDE.md poprawione.

Strony: system-overview (guardraile 2026-08-05, przepływ z TP), learning-loop
(favorable run, clamp 8–120, wersja promptu). Pełny postmortem w pamięci
projektu Claude (nzd-postmortem-2026-08-04).

## INGEST 2026-08-05 (2): finalny audyt adwersaryjny pakietu postmortem — 7 potwierdzonych defektów naprawionych

Po wdrożeniu pakietu postmortem przeprowadzono finalny audyt całego commita
(3 soczewki × adwersaryjna weryfikacja każdego znaleziska; 11 zgłoszonych,
4 obalone, 7 potwierdzonych). Naprawione (719 testów zielonych):

1. **Pętla reconcile zerowała liczniki debounce** (position_manager): przy
   trwałej awarii zapisu position-store (dysk pełny/zablokowany plik) każdy
   raport wchodził w gałąź reconcile i zerował _spread_breaches oraz
   _profit_drop_breaches PRZED guardrailami — potwierdzone zamknięcia
   (spread, profit-protection) nigdy nie osiągały drugiego raportu. Reset
   tylko przy prawdziwym recovery (recovery_state == "pending").
2. **Poduszka prowizji po partialu liczona od pełnego lota**: broker
   realized_usd zawiera już prowizję wejściową CAŁEJ pozycji, więc poduszka
   liczy się teraz od remaining_lots — stara wersja tworzyła martwą strefę
   (np. total 9.5$ przy poduszce 10.5$), w której guardrail nie zamykał i
   zerował licznik.
3. **MODIFY_TP bez walidacji jednostek/strony**: tp_price=15 (pipsy zamiast
   ceny) na NZDUSD lądował po WAŻNEJ stronie rynku — broker przyjmował,
   realny TP znikał. Teraz walidacja strony zysku + pasmo 500 pipsów od
   rynku; nieprawidłowy MODIFY_TP degraduje do HOLD z logiem.
4. **Odrzucony przez brokera MODIFY_TP ginął bez re-konsultacji**: po
   MAX_COMMAND_DELIVERIES resetuje teraz last_llm_check (jak CLOSE) — decyzja
   o bankowaniu jest czasowo krytyczna; MODIFY_SL celowo zostaje na cyklu.
5. **float(None) w parserze exit**: JSON null w polu liczbowym (naturalne
   dla nowego tp_price) rzucał TypeError niełapany przez except — ważny HOLD
   modelu degradował do rule-based fallbacku. Teraz `or 0` + TypeError w
   except.
6. **Panel: pominięte pola NaN raportowały sukces**: teraz głośny toast
   "NOT saved (…) — previous values still armed" + przeładowanie karty
   wartościami realnie uzbrojonymi (zweryfikowane w przeglądarce E2E).

ODROCZONE (minor, udokumentowana luka): fill widoczny z opóźnieniem i
zamknięty przez ciasny TP brokera ZANIM jakikolwiek poll recovery zobaczy
pozycję → trade znika z księgowości (bez opened/closed, RECOVERY_NONE po
30 s). Rzadki wyścig pod obciążeniem publikacji; pieniądze bezpieczne,
gubi się tylko wpis w historii/limicie dziennym. Fix wymagałby skanu
historii dealów przy wygaśnięciu okna pendingOpen — do osobnej rundy.

Obalone przez weryfikatorów (nie są bugami): "katastrofa niespełnialna przy
produkcyjnych lotach", "gorszy drugi raport kasuje potwierdzenie", "testy
nie pinują semantyk", "renderer 30-min bez sufitu".

## 2026-08-17 INGEST | Multi-instrument: profile instrumentów + routing eventów USD na XAUUSD

Źródło: branch `feature/multi-instrument` (e312e37 profile/szwy, 91c1413 EA,
e787417 routing + prompt + panel), `SkyTowerAI/research/DAX_OPEN_PLAN.md`
(§0-9 DAX open, §10 alternatywy).

Co się zmieniło: nowy `instrument_profiles.py` (XAUUSD 1 pip=$0.10, GER40/US500
1 pkt; forex → None), hook w `forex_pip_size` (jedyny punkt), klampy silnika per
instrument, guardraile/exit engine/recorder/kalibracja przez `profile_value`,
routing `INSTRUMENT_ROUTING` (env + panel + `/api/config/routing`) w
`_build_market_context_for_event` (świeże dane EA decydują), sekcja INSTRUMENT
w prompcie tylko dla nie-FX, EA: `InpPipSizeOverride`, print SPEC, root-guard,
cap marginu (default 0). Wiedza: +4 721 ścieżek XAUUSD (HistData 2023-26) →
`learned_stats.json` ma bloki XAUUSD dla eventów USD.

Dlaczego: research pokazał, że otwarcie DAX 09:00 nie ma katalizatora
informacyjnego (kierunek 50/50), a największą słabością tezy newsowej na FX jest
koszt (spread 40-100% ruchu); złoto/US500 na tych samych eventach USD kosztują
2-8% ruchu. Routing zachowuje pipeline i wzajemne wykluczanie (1 slot, 1
pozycja) — więcej instrumentów = więcej eventów opłacalnych, nie więcej
równoległych pozycji.

Strony: nowa `pages/multi-instrument.md`; `index.md`; `documentation-map.md`
(wiersz DAX_OPEN_PLAN); RUNBOOK (sekcja „Instrumenty nie-FX"), CLAUDE.md (env,
endpoint, drzewo). Testy: 842 zielone (+101), 2 znane wstępne w test_config.
Nie wdrożone: binarka EA do datafolderów terminala (krok operatora), włączenie
routingu w panelu (decyzja operatora po odczycie SPEC).
