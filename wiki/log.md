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
