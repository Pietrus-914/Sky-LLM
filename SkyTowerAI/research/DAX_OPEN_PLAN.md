# DAX / GER40 na otwarciu 09:00 — analiza i plan działania

*Stan na 16.08.2026. Źródła: 6 czytelników kodu (server, silnik decyzji, pozycje/wyjścia, EA, dane/config/narzędzia, testy/dashboard), 3 researchy rynkowe (mikrostruktura otwarcia, źródła danych, specyfika instrumentu i matematyka ryzyka), 3 niezależne projekty architektury + 2 sędziów, ocena szans (bull / bear / referee). Statystyki DAX policzone samodzielnie na danych Yahoo `^GDAXI` (indeks kasowy Xetra: 10 lat D1, 2 lata H1, 60 sesji M5); wszystkie kluczowe cytaty z kodu zweryfikowane grepem.*

---

## 0. Werdykt w skrócie

1. **Technicznie: tak, da się to dodać bez ruszania flow newsowego.** Właściwa architektura to *syntetyczny event „DAX Cash Open"* wstrzykiwany dokładnie tam, gdzie dziś wchodzi fake event (`server.py:2698-2735`), plus **rejestr profili instrumentów** (punkty zamiast pipsów) i **subklasa silnika decyzji** z własnym promptem. Zero drugiego schedulera, zero drugiego źródła decyzji dla `/api/signal`, zero zmian protokołu EA. Flaga wyłączona = system bajt w bajt taki jak dziś. Szacunek: 5 PR-ów, ~15-20 dni roboczych + faza zerowa (2-3 tyg. kalendarzowo, równolegle).
2. **Merytorycznie: samo otwarcie 09:00 to zdarzenie płynnościowe, nie informacyjne.** FDAX i CFD handlują od 00:10 UTC, więc o 08:55 wszystko, co publiczne (US close, Azja, wyniki spółek z 07:00, dane Destatis z 08:00), jest już w cenie. Na 10 latach danych: kierunek open→close zgodny z gapem w **50%** przypadków w każdym koszyku wielkości gapu, korelacja gap ↔ ruch pierwszej godziny **−0.02**, dryf po otwarciu ≈ 0 (10-letni zysk DAX: +89% „przez noc" vs +18% „w sesji"). Mechaniczne reguły otwarcia (ORB, gap-fade) mają PF ≈ 1.0 przed kosztami.
3. **Kalibrowane szanse na dodatnią oczekiwaną wartość po kosztach w horyzoncie 12+ mies.:** S1 „codziennie o 9:00" ~**15%**; S2 „tylko dni z katalizatorem, reszta SKIP" ~**22%**; **S3 „GER40 jako dodatkowy instrument w ISTNIEJĄCYM flow eventowym"** (dane DE/EU/US) ~**30%** — bo zachowuje tezę informacyjną i naprawia największą słabość FX: koszt (spread 2-2.5 pkt = 3-8% typowego ruchu vs 10-15 pipsów = 40-100% ruchu na FX).
4. **Rekomendacja:** budować moduł (tani, odwracalny, daje próbki do learning loop), ale: (a) najpierw **faza 0** — fakty brokera, spis spreadu na otwarciu, backtest offline; (b) 09:00 wyłącznie w **trybie shadow** przez 60-120 sesji, S3 równolegle w shadow; (c) **kryteria zabicia zapisane z góry**; (d) nie finansować wariantu S1.
5. **Największe ryzyko to nie strata na DAX-ie, tylko ciche zepsucie FX**: jednostki (pips vs punkt) w 9 miejscach serwera i w EA, jeden slot decyzji, jeden PositionManager, wspólne limity dzienne, brak sprawdzenia marginu w EA przy dźwigni **1:100** na GER40 (Purple SC daje 1:500 tylko na FX).

---

## 1. Czy to ma sens — dowody

### 1.1 Jak zachowuje się DAX po 09:00 (indeks kasowy, obliczenia własne)

| Miara | Wartość | Uwagi |
|---|---|---|
| Mediana zakresu pierwszych 5 / 15 / 30 / 60 min | 32 / 58 / 80 / 107 pkt (0.13 / 0.23 / 0.32 / 0.42%) | 60 sesji V-VIII 2026, DAX ~25-26k; cały dzień 247 pkt |
| Zakres 09:00-10:00, 2 lata (726 sesji) | mediana 87 pkt; 2024: 70, 2025: 96, 2026: 109 | pierwsza godzina ≈ 45% zakresu dnia |
| Mediana ruchu netto od otwarcia po 30 / 60 / 120 min | 37 / 51-58 / 70-81 pkt | rośnie ~√t — ruch **nie „rozstrzyga się" w 30 min** |
| Kierunek open→close zgodny z gapem | **50%** (n=2 534, 10 lat, każdy koszyk gapu) | brak informacji kierunkowej w gapie |
| Pierwsza godzina kontynuuje gap | 49%; Spearman(gap, ruch 1h) = −0.02 | j.w. |
| Domknięcie gapu tego samego dnia | 58% ogółem; <0.2%: 84%, 0.2-0.5%: 61%, 0.5-1%: 33%, >1%: 20% | artefakt zakresu dnia (~1%) vs gap (~0.3%), nie edge |
| Fade skrajnego gapu (\|gap\|≥1%) w 1. godzinie | +0.21% (≈50 pkt), 61% trafień, **n=56, t=2.1**; gap-down ≥1%: 69% odwrócenia (n=29) | mała próbka, zdominowana przez dni szoku 2024-25; ~15-30 dni/rok |
| P(SL zbity przy losowym kierunku) SL 40 / 60 / 100 pkt | 30 min: 45 / 27 / 8%; 60 min: 58 / 36 / 16%; 120 min: 68 / 47 / 27% | SL 40 = głównie szum; naturalna siatka SL 60-100 |
| Reżim zmienności | średni zakres dzienny 2024: 173, 2025: 268, 2026: 304 pkt; ATR14 = 267 (14.08) | najwyższy od 2022 — parametry stałe w punktach się zestarzeją |
| Backtesty mechaniczne (zewnętrzne, transparentne) | FDAX overnight-range break 2022-26: 5 069 transakcji, PF 1.03, +0.46 pkt/trade przed kosztami, 2025-26 na minusie | ORB/gap-folklor ≈ zero edge po jednym ticku poślizgu |
| „Overnight drift" S&P przy otwarciu Europy | 3.7%/rok 1998-2020 → ≈0 od 2021 (NY Fed 2026) | jedyna udokumentowana anomalia w tym oknie umarła |

### 1.2 Koszty — tu DAX wygrywa z FX

| | GER40 na otwarciu | Trade eventowy FX (dziś) |
|---|---|---|
| Koszt round-trip | spread 2.1-2.5 pkt + poślizg 1-3 pkt + prowizja RAW $0.40/lot ≈ **3-5 pkt** | 10-15 pipsów spreadu (+ $10/lot RAW) |
| Typowy ruch 30 min | mediana 37-45 pkt | mediana 9 pipsów (62 wiersze tier-1 w `learned_stats.json`), 25-29 dla najlepszych eventów |
| Koszt jako % ruchu | **5-12%** (0.05-0.08R przy SL 60) | **40-100%+** (0.25-0.6R przy SL 25-40) |
| Break-even trafności przy 1:1 | ~52-53% | ~65-75% |

Wniosek: koszt nie jest wąskim gardłem — **kierunek jest**. I właśnie kierunku o 08:55 nic publicznego nie przewiduje.

### 1.3 Matematyka próby (raz dziennie)

* Xetra 2026: 254 dni handlowe; realnie **120-180 transakcji/rok** (S1), **40-70** (S2), **100-200 decyzji** (S3).
* Wykrycie trafności 55% vs 50% (α 5%, moc 80%): **616-783 transakcji** (~4 lata przy S1); 58%: 239-304; 60%: 152-194. W ujęciu R: +0.1R/trade ≈ 610 transakcji, +0.05R ≈ 2 470 (praktycznie nigdy).
* Odrzucenie złej strategii jest szybkie: −0.2R/trade → ~150 transakcji (7-12 mies.), −0.3R → ~70. SPRT na kierunku (H0 50% vs H1 60%) zabija strategię 40-45% po ~50-70 transakcjach.
* **Demo może sfalsyfikować, ale nie potwierdzić małego edge'a.** Każda zmiana promptu/panelu resetuje próbkę (kalibracja kluczowana model+prompt_version).

### 1.4 Ocena szans długoterminowych (referee)

| Scenariusz | P(dodatnia EV po kosztach, 12+ mies.) | Dlaczego |
|---|---|---|
| **S1** codziennie 09:00, LLM wybiera kierunek | **12-18%** | zdarzenie płynnościowe, 50/50, brak dryfu; próg 52-53% trafności niski, ale nic nie wskazuje, że panel go przekracza |
| **S2** tylko dni z katalizatorem (wyniki spółek przed sesją, niespodzianka Destatis 08:00, \|gap\|≥~1%, duży ruch US/Azji, dzień EBC/US CPI), reszta SKIP | **18-28%** | jedyny mechanizm z dowodem (fade gap-down ≥1%, NY Fed „asymetryczne odwrócenie") — ale t=2.1/n=56; SKIP-by-default nie płaci kosztu w dni losowe |
| **S3** GER40 na eventach makro (08:00/09:30/10:00/11:00/14:15/14:30 Berlin) w istniejącym pipeline | **22-38%** | zachowuje zdarzenie informacyjne + „fade ostatnich świec" (niezależny od instrumentu), a koszt spada z 40-100% do 3-8% ruchu; DAX reaguje 100-200 pkt na US CPI/FOMC/EBC |
| S4 punkt odniesienia: obecna strategia FX (mała próbka demo) | 15-35% | z lokalnej maszyny nie widzę wyników z maszyny US — próbka decyduje |

Uczciwie: **to program badawczy z cienkim, reżimowo zależnym edge'em, nie drugie centrum zysku.** Wartość LLM-a jest najbardziej wiarygodna w *nietradowaniu* (rozpoznanie dnia bez katalizatora, wygasania, święta) i w doborze wielkości/horyzontu, a nie w zgadywaniu kierunku.

---

## 2. Co się zepsuje, jeśli podpiąć GER40 „jak parę" — dlatego moduł + profil instrumentu

Każdy punkt to konkretne miejsce w kodzie (zweryfikowane):

| Warstwa | Założenie FX | Co się dzieje dla `GER40` |
|---|---|---|
| `trading_units.forex_pip_size` (17-20) | symbol = 6 liter, pip 0.0001 (JPY 0.01) | `normalize_symbol('GER40')` → `'GER'` → pip **0.0001 pkt**; wywoływane z `market_context`, `position_manager` (BE/SL/TP tolerancje 1575/1619/1627), `exit_decision_engine` (358/399/430), `event_reaction_history`, `event_path_recorder` → ATR ×10⁴, MODIFY_TP martwe (pasmo 500 „pips" = 0.05 pkt), trailing 0.001 pkt |
| EA `SkyPipSize` (`Units.mqh:7-14`) | pip = point×10 dla digits 3/5, inaczej point | GER40 z 2 miejscami → pip = **0.01 pkt**: spread 2 pkt = 200 „pips" > `InpMaxSpreadPips`(10) i twardo zakodowane EXTREME ≥15 (`EA:3061-3079`) → **każde wejście zablokowane**; `InpEmergencySpreadPips`(15) zamknąłby pozycję po 3 tickach |
| Klampy LLM (`llm_decision_engine.py:1267-1272`) + EA SL 20-100 „pips" (1744-1766) | SL 25-80, TP 8-120 pipsów, exit 5-15 min | SL 0.25-0.8 pkt → odrzucenie przez stops level lub natychmiastowy stop; sizing z `OrderCalcProfit` daje ogromne loty na mikro-stopie |
| `POSITION_MANAGEMENT_CONFIG.max_hold_minutes` (`config.py:339`) | 30 min, globalnie, nie z panelu | serwer zamknie DAX po 30 min niezależnie od `InpMaxHoldMinutes` na wykresie |
| Jeden slot `next_decision` (`server.py:2847-2909`) | najbliższy event wygrywa | decyzja DAX przypięta od T-150 s do T+120 s zacienia event newsowy w tym samym oknie i odwrotnie (lato: 07:00 UTC vs dane GBP 06:00/07:00 UTC) |
| Jeden `PositionManager`, wspólne liczniki dzienne | 1 pozycja, 5 tradów/dzień, $300/dzień | strata na DAX o 07:00 UTC zjada budżet całej sesji US; druga równoległa pozycja = `recovery_state='conflict'` blokuje wszystko |
| `DEFAULT_PAIRS.get(currency, f'{currency}/USD')` (`server.py:1131`, `llm:367`) | waluta 3-literowa → para | tag `DAX` → para `'DAX/USD'` → `/api/signal?pair=GER40` odpowiada „Not selected" |
| `_currency_bias_to_direction` (`llm:1668-1678`) | `pair.startswith(currency)` → BUY | `GER40` nigdy nie zaczyna się od tagu → w fallbacku/FORCE_DECISION **bias BULLISH mapuje się na SELL** |
| `_entry_prompt` (`llm:1163-1176`) | COT/sentiment/forecast zawsze w prompcie | wydrukuje `{error: ...}` / `NO_DATA` JSON — łamie niezmiennik prompt-invisibility |
| `ConvertPairToSymbol` (`EA:2895-2918`) | 6-literowy root + suffixy FX | `GER40.cash` może nie zostać rozwiązane albo trafić na inny symbol niż wykres (recovery filtruje `POSITION_SYMBOL==_Symbol`) |
| Wejście EA (`EA:852`) | 15 s PRZED czasem eventu; serwer nie serwuje po T0 | trade na otwarciu chce wejść PO 09:00 → czas syntetycznego eventu = pożądane wejście + 15 s |
| Margin (0 wystąpień `OrderCalcMargin` w EA) | 1:500 FX, margin pomijalny | Purple SC: **GER40 1:100**. Margin = budżet × cena / (SL × dźwignia): budżet $600, SL 40 pkt @26 440 → **$3 966**; SL 80 → $1 983. Przy saldzie ~$1 000 realny budżet ryzyka na DAX to **~$150** (SL 80, 50% marginu) — albo retcode 10019 i brak wejścia |
| Strefy czasowe | wszystko UTC, brak DST | 09:00 Berlin = **07:00 UTC 29.03-25.10.2026, 08:00 UTC poza tym**; MT5 D1 zaczyna się o 00:00 czasu brokera (nie jest to open Xetra); brak kalendarza świąt Xetra |

---

## 3. Architektura docelowa (hybryda wybrana przez obu sędziów)

**Zasada:** jeden *provider* syntetycznych eventów + jedna *subklasa silnika* + jeden *rejestr profili instrumentów*. Bez drugiego schedulera, bez drugiego rejestratora ścieżek, bez `Strategy Protocol`/wrappera na flow newsowy. Wzajemne wykluczanie (1 pozycja, wspólne limity dzienne) **zostaje z konstrukcji** — to najważniejsza własność regresyjna.

### 3.1 Nowe pliki (`SkyTowerAI/python/` o ile nie zaznaczono)

| Plik | Rola |
|---|---|
| `instrument_profiles.py` | `InstrumentProfile` (frozen dataclass): `symbols`, `pip_size` (1.0 pkt), `units_label`, `sl_range`, `tp_range`, `exit_range`, `lot_max`, `max_hold_minutes`, `emergency_spread_pips`, `typical_spread_pips`, `exit_llm_interval_seconds`, `exit_system_prompt`, `tp_sanity_band_pips`, `rule_be_buffer_pips`, `rule_trail_pips`, `commission_cushion_usd_per_lot`, `learning_tag`. `profile_for(symbol)` po `normalize_pair` (`GER40.cash`→`GER40`) → `None` dla każdego 6-literowego rootu FX (assert: klucze rejestru muszą zawierać cyfrę). `profile_value(symbol, key, default)`. |
| `session_calendar.py` + `knowledge/xetra_holidays.json` | `SessionSpec(tz, open, close, holidays)`, `next_session_open(now_utc)`, `entry_event_time(open, entry_offset_s, ea_entry_seconds_before)`; **pytz** `Europe/Berlin` (już w `requirements.txt`; tzdata na venv US niezweryfikowane); `utcnow` importowane po nazwie z `timeutil` (patchowalne w testach). |
| `strategies/__init__.py` | tylko `SYNTHETIC_PROVIDERS: list` i `ENGINE_BY_SOURCE: dict` + `engine_for(event)` = `ENGINE_BY_SOURCE.get(event.source, decision_engine)` (leniwie, przez global modułu — istniejące fixture'y patchujące `server.decision_engine` dalej działają). |
| `strategies/index_open.py` | `IndexOpenProvider`: `upcoming_events(now, calendar_events)` → `[EconomicEvent(datetime_utc=open+entry_offset+15 s (naive UTC), currency='DAX', event_name='DAX Cash Open', impact='HIGH', forecast=None, previous=None, source='index-open')]` lub `[]` (wyłączony / weekend / święto Xetra / **yield-to-news: event kalendarzowy w ±10 min → nie emitujemy**, powód na status); `recordable_events(now)` → kotwica T0 09:00:00 (`source='index-open-anchor'`, nigdy tradowalna) + event wejścia, **niezależnie od `enabled`** (ścieżki i kalibracja zbierają się od 1. dnia); `fake_open(in_seconds)` z sufiksem „(FAKE TEST)" (→ `test:true`); `status()`; czyste, bez I/O, nigdy nie rzuca (działa pod `decision_lock`). |
| `strategies/index_open_engine.py` | `IndexOpenDecisionEngine(LLMDecisionEngine)`: własny `SYSTEM_PROMPT` (zachowuje `%DIRECTION_VALUES%/%SKIP_POLICY%`), `PROMPT_VERSION='index-open-2026-08.1'`, `CHANNEL='entry-index'`, klampy z profilu, `learned_stats_file=knowledge/index_learned_stats.json`; nadpisuje `_gather_data` (zachowuje klucze bazowe `event/suggested_pair/forecast_info/cot_analysis/sentiment_analysis/_source_status` + `index_pack`), `_entry_prompt` (**pomija** COT/SENTIMENT/FORECAST/CROSS-PAIR; każda sekcja indeksowa renderowana *tylko gdy klucz obecny*), `_rule_based_decision`→SKIP, `_forced_direction`→jawna reguła (nigdy `startswith(currency)`), `_trades_for_currency` po `profile.symbols`. Dziedziczy `_ensemble_decision`, `_chat`, deadline, kworum, parsowanie. |
| `strategies/index_context_pack.py` | kolektory z kontraktem `SentimentAggregator` (`fetch()->dict\|None`, `last_status`, cache 5 min, timeout ≤5 s per źródło) — **na własnym wątku daemon; updater tylko czyta cache**; ring-buffer M1 GER40 (600 barów, `logs/index_open_bars.jsonl`) zasilany z `report_market_data`; `build_index_pack()` z samymi obecnymi kluczami. |
| `strategies/index_open_api.py` | blueprint: `GET/POST /api/config/index-open` (tabela `(key, lo, hi, cast)` all-or-nothing jak `config_risk` 223-280, persist przez `cfg.save_runtime_overrides`), `GET /api/index-open/status`, `POST /api/index-open/dry-run {"in_seconds":240}` (demo, bez edycji `.env`). |
| `tools/build_index_open_paths.py` | kotwice = każdy dzień handlowy Xetra 09:00 Berlin (+ wariant z offsetem wejścia); bary z `fetch_histdata.py --pairs grxeur` (HistData: **EST bez DST** — walidacja sygnaturą zmienności 09:00 Berlin) lub M1 brokera przez `mt5_data_exporter` (ten sam feed, kolumna spread — preferowane); `measure_path(bar_map, t0, pip=1.0, allow_partial=True)` bez zmian; rekordy `currency='DAX', pair='GER40', non_data=True` + pola gap/overnight → `knowledge/index_open_paths.jsonl.gz` → `build_learned_stats.py --out knowledge/index_learned_stats.json` (schema_version 1, klucz `DAX\|dax cash open`). |
| testy | `test_instrument_profiles`, `test_session_calendar` (oba przełączenia DST, Wielkanoc, latch), `test_index_open_provider` (wyłączony→`[]`, yield, klucz `DAX_YYYYMMDD_HHMM` bez `_` w tagu), `test_llm_engine_attrs` (piny klampów bazowych + `PROMPT_VERSION=='2026-08-05.1'` + niezmieniony prompt bazowy), `test_index_open_engine` (prompt nigdy nie zawiera COT/RETAIL SENTIMENT/FORECAST COMPARISON/CROSS-PAIR/error/NO_DATA; klampy w punktach; forced nieodwrócony; ensemble odziedziczony), `test_position_manager_profiles`, `test_exit_engine_profiles` (hash `EXIT_SYSTEM_PROMPT`), `test_event_path_recorder_index`; integracyjne: kontrakt sygnału (`?pair=GER40.CASH` serwowany, `?pair=NZDUSD` „Not selected", `event_currency=='DAX'`, `max_loss_usd` obecny), config API; e2e `test_index_open_full_loop` (świat z tmp, pushe GER40 z 2 miejscami, fake open, panel skryptowany, `HttpFakeEA` w punktach, wiersz ścieżki, wiersz kalibracji, refleksja pod DAX, `event_paths` FX nietknięte, **testy kolizji**: FX przypięty pierwszy → DAX nieanalizowany; DAX 60 s bliżej → provider ustępuje; wyłączony → lista eventów identyczna). `conftest`: autouse `INDEX_OPEN_CONFIG['enabled']=False`, kolektory przypięte do `None`, `_OVERRIDES_FILE`→tmp. |
| docs | `wiki/pages/index-open-strategy.md` + `wiki/index.md` + `wiki/log.md` INGEST + wiersz w `documentation-map.md`; RUNBOOK KROK 2 (5. wykres, inputy GER40) + KROK 4b (dry-run indeksu); CLAUDE.md (env/endpointy); Przewodnik (5. wykres, matematyka lota w punktach, FAQ o wspólnym slocie/limitach). |

### 3.2 Szwy w istniejących plikach (każdy `if profile else <dzisiejsze wyrażenie>`)

* `trading_units.forex_pip_size` — pierwsza linia: `p = profile_for(symbol); if p: return p.pip_size` (3 linie; naprawia wszystkie 9 wywołań naraz; **nie** dodawać rodzeństwa i nie podmieniać wywołań).
* `llm_decision_engine.py` — literały → atrybuty klasy `PROMPT_VERSION, SL_RANGE, TP_RANGE, EXIT_RANGE, LOT_MAX, UNITS_LABEL, CHANNEL` czytane przez `self.` (1014-1016, 1266-1272, 1443-1469, nagłówek `_chat`, „pips" w 863/901/964); wartości identyczne; `learned_stats_file` jako kwarg konstruktora.
* `position_manager._check_guardrails` (1392, 1410, 1460) — `profile_value(pos.symbol, 'max_hold_minutes', ...)`, `emergency_spread_pips`, cushion; kadencja LLM wyjść (1166-1205) z profilu; rekord zamknięcia + `instrument_profile` (addytywnie).
* `exit_decision_engine._llm_decision` (252/276/286) — system prompt z profilu; `_validate_modify_tp` pasmo, BE/trail reguły z profilu (domyślne 500/1/10 → FX identyczne).
* `event_path_recorder` — `__init__(instrument_resolver=None)`; `_fresh_pairs` (288-299) używa resolvera, gdy zwróci listę, inaczej dzisiejsze cięcie [:3]/[3:6]; `_base_record`: `non_data = ... or event.source in {'index-open','index-open-anchor'}` (**nigdy** backfill z FF — inaczej do 12 zapytań/event i 429).
* `calendar_fetcher.CalendarAggregator.peek_raw_feed_events()` — addytywny odczyt `_last_good` (makro dnia dla packa bez nowego klucza cache i bez dodatkowego fetchu FF).
* `calibration.news_spread_pips` (42-52) — najpierw `profile.typical_spread_pips`; `reflections._currency_of` — `learning_tag or symbol[:3]` (dopiero w PR5, za pinami).
* `config.py` — osobny `INDEX_OPEN_CONFIG` (enabled False, symbol `GER40`, tz/open, `entry_offset_seconds` 90-180, `ea_entry_seconds_before` 15, `max_hold_minutes` 90-120, `exit_llm_interval_seconds` 60, `emergency_spread_points` 10, zakresy SL/TP/exit, `lot_cap_percent`, `yield_to_news_minutes` 10) + env `SKYTOWER_INDEX_OPEN_*` + późne `_apply_index_open_runtime_overrides()` wzorem `_apply_model_runtime_overrides` (604-636) — **nie** dotykać `POSITION_MANAGEMENT_CONFIG` ani przebiegu kluczy ryzyka (piny w conftest); `DEFAULT_PAIRS['DAX']='GER40'` (jedna linia; `CURRENCY_PAIRS` bez zmian → kalendarz nigdy nie monitoruje DAX/EUR); `TYPICAL_NEWS_SPREADS['GER40']=2.5`.
* `server.py` (≤25 linii, 5 szwów) — `ensure_services` buduje `index_engine`, provider, blueprint, `EventPathRecorder(..., instrument_resolver=...)`; `_get_next_unanalyzed_events` (2710-2718): `extras = [fake] + provider.upcoming_events(utcnow(), events)` sortowane `_naive_time`; Faza 2 (2934): `engine = engine_for(event_to_analyze)`; `_run_event_path_recorder` (2793-2795): `+ provider.recordable_events(utcnow())`; `report_market_data`: `provider.on_market_data(pair, entry)` w try/except; `config_models`: pętla po obu silnikach; `_last_served_signal` + `"pair"`. **`position_opened` i `/api/signal` bez zmian** — decyzja indeksowa JEST `next_decision`, więc czyszczenie slotu przy otwarciu jest poprawne, a filtr `normalize_pair('GER40.cash')=='GER40'` kieruje sygnał na właściwy wykres.
* `dashboard.html` — karta „Index Open (GER40)" (toggle immediate-persist jak `tradeAllEvents` 2074-2089), karta „Next index open" z odliczaniem z `/api/index-open/status`, etykieta „SL/TP Points" gdy tag indeksowy, wiersze w Przewodniku.

### 3.3 EA (jedna rekompilacja, domyślnie bez zmiany zachowania)

* `SkyTowerAI_Units.mqh`: `double g_skyPipSizeOverride = 0.0;` i w `SkyPipSize` pierwsza linia `if(g_skyPipSizeOverride > 0) return g_skyPipSizeOverride;` → wszystkie bramki spreadu (1647-1669, 1825-1839, twarde EXTREME 3061-3079), granice SL 1744-1766, awaryjny spread 2141-2157, BE 2313 i `spread_pips` w raportach liczą się w **punktach** na wykresie GER40.
* `SkyTowerAI_EA.mq5`: `input double InpPipSizeOverride = 0.0;` (przypisane w OnInit); rozszerzyć print STOPS/FREEZE (636-639) o digits/point/tick value/contract size/volume min-step-max/margin per lot — **ten print jest odkryciem specyfikacji** decydującym o `pip_size` i `InpSlippage`; `ConvertPairToSymbol`: `if(RootOf(pair)==RootOf(_Symbol)) return _Symbol;` przed sondowaniem suffixów (root = do pierwszego `.`/`_`/`-`); `CalculateLotSize`: cap wolnym marginem przez `OrderCalcMargin` (+ obsługa retcode 10019); opcjonalnie `pip_size` w payloadach raportów (serwer ignoruje nieznane pola; później asercja).
* Inputy na wykresie GER40 (bez kodu): `InpPipSizeOverride=1.0`, `InpMaxSpreadPips≈4`, `InpEmergencySpreadPips≈10`, `InpMaxHoldMinutes` ≥ wartość serwera, `InpSlippage 300-500`, `InpCheckInterval=15`, `InpEntrySecondsBefore=15` (musi równać się `ea_entry_seconds_before`), strefy OFF.
* Kompilacja wg RUNBOOK 102-125 / `deploy_ea.ps1` (skasować log, `Start-Process -Wait`, świeżość `.ex5`); najpierw 4 wykresy FX + dry-run fake eventu FX, dopiero potem wykres GER40.

**Niezmiennik jednostek:** „1 pip serwera == 1 punkt indeksu == 1 pip EA przez `InpPipSizeOverride=1.0`"; kontrakt na drucie (`stop_loss_pips/take_profit_pips/spread_pips/max_loss_usd`) bez zmian; klampy indeksu w silniku (SL 20-120 / TP 15-250 pkt / exit 10-90 min) dobrane tak, by twarde 20-100 EA (teraz w punktach) nigdy nie korygowało wartości serwera.

### 3.4 Przepływ dnia (czas Berlin, lato = UTC+2)

| Czas | Co się dzieje |
|---|---|
| 04:05 / 06:05 | dpa-AFX TAGESVORSCHAU (terminy dnia) w RSS |
| 08:00-08:10 | Destatis RSS: **actual** danych 08:00 w tytule; FF `_last_good` daje forecast/previous |
| 08:17-08:22 | dpa-AFX „Aktien Frankfurt Ausblick" |
| 08:40-08:47 | wątek kolektorów buduje pack (EA M1 GER40, Yahoo/TradingView, VIX, RSS, IG, kalendarz) — cache |
| 08:50 | Xetra: faza call aukcji otwarcia (do 09:00 + losowe zakończenie ≤~30 s) |
| ~08:59 | updater: event syntetyczny wchodzi w okno T-150 s → `engine_for` → panel z packiem; deadline T-20 s |
| **09:00** | otwarcie kasowe; kotwica T0 do rejestratora ścieżek |
| 09:01-09:02 | EA wchodzi 15 s przed „czasem eventu" (= open + offset 90-120 s), tylko jeśli spread ≤ bramka, cena nie uciekła > 0.3×SL od referencji 08:58, M1 świeże ≤90 s |
| 09:05 → | exit engine (kadencja 60 s), guardraile w USD; brak dyskrecjonalnych wyjść przed 09:05 |
| 09:30 / 10:00 / 11:00 | PMI / ifo / ZEW — flagowane w prompcie („trzymaj przez / wyjdź przed") |
| ≤11:00 | max hold 120 min — zamknięcie przed danymi US 14:30 |

### 3.5 Czego NIE robić (konsensus sędziów)

* Drugi wątek schedulera / maszyna stanów / drugie źródło decyzji w `/api/signal` (projekt C) — dwa uzbrojone sygnały → druga pozycja → `recovery_state='conflict'` blokuje cały handel.
* `StrategyRegistry`/9-metodowy `Strategy Protocol`/wrapper `NewsEventStrategy` (projekt B) — przechwytywanie `decision_engine` w `ensure_services` psuje istniejące fixture'y; dispatch po `event.source` w jednym dict wystarczy (US500 = drugi provider + wpis).
* Rodzeństwo `instrument_pip_size` + podmiana 9 wywołań — hook **w środku** `forex_pip_size`, inaczej strefy/target rozjadą się z resztą.
* Tagowanie eventów `EUR`, dodawanie EUR do `CURRENCY_PAIRS` (uczyni eventy EUR tradowalnymi bez EA), tag z `_` (`cleanup_stale_registrations` dzieli po `_`).
* Nowy kluczowany fetch FF dla makro dnia (dzieli cache, 429) — czytać `_last_good`; syntetyczne eventy bez `non_data=True`.
* Progi DAX w `POSITION_MANAGEMENT_CONFIG` (piny conftest); zapisy do `runtime_overrides.json` poza walidowanym endpointem.
* Ponowne użycie forexowych `_entry_prompt/_gather_data/_rule_based_decision` (JSON błędów w prompcie, inwersja na SELL).
* Scalanie statystyk DAX do `knowledge/learned_stats.json` w v1; zmiana domyślnego schematu `measure_path`.
* `/api/test-signal` do dry-runu indeksu (ustawia `currency=pair[:3]='GER'`) — używać fake open z „(FAKE TEST)".
* Sloty pozycji per strategia / osobne budżety dzienne w v1 — udokumentować wykluczanie w Przewodniku, pokazać „yielded to «event»" na karcie statusu.
* Wysyłka przekompilowanego EA na maszynę US przed przejściem dry-runu FX **i** dry-runu indeksu na demo.

---

## 4. Zarządzanie pozycją na DAX — czym różni się od eventów

| Aspekt | Eventy FX (dziś) | DAX open (propozycja) | Uzasadnienie |
|---|---|---|---|
| Wejście | T-15 s przed publikacją | **09:01-09:02**, nigdy 09:00:00 | aukcja otwarcia z losowym końcem, „nieświeże" składniki w pierwszych tickach indeksu (reguła STOXX), skok spreadu; decyzja panelu gotowa do 08:58 |
| SL | 25-80 pipsów | `clamp(round(0.30 × ATR14), 50, 150)` ≈ **80 pkt** dziś | SL 40 zbity w 45-68% przypadków przy zerowym edge'u; 70-90 pkt trzyma szumowe stopy na 20-30% przy 60 min |
| TP | 8-120 pipsów / p80 favorable run | TP brokera w zleceniu: **TP1 ≈ 0.6×SL** (~50 pkt, mediana ruchu 60 min) zamyka 50% + SL na BE+spread; **TP2 1.25-1.5×SL** lub p80 favorable run z `index_open_paths` — mniejsza z nich | ruch rośnie ~√t; p80 ~88 pkt @60 min, ~127 @120 min (indeks kasowy) |
| Hold | max 30 min (twardo) | **≤120 min** (koniec do 11:00), time-stop przy 60 min jeśli w ±0.2R | otwarcie nie „rozstrzyga się" w 30 min; przed danymi US 14:30 |
| Exit engine | co 30 s, prompt „forex news 0-30 min" | kadencja **60 s**, własny prompt sesyjny z profilu, klampy 30-120 min, brak dyskrecji przed 09:05 | 30 s × 120 min = 240 wywołań (~$0.5-1/trade); wzorzec z postmortem NZD (zamykanie na szumie) |
| Sizing | budżet = min(saldo×%, max_loss) × lot_percent 60-85% | to samo **plus cap marginu** `0.5 × free_margin × leverage × SL / price` | przy $1 000, 1:100, SL 80 → ~$150 ryzyka; budżet DAX jest ograniczony marginem, nie ryzykiem |
| Limity | 5/dzień, $300/dzień, wspólne | **max 1 trade GER40/dzień**, bez re-entry po stopie; w v1 wspólny envelope (widoczne w panelu) | wykluczanie z konstrukcji |
| SKIP twarde (przed wywołaniem LLM) | wypowiedzi, spread | święta Xetra 2026 (1.01, 3.04, 6.04, 1.05, 24/25/31.12 — CFD handluje, ale nie ma otwarcia kasowego), 3. piątki (wygasania; kwartalne = triple witching), poniedziałki rewizji indeksu (23.03, 22.06, ~21.09, ~21.12), poranek dnia decyzji EBC, spread > bramka, M1 EA nieświeże, brak źródła notowań overnight, ruch >0.5% w 1. minucie, limit dzienny zużyty ≥50%, sesja GER40 zamknięta wg `SymbolInfoSessionTrade` | flagi miękkie w prompcie: święta US/UK, tygodnie rozjazdu DST (9-27.03, 26-30.10), cienkie dni DE (14.05, 25.05, 4.06) |
| Wariant zalecany | — | **bramkowanie S2**: SKIP gdy \|gap vs zamknięcie 17:30\| < 0.7% i brak wyników/niespodzianki 08:00/skrajnego sentymentu IG (≤25% lub ≥75%) — moduł SKIP-heavy jak FX; S1 tylko w shadow | |

Dla S3 (GER40 na eventach): logika wejścia FX (decyzja T-15 s, wejście przy publikacji), jednostki indeksowe, bramka spreadu zmierzona o 14:30, SL skalowany do wyuczonego ruchu eventu (ścieżki GRXEUR), hold 30-45 min. **Uwaga:** S3 na żywo wymaga rozwiązania kolizji jednego slotu (ten sam event dla pary FX i dla GER40) — w v1 S3 wyłącznie jako **decyzje shadow** (nie serwują sygnału, więc nie dotykają slotu ani PositionManagera).

---

## 5. Pakiet danych dla LLM (ranking; wszystko zweryfikowane 15.08.2026)

Zasady: każde pole z czasem „as-of" UTC i opóźnieniem; brak źródła = brak sekcji (prompt-invisibility), **nigdy nie symulować**; kolektory tylko na własnym wątku, jeden call/źródło/rano, cache na dysku, timeout ≤5 s.

| # | Dane | Źródło | Dostęp | Odporność | Wartość |
|---|---|---|---|---|---|
| 1 | Cena GER40 08:55, **gap vs zamknięcie 17:30 Xetra i vs 22:00 CFD**, zakres nocy od 00:15 UTC, ATR14, ostatnie 20 M1, spread na żywo | **EA na wykresie GER40** (istniejący push M1) | bez sieci | wysoka | najwyższa — jedyne źródło spreadu/poślizgu brokera i spójnej definicji gapu; zasila też rejestrator ścieżek |
| 2 | Bramka kalendarzowa | `exchange_calendars` XETR + `knowledge/xetra_holidays.json`, 3. piątki, rewizje indeksu, dni EBC, DST-aware 09:00 Berlin | offline | wysoka | wysoka, koszt ~0 |
| 3 | Overnight/global: prev close ^GDAXI, ES=F/NQ=F 1m nocą, ^N225, ^HSI, ^KS11, 000001.SS, ^STOXX50E, EURUSD=X, ^TNX, CL=F, DX-Y.NYB | Yahoo v8 chart (`query1.finance.yahoo.com/v8/finance/chart/...`, bez auth, UA przeglądarki); fallback TradingView scanner (`FDAX1!`, `FESX1!`, `DE10Y`, `FGBL1!` — opóźnienie 10-15 min) | REST | średnia (nieoficjalne, 429 możliwe) | średnia — już w cenie, ale definiuje reżim nocy i tag „duży ruch US/Azji" dla S2 |
| 4 | Makro dnia z **actualami**: FF `_last_good` (forecast/previous, GMT) + **Destatis „Aktuell" RSS** (`.../RSSNewsfeed/Aktuell.xml`, liczba w tytule, publikacja 08:00 CEST) | RSS | wysoka | średnio-wysoka w dni katalizatora; **pierwsze prawdziwe „actual" w systemie**. Długoterminowo: `CalendarValue*` w MQL5 przez EA (naprawiłoby actual też dla FX — do przetestowania na Purple) |
| 5 | Reżim zmienności: VIX | Cboe delayed JSON (`cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json`) + własna zmienność zrealizowana z M1 | REST | wysoka | średnia (skalowanie SL/TP/hold); VDAX-NEW **nie** jest dostępny za darmo (Yahoo stale od 2019, TradingView pusty) |
| 6 | Poranne nagłówki DE: `finanznachrichten.de` RSS (marktberichte, germany-40, konjunktur, aktien-adhoc; dpa-AFX „Aktien Frankfurt Ausblick" ~08:20, TAGESVORSCHAU 06:05) + 1 zapytanie Google News RSS (`q=DAX+when:1d&hl=de`), tylko tytuły+czas z ostatnich 18 h, deduplikacja | RSS | średnio-wysoka | średnia (identyfikacja katalizatora: wyniki, ad-hoc, geopolityka) |
| 7 | Wyniki spółek DAX-40 | statyczne `knowledge/dax40_constituents.json` (odświeżane przy rewizjach III/VI/IX/XII) + nocny sweep `yfinance` (`Ticker('SAP.DE').get_earnings_dates()`) + EQS ad-hoc z #6 | pkg/RSS | średnia | średnia w ~40-60 poranków/rok; brak darmowego API z godziną publikacji dla .DE (Finnhub/FMP/Twelve Data — free tier bezużyteczny) |
| 8 | Sentyment detaliczny „Germany 40" | strona publiczna IG (`ig.com/uk/indices/markets-indices/germany-40`, regex `--long-percent: NN%`, odświeżana co 15 min; 15.08: **19% long / 81% short**) → docelowo IG Labs `/clientsentiment/{marketId}` przez `trading-ig` (wymaga darmowego konta demo IG założonego przez Ciebie) | HTML/REST | niska-średnia / wysoka | niska-średnia (kontrarian, nieudowodniony dla indeksu); ściśle prompt-invisible gdy brak. Myfxbook/FXSSI/DailyFX — brak DAX / martwe |
| 9 | „Poranny briefing" LLM online | OpenRouter `perplexity/sonar` (~$0.01/call) lub natywny web-search modeli panelu (~$0.10-0.25) | REST | średnia | niska — tylko warstwa narracyjna z cytatami, **nigdy źródło liczb** |
| 10 | Offline / backtest | HistData **GRXEUR M1 2010→VIII 2026** (bid, EST bez DST; `tools/fetch_histdata.py --pairs grxeur` przechodzi bez zmian) + Dukascopy `DEU.IDX/EUR` ticks od 2012 (bid/ask) | zip/pkg | wysoka | wysoka dla priorów i decyzji kill |

**Nie budować na:** stooq (od III 2026 klucz API + JS proof-of-work), finanzen.net RSS (403 Akamai), DailyFX (zamknięty 09.2024), TradingEconomics guest API (410), investing.com (403), API boerse-frankfurt/live.deutsche-boerse (podpisywane nagłówki, dwukrotnie zepsute w 2026), płatne API earnings.

---

## 6. Plan działania

### Faza 0 — feasibility (2-3 tyg., **bez kodu strategii, zero ryzyka**)

| Krok | Co | Wynik / próg |
|---|---|---|
| 0a | Skrypt MQL5 / print OnInit na wykresie GER40 (Purple demo): `SYMBOL_TRADE_CONTRACT_SIZE`, `TICK_SIZE/VALUE`, `DIGITS/POINT`, `VOLUME_MIN/STEP/MAX`, `TRADE_CALC_MODE`, `MARGIN_INITIAL`, `CURRENCY_PROFIT`, `SymbolInfoSessionTrade` pn-pt, `OrderCalcProfit/OrderCalcMargin` dla 1 lota z SL 60 pkt, `ACCOUNT_LEVERAGE`, `STOPS/FREEZE_LEVEL` | zapis do `research/`; **kill:** margin dla minimalnego sensownego SL (60-80 pkt) przy planowanym budżecie > 50% salda i brak zgody na zmniejszenie budżetu |
| 0b | Rejestrator spreadu/poślizgu 08:55-09:15 Berlin, 10-15 sesji (bid/ask co tick; p50/p80 spreadu w koszykach 15 s, czas normalizacji; poślizg mikro-zleceń) | **kill:** p80 spreadu o 09:01-09:02 > 6 pkt lub mediana poślizgu > 3 pkt lub spread nie wraca do ≤1.5× poziomu 08:55 do 09:02 → zostaje tylko S3 |
| 0c | Bootstrap offline: GRXEUR M1 2010-2026 (+ Dukascopy) → `build_index_open_paths.py` → zakresy pierwszych N min, MAE/MFE 30/60/120, favorable run p80 dla TP, replikacja fade gap-down ≥1% out-of-sample (2010-2023, bez 2024-26) | **kill S2:** fade nie replikuje się z t>2.5 i dodatnią średnią w ≥3 z 4 podokresów → S2 tylko shadow bezterminowo |
| 0d | Kolektor packu uruchomiony codziennie 08:40-08:47 w shadow: dostępność/latencja per źródło, pack zapisany obok `decision_context` | próg: dostępność rdzenia (EA M1 + ≥1 źródło notowań + bramka kalendarza) ≥ 90% |

**Punkt decyzyjny A:** dalej tylko, gdy margin/spread/poślizg przechodzą i pack ≥ 90%.

### PR-y (branch `index-open-strategy` od `main`; każdy PR: pełny pakiet testów zielony offline, liczba testów w commicie, wpis INGEST w `wiki/log.md`)

| PR | Zawartość | Testy | Szacunek |
|---|---|---|---|
| **PR1 — szwy serwera, zero zmiany zachowania** | `instrument_profiles.py`, hook w `forex_pip_size`, atrybuty klasy w silniku + kwarg `learned_stats_file`, gałęzie profilu w PM/exit/recorder, `peek_raw_feed_events`, `engine_for` (na razie zawsze `decision_engine`) + mirror e2e używa tego samego helpera, `_last_served_signal.pair` | piny FX w `test_trading_units.py` + property test po `CURRENCY_PAIRS` (w tym krzyże JPY), atrybuty `LLMDecisionEngine` == literały, regresja klampów ze skryptowanym `_chat`, hash `EXIT_SYSTEM_PROMPT`, guardraile nietknięte (NZDUSD max_hold 30 / spread 15), cięcie FX w recorderze niezmienione, przypadki GER40 +/− | 2-3 dni |
| **PR2 — EA** (niezależny, może iść równolegle) | `g_skyPipSizeOverride` w `SkyPipSize`, `InpPipSizeOverride`, print specyfikacji w OnInit, root-guard w `ConvertPairToSymbol`, cap marginu w `CalculateLotSize` + retcode 10019 | kompilacja wg procedury; dry-run fake eventu FX na 4 wykresach; dopiero potem wykres GER40 z `InpPipSizeOverride=1.0`, potwierdzenie `spread_pips` ~1-3 w `/api/market-data`; **z printu wybrać `pip_size`** (bramkuje wartości profilu w PR3) | 1-2 dni + kompilacja/attach |
| **PR3 — provider + silnik + config + blueprint + karta, flaga OFF** | `session_calendar.py` + święta, `index_open.py`, `index_open_engine.py` (prompt v1: struktura rynku w punktach + gap z bufora + learned stats/playbook gdy są), `INDEX_OPEN_CONFIG` + późne overrides, merge w `_get_next_unanalyzed_events`, dispatch `engine_for`, pętla `config_models`, karta dashboardu, piny conftest, fake open | `test_session_calendar` (27→30.03 07:00Z, 23→26.10 08:00Z, W. Piątek/Poniedziałek Wlk., weekend, latch), provider (wyłączony→`[]`; włączony→dokładnie 1 tradowalny + 1 kotwica/dzień; yield ±10 min; nazwa bez markerów NON_DATA i „FAKE TEST" poza fake'iem; klucz parsuje się w `cleanup_stale_registrations`), silnik (jw.), config API, kontrakt sygnału, **testy kolizji updatera** | 4-5 dni |
| **PR4 — pakiet danych** | `index_context_pack.py` (ring-buffer, wątek kolektorów, gap vs 17:30/17:35 Berlin, overnight/Azja/pre-market, 1. minuta, wykresy monitorujące US500/USTEC/EURUSD jeśli podłączysz EA, makro dnia z `peek_raw_feed_events`, VIX z cache dziennego, RSS, IG, notatki operatora), prompt v2 | wszystkie kolektory przypięte offline w conftest; jeden przebieg z `SKYTOWER_TEST_BLOCK_NET=1` | 3-4 dni |
| **PR5 — substrat uczenia + docs** | mapa tagów recordera + kotwica T0 na żywo, `build_index_open_paths.py` (z self-checkiem DST), `index_learned_stats.json`, wpis playbooka „DAX Cash Open", roster distillera, przełącznik w `replay_decisions.py` (`prompt_version.startswith('index-open')`), e2e `test_index_open_full_loop`, RUNBOOK (5. wykres + dry-run), CLAUDE.md, Przewodnik, strona wiki + INGEST | e2e pełnej pętli + brak regresji `event_paths` FX | 3-4 dni |

**Rollout:** tylko demo — `POST /api/index-open/dry-run {"in_seconds":240}` (rekordy `test:true`), potem `enabled=true` z `lot_cap_percent≈20` przez ≥10 sesji (w miarę możliwości z tygodniem DST), codzienne porównanie jednostek serwer/EA w logach; **maszyna US na końcu** (ZIP musi zawierać `knowledge/index_*`, nigdy nie nadpisywać `python/logs/` ani `.env`, rekompilacja tam, `InpServerPort=5556`).

### Faza 1 — shadow (60-120 sesji, ~3-6 mies., $0 ryzyka, ~$0.21/dzień LLM)

Każda sesja Xetra: panel dostaje pełny pack (**zamrożony** `prompt_version` i skład panelu), zwraca BUY/SELL/SKIP + confidence + SL/TP/hold w punktach; nic nie idzie do EA. Logujemy `decision_id`, kierunek, confidence, tagi katalizatora (gap-down ≥1%, gap-up ≥1%, poranek wyników, niespodzianka Destatis, dzień EBC/US CPI, wygasanie/rewizja) i hipotetyczne wyniki z M1 GER40 po 30/60/120 min netto po zmierzonych kosztach (wejście 09:01:30, SL/TP jak zdecydowano). Metryki: trafność ogółem i per tag (Wilson 95% CI), średnie/mediana R, PF, Brier + krzywa kalibracji (istniejąca księga), skip-rate, hipotetyczne R vs 3 bazowe reguły (zawsze SKIP; mechaniczny fade \|gap\|≥1%; kontynuacja pierwszych 15 min). **S3 od 1. dnia w tym samym harnessie shadow** (dodatkowe próbki bez slotu/PositionManagera).

**Punkt decyzyjny B** (n ≥ 100 shadow, lub ≥ 60 gdy podgrupa uderzająca): demo-live tylko gdy trafność ≥ 58/100 (jednostronne p<0.05) lub prerejestrowana podgrupa ≥ 60% przy n ≥ 30, **i** hipotetyczny PF netto ≥ 1.3, **i** kalibracja przynajmniej słaba (Brier < 0.25); inaczej przedłużenie o 60 sesji raz, potem kill lub parking.

### Faza 2 — demo-live (6-12 mies.)

Sub-envelope w limitach panelu (max 1 trade GER40/dzień, max loss ograniczony regułą marginu ~10-15% salda przy 1:100, własny cap dzienny, by strata o 09:00 nigdy nie zablokowała eventu FX o 13:30), SL/TP brokera w zleceniu, tabela klampów indeksu w exit engine, SPRT na kierunku, przegląd miesięczny (R netto, PF, CI trafności, poślizg zrealizowany vs shadow, MAE/MFE vs priory offline, wkład exit engine vs stały hold 60/120 min, udział decyzji z niepełnym packiem, zdrowie flow FX — zero zablokowanych eventów, testy zielone).

**Punkt decyzyjny C** (≥ 100 transakcji live): PF ≥ 1.2 netto z dolną granicą CI trafności > 50% → rozważyć mały realny kapitał w tym samym envelope; PF 1.0-1.2 → dalej demo, bez kapitału; inaczej kill.

---

## 7. Kryteria zabicia (zapisane z góry — pre-rejestracja w `research/` przed 1. decyzją shadow)

* **Przed budową:** margin nietradowalny przy 1:100 (0a); spread/poślizg na otwarciu ponad progi (0b); fade gap-down nie replikuje się OOS (0c).
* **Shadow:** po 60 decyzjach trafność ≤ 50% **i** żadna prerejestrowana podgrupa ≥ 60% przy n ≥ 15 → stop; po 120: trafność < 53% lub hipotetyczne R netto nie bije obu baz → stop modułu 09:00 (S1/S2), zostaje S3 w shadow.
* **Demo-live statystyczne:** SPRT H0 p=0.50 vs H1 p=0.60 (α=β=0.05) — moduł ginie w dniu przecięcia dolnej granicy (40% umiera po ~50, 45% po ~70 transakcjach). Twarde backstopy: skumulowane R ≤ −15R po 50 transakcjach; PF < 0.9 po 100; drawdown > 25R.
* **Czasowe:** po 12 mies. lub ≥ 120 transakcji live z PF < 1.1 i CI trafności zawierającym 50% → wyłączyć lub trwały shadow; nie przedłużać „żeby zebrać więcej danych", chyba że podgrupa jest istotna.
* **Operacyjne (chroni FX):** incydent zużycia wspólnego capu/licznika blokujący prawdziwy event FX; odrzucenie z marginu (10019) lub ciche przycięcie lota; jakakolwiek regresja FX (pakiet testów musi być zielony, ścieżka sygnału FX bajt w bajt identyczna przy `enabled=false`); błąd jednostek → moduł wyłączony do naprawy; dwa incydenty w kwartale → usunięcie z serwera live.
* **Dane:** rdzeń źródeł < 90% poranków przez 3 tygodnie lub pack pusty w > 20% decyzji → brak handlu live; nie łatać web-searchem LLM-a.
* **Guardraile:** > 30% pozycji GER40 zamkniętych w pierwszych 10 min na szumie po przeklampowaniu → pauza i re-tuning; > 3 zamknięcia/mies. ścieżką awaryjnego spreadu → bramki źle ustawione.
* **Zmiana promptu (soft kill):** każda zmiana `prompt_version`, składu panelu, klampów lub offsetu wejścia resetuje zegar; ≥ 40 nowych sesji shadow przed wznowieniem live.

Koszt bieżący: +1 wywołanie panelu/dzień (~$0.21) + wyjścia przy 60 s przez 90-120 min (~$0.5-1/trade) ≈ **$100-250/rok** — pomijalny. Realną ceną jest czas inżynierski i ryzyko regresji FX.

---

## 8. Największe niepewności

1. Czy panel LLM dodaje **jakąkolwiek** informację kierunkową o 09:00 ponad to, co CFD wyceniany 24 h przez futures już zawiera — na tym stoi cała sprawa S1/S2.
2. Fakty Purple GER40 nieosiągalne z sieci: contract size, tick value, digits, cash- czy futures-basis, faktyczny spread/poślizg w pierwszych 30-120 s.
3. Reżim: zmienność 2025-26 najwyższa od 2022; parametry kalibrowane dziś mogą być 2× za szerokie/wąskie w reżimie typu 2024.
4. Kadencja vs próba: edge +0.05-0.10R po kosztach jest nieodróżnialny od zera w horyzoncie; każda zmiana promptu resetuje zegar.
5. Kruchość packu: Yahoo v8, TradingView, strona IG — nieoficjalne/za Akamai; sentyment FX umarł tak samo (Myfxbook 403, FXSSI 0 par).
6. Ryzyko inżynierskie: jednostki, wspólny cap, jeden wątek updatera — subtelny błąd może cicho wyłączyć lub przeskalować trady FX.
7. Krawędzie czasu/kalendarza: 07:00 vs 08:00 UTC, Eurex od 00:10 UTC, D1 MT5 od 00:00 brokera, święta Xetra na których CFD handluje, wygasania, rewizje, święta US/UK.
8. Jakość backtestu: statystyki z indeksu kasowego (pierwsze ticki z nieświeżymi składnikami, brak spreadu); nieciągłość CFD 08:59→09:01 niezmierzona; HistData bid-only, EST bez DST.

---

## 9. Decyzje, które są Twoje

1. **Wzajemne wykluczanie w v1** (1 pozycja, wspólne limity dzienne, DAX zużywa 1 z 5 tradów) — akceptujesz? Alternatywa (sloty per strategia) to przebudowa PositionManagera, poza „minimalnie inwazyjnym modułem".
2. **Budżet ryzyka DAX** — przy saldzie demo ~$1 000 i dźwigni 1:100 realnie ~$100-200/trade (SL 60-80 pkt). Zwiększyć saldo demo (np. $5-10k), czy zaakceptować mały budżet jako „koszt badań"?
3. **Priorytet wariantu:** 09:00 (S2 shadow → demo) czy od razu równolegle **S3** (GER40 na eventach) — referee daje S3 najwyższą szansę, ale na żywo wymaga rozwiązania kolizji slotu (v1: tylko shadow).
4. **Faza 0 teraz?** — potrzebuję zgody na: (a) pobranie ~16 zipów GRXEUR M1 z HistData (kilka-kilkanaście MB każdy) narzędziem, które już masz; (b) podpięcie EA/skryptu na wykres GER40 na demo (print specyfikacji + rejestrator spreadu).
5. Nazwa symbolu DAX w Twoim Market Watch (GER40? z suffixem?) — jedno spojrzenie w terminal.
6. (Opcjonalnie) darmowe konto demo IG dla stabilnego API sentymentu.

---

## 10. Uzupełnienie (16.08, wieczór): co INNEGO na zasadach strategii newsowej da się wkomponować — i czy RSI ratuje otwarcie DAX

*Źródła: 5 researchy (energia EIA, metale na danych US, indeksy na eventach, anomalie przepływów FX, filtry warunkowe DAX) + sceptyk-syntetyzator. Agent od metali wykonał własne badanie M1 (HistData, 596 publikacji US 2023-01→2026-07, XAUUSD/XAGUSD/5 par FX) — pliki w tymczasowym scratchpadzie sesji, nie w repo.*

### 10.1 Odpowiedź w jednym zdaniu

Nie szukaj **innego momentu** — szukaj **innego instrumentu na te same momenty**. Wszystko, co ma trzy cechy tezy newsowej (zaplanowana publikacja + znany forecast/previous + pozycjonowanie), to te same eventy USD, które system już handluje; różnica polega na tym, że złoto i US500 reagują na nie tak samo mocno w %, a kosztują **2-8% ruchu zamiast 40-100%** — próg opłacalności trafności spada z ~60-65% do ~52-53%. To jest naprawa kosztu, nie nowy edge — ale to jedyna dźwignia, którą widać w danych.

### 10.2 Ranking kandydatów (sceptyk; P = kalibrowana szansa dodatniej EV po kosztach, 12+ mies., wersja z panelem LLM)

| # | Kandydat → instrument | Eventów/rok | Koszt/ruch | Dowody | P | Werdykt |
|---|---|---|---|---|---|---|
| 1 | **US CPI (PPI/PCE) → US500** | 12 (+24) | 2-8% (0.4-0.7 pkt vs 25-45 pkt) | Kroner (Fed 2025): znak reakcji ES na niespodziankę CPI stabilny w obu reżimach, czułość ×13 większa 2021-23 | **0.32** | ten sam event, koszt spada 10×; brak historii M1 indeksów w projekcie |
| 2 | **US NFP → XAUUSD** | 12 | 6-8% (spread $0.54-0.64 vs \|m30\| $8.5) | badanie własne n=38: znak niespodzianki→kierunek złota 78%/76%/70% (5/15/30 min), duże niespodzianki 94%; mapowanie trzymało każdy rok 2023-26 | **0.30** | najstabilniejsze mapowanie; NZDUSD na tych samych eventach 67%/59% |
| 3 | **US CPI → XAUUSD** | 12 | 6-8% | n=40: 86%/77% (15/30 min), corr −0.61; **odwrócone w 2025** (3/7 — „złoto rośnie na wszystko"), 2026 3/3 | **0.28** | HistData M1 już pobrane → learned_stats od 1. dnia; XAU/USD zachowuje semantykę base/quote promptu |
| 4 | US NFP → US500/US100 | 12 | 2-6% | Kurov: największa reakcja ES (R² 0.50), ale **znak zależy od reżimu** („good news is bad news"), odwrócenia w 1. minucie | 0.22 | dla NFP lepsze złoto |
| 5 | US CPI/NFP 14:30 → **GER40** | 24 | 2-6% | transmisja 2. rzędu przez EURUSD; GRXEUR M1 do backtestu | 0.22 | katalizator, którego brakowało otwarciu — drugi indeks po US500 |
| 6 | ISM (16:00) / PCE → US500 | 24+12 | 3-10% | Kurov: dla ISM **poinformowany dryf przed publikacją** = 49% ruchu → tu regułę „fade ostatnich świec" trzeba ODWRÓCIĆ (follow) | 0.20 | połowa ruchu znika przed printem |
| 7 | XAUUSD tier-2 (PCE, PPI, ISM, JOLTS, claims) | ~130 | 10-15% | ruchy 0.15-0.20% ledwo ponad szum złota; mapowanie 60-75% | 0.18 | tylko PCE/PPI warte slotu; claims 52×/rok zjadłyby limit 5/dzień |
| 8 | FOMC 20:00 + konferencja → US500 | 8 | 1-4% | Narain-Sangani: po 2022 konferencja odwraca ruch komunikatu (corr −0.48), ale specyficzne dla przewodniczącego, R² 0.08 | 0.25 | wymaga holdu 30-90 min i decyzji dwuetapowej |
| 9 | **EIA ropa (śr. 16:30) → USOIL** | 52 | 15-25% (spread $0.046 + $10/lot vs \|m30\| $0.35) | literatura 2005-16: silna odwrotna reakcja; **dane własne 2024-26 (n=102): zgodność znaku 48-51%, corr +0.05** — reżim wygasł/zdominowany przez geopolitykę | 0.15 | wygląda jak teza na tańszym instrumencie, ale kierunek dziś losowy; FF impact LOW → obejście filtra |
| 10 | XAGUSD na CPI/NFP | 24 | 28-53% | ruch 1.9× złota, ale koszt 6× | 0.15 | przegrywa slot ze złotem |
| 11 | Momentum ostatnich 30 min do zamknięcia (US500 15:30-16:00 ET; **GER40 17:00-17:30**) | ~250 | 30-50% edge'u | **najlepsze dowody z całej listy** (Gao et al. JFE 2018; Baltussen et al. JFE 2021: DAX t=5.45, 55-61% trafień, Sharpe ~1.7 brutto) | 0.38 | **inny produkt**: brak eventu informacyjnego, kierunek mechaniczny, edge ~3 bp/trade, twarde wyjście 17:30, bez panelu LLM |
| 12 | EBC 14:15/14:45 → GER40/EU50 | 8 | 3-10% | kierunek akcji przy niespodziance teoretycznie i empirycznie niejednoznaczny | 0.15 | EURUSD lepszym wehikułem; wiersze EUR odfiltrowane z parsera |
| 13 | Fixing WM/R 16:00 Londyn, koniec miesiąca (noga T-30 min) | 12/parę | ok. | Melvin-Prins 2015 recenzowane, ale R² 0.03; trafność praktyków < 45% od 2018 (hedgerzy → VWAP) | 0.28 | tylko logowanie ścieżek; n=12/rok nieweryfikowalne |
| 14 | Dryfy bezwarunkowe (long US500 24 h przed FOMC; long GER40 D-1 przed EBC) | 8+8 | ok. | opublikowane, dekady, ale słabsze od 2015 i głównie przy wysokim VIX | 0.40 | najwyższe P listy, ale hold 24 h, brak informacji — nie produkt SkyTower |
| 15 | EIA gaz (czw.) → NGAS | 52 | **55-70%** (spread 0.55% ceny) | najwyższa wierność tezie (74-87% kierunku po dużych niespodziankach, n=110), ale arytmetyka ujemna | 0.12 / 0.22 na tanim rynku | research/tick-log, nie live na Purple |
| — | ifo/ZEW/PMI → GER40 (0.05-0.12%/SD, impulsy 1-4 min), UK CPI/BoE → UK100, BoJ → JP225 (brak stałej godziny), gotobi 9:55 JST, luka weekendowa FX, option-cut 10:00 NY, aukcje obligacji US (brak forecastu), GDT (brak forecastu, nieznana minuta), Baker Hughes, OPEC | | | | 0.03-0.15 | nie warte kodu; GDT i aukcje co najwyżej jako kontekst/logowanie |

### 10.3 Trzy rzeczy, które ten research mówi o SAMEJ strategii newsowej

1. **Koszt jest wąskim gardłem FX.** Spread 10-15 pipsów przy medianie ruchu 30-min 9-29 pipsów oznacza próg trafności ~65-75% przy 1:1. Na złocie/US500 ten sam ruch w % kosztuje 2-8%.
2. **„Fade ostatnich świec" nie potwierdza się w zbiorczym teście 2023-26** (badanie własne M1, ~550 eventów US): ruch 15 min przed publikacją przeciwny do ruchu 30 min po niej w **50%** dla złota, **51% NZDUSD, 48% AUDUSD**; dla publikacji 10:00 ET (ISM) dryf przedpublikacyjny jest *poinformowany* — trzeba go podążać, nie fade'ować. To nie unieważnia Twojego 78% z ekranów 2017-20 (inna definicja, konfluencja, inne lata), ale **trzeba to zweryfikować na własnych `historical_paths` (44 679 ścieżek) zanim reguła trafi na nowy instrument** — koszt: jeden skrypt offline.
3. **Kierunek trzeba nadal wywołać PRZED publikacją** — złoto/indeks nie zmieniają tego problemu; zmieniają tylko cenę pomyłki. Test kluczowy (bez żadnego instrumentu): czy publiczny pre-state przewiduje znak niespodzianki > 55%? (Cleveland Fed nowcast vs konsensus dla CPI/PCE; ADP / claims / ISM-employment vs konsensus NFP) — na archiwum FF (ma actuale) 2021-26 + replay panelu istniejącym harnessem F5. Jeśli ≤ 52%: żadna zmiana instrumentu nie ratuje tezy; jeśli 55%+: złoto/US500 robią ją dodatnią tam, gdzie FX nie mógł.

### 10.4 Czy RSI (lub inne filtry) ratują otwarcie DAX?

**Nie.** Filtry cenowe warunkują na cenie, a nie tworzą informacji; otwarcie jest zdarzeniem płynnościowym. Transparentne testy: MNQ 2021-25 — 14 rodzin sygnałów OHLCV, żadna nie przeżywa 2 pkt kosztu (ORB t=1.50, gap-fade t −0.4..−0.6); ML na otwarciu 50.0-50.9% (permutacja p 0.13-0.52); brak trwałego efektu poniedziałku/wygasania na DAX. **Nie istnieje żadne badanie RSI na otwarciu indeksu przewidującego kolejne 30-120 min**; RSI o 08:59 na CFD to zaszumione przekodowanie gapu/nocnego zwrotu — jeśli „działa" in-sample, to dlatego, że przybliża duży gap; testuj gap wprost.

Warte przetestowania offline na GRXEUR M1 2010-2026 — **dokładnie cztery pre-rejestrowane hipotezy** (Bonferroni α 0.0125), reszta jako kolumny kontrolne:
* **H1** fade vs continue gap-down ≥1% (też 0.7%/1.5%) vs zamknięcie 17:30, warunkowo na znaku ruchu 17:00-17:35 dnia poprzedniego (proxy nierównowagi zamknięcia, Boyarchenko-Larsen-Whelan); horyzonty 30/60 min; też wejście o 08:00 zamiast 09:00 (dokumentowane odwrócenie siedzi głównie w 08:00-09:00 i wg NY Fed ≈0 od 2021).
* **H2** fade vs continue dryfu 08:30/08:45→09:00 (analog „fade ostatnich świec"), tylko górny kwintyl \|dryfu\|, 15/30 min, z wykluczeniem dni danych 08:00 (DE/UK) i 09:30 (PMI). Literatura mówi „price discovery INTO the open" — oczekuj porażki lub szumu, ale to najtańsza odpowiedź na Twoje pytanie.
* **H3** kontynuacja pierwszych 5 min jako ORB (wejście 09:05, stop = przeciwna strona zakresu 09:00-09:05, TP 2R/3R, time-stop 60 min, bramka ATR20 górna połowa) — oceniać oczekiwaną wartością w punktach, nie trafnością (jeśli działa, to jako produkt 24-45% trafień z asymetrycznym R).
* **H4** momentum do zamknięcia: znak 09:00-17:00 zgodny ze znakiem pierwszych 30 min → hold 17:00-17:30, wyjście 17:30:00 (Baltussen JFE 2021: t=5.45, OOS R² 1.2%) — najbardziej prawdopodobny mały edge brutto; netto zależy od zmierzonego spreadu GER40 17:00-17:30.

Numerologia (kolumny kontrolne, bez slotu hipotezy): RSI(14) na dowolnym TF, „sweep" ekstremum nocnego zakresu, dzień tygodnia/wygasanie/koniec miesiąca/gap×trend jako KIERUNEK, ML poza regularyzowaną regresją logistyczną. Reżim zmienności (VDAX/ATR20) — tylko jako **bramka** wielkości/holdu, nigdy powód wejścia. Protokół: cechy o 08:59:45, cele +15/+30/+60/+120 z MFE/MAE, netto 4 pkt round-trip, discovery 2010-16 / validation 2017-21 / holdout 2022-26 otwarty raz; promocja tylko przy holdout t > 2.5, n ≥ 150, ≥ 70% dodatnich lat, monotoniczna odpowiedź po kwintylach. Uwaga o mocy: przy 100-300 sesjach na koszyk wykrywalne są tylko trafności ≥ 58-60%; Twoje 69% na n=29 ma CI Wilsona 51-83%.

### 10.5 Co to zmienia w architekturze (routing „event → instrument")

* **Filtry kalendarza:** zamiast „waluta → para" jawna tabela `(wzorzec nazwy, waluta) → uporządkowana lista instrumentów` w `_event_is_tradeable` z *per-wpis nadpisaniem impactu* (FF: „Crude Oil Inventories"/„Natural Gas Storage" = LOW dla USD, a `MIN_IMPACT` jest AND-owany z whitelistą — sama nazwa w `SKYTOWER_EXTRA_EVENTS` nie wystarczy); `NON_DATA_EVENT_MARKERS` nietykalne; wiersze EUR wymagają włączenia w parserze.
* **Jeden slot decyzji na event → jeden instrument na event w v1.** Porównanie FX vs złoto/US500 na żywo byłoby sekwencyjne (12 CPI/rok → 24+ mies. zanim cokolwiek widać). Taniej: **decyzja pojedyncza, ale scoring na każdym zarejestrowanym instrumencie** — wywołanie znaku USD jest identyczne dla USDCAD/AUDUSD/NZDUSD/XAUUSD/US500, a `event_path_recorder._fresh_pairs` już mierzy każdy pushowany symbol zawierający walutę eventu (`currency in (norm[:3], norm[3:6])`) i zapisuje spread w T0. **XAUUSD przechodzi ten filtr bez zmian kodu** (USD na pozycji [3:6]); US500/GER40/USOIL (5 znaków) odpadają na `len(norm) >= 6` i wymagają wpisu w tabeli. To daje równoległy shadow A/B `decision_id × ścieżka instrumentu` bez ruszania ścieżki sygnału. Arbitraż instrumentu serwowanego na żywo: najniższy koszt/oczekiwany ruch (indeks lub złoto przed FX dla printów USD); limity dzienne i „jedna pozycja" bez zmian.
* **EA i jednostki:** dokładnie ten sam rejestr profili co w planie DAX (pkt 3.1-3.3): pip override, bramka spreadu w punktach/$/% (`InpMaxSpreadPips=15` blokuje złoto — spread ~54 „pips" przy 2 miejscach — i NGAS z konstrukcji), klampy per instrument (złoto CPI/NFP SL ~$8-10 / TP ~$12-16 skalowane zrealizowanym zakresem; US500 SL 8-30 pkt), lot z `SYMBOL_TRADE_CONTRACT_SIZE`/tick value (Purple US500 $1 czy $10/pkt — niezweryfikowane; powtórka błędu 50 lotów bez tego), cap marginu (1:100 złoto/indeksy, **1:20 energie**), godziny per symbol.
* **Kolektory pozycjonowania:** legacy raport COT (waluty) jest złym plikiem dla wszystkiego nowego — Disaggregated „Metals and Other" (GOLD 088691, SILVER 084691, Managed Money), „Petroleum" (WTI-PHYSICAL 067651 — stara nazwa „CRUDE OIL, LIGHT SWEET" wycofana), TFF dla S&P 500 (Asset Manager/Leveraged Funds); brak COT dla DAX/Euro Stoxx. Walidować każdą nazwę grepem po pliku CFTC — martwe nazwy USD/NZD pokazały, że awaria jest cicha (200 + []). Sentyment detaliczny dla złota/indeksów: brak dowodów, że „fade retail" przewiduje reakcje 30-min — podawać jako kontekst, oceniać księgą kalibracji. Zamiast tego pre-state per instrument: Cleveland Fed nowcast vs konsensus (CPI/PCE), ADP/claims/ISM-employment (NFP), CME FedWatch (FOMC), API + Cushing (EIA); logować actual EIA z ir.eia.gov.

### 10.6 Kolejność testów (najmniej kodu najpierw)

1. **Bez kodu:** specyfikacja XAUUSD/US500/GER40/USOIL/NGAS w terminalu Purple (contract size, tick value, digits, margin, prowizja RAW — raporty rozjeżdżają się między ~$0.2/lot a „$5 per side" dla indeksów) + tick-log bid/ask T-120 s…T+120 s na demo przy 3-4 najbliższych publikacjach US (CPI, NFP, ISM). Cała kalkulacja kosztu zakłada normalny spread; złoto $3 albo indeks ×3 przy T-15 s sprowadza sprawę do poziomu FX (35-70%).
2. **Wykres XAUUSD z EA w trybie monitor-only** (bez routingu, bez sygnału): recorder od razu zapisuje ścieżki złota i spread T0 dla każdego eventu USD (symbol przechodzi filtr walutowy jak jest; potrzebny tylko pip override dla poprawnych „pipsów"). Scoring istniejących i przyszłych `decision_id` względem ścieżki złota post hoc = równoległy shadow A/B bez zmiany ścieżki sygnału.
3. **Test kluczowy offline (bez instrumentu):** czy pre-state przewiduje znak niespodzianki (10.3 pkt 3). Próg > 55% przy n ≥ 40 na event przed jakimkolwiek routingiem live.
4. **Offline:** `build_historical_paths.py` na zipach XAUUSD/XAGUSD (są w scratchpadzie; dociągnąć 2021-22) → learned_stats/favorable-run p80 dla złota od 1. dnia; **re-walidacja „fade ostatnich świec" 2023-26 na własnych ścieżkach FX**.
5. **Offline:** pre-rejestrowane badanie GRXEUR (10.4, H1-H4).
6. **Małe naprawy niezależnie od decyzji:** martwe nazwy kontraktów COT (USD/NZD) + złoto/WTI/NG/TFF z walidacją grepem; mini-scraper actual EIA do logu reakcji; whitelist „GDT Price Index" tylko do rejestrowania ścieżek.
7. Dopiero potem: rejestr profili + pip override EA + cap marginu + bramka spreadu per symbol + tabela routingu z nadpisaniem impactu — generycznie, testowane najpierw na XAUUSD (wszystkie elementy planu DAX są ponownie użyte).
8. **Nie budować teraz:** wejście „reaction mode" po publikacji (nowa semantyka sygnału + wait-for-trigger w EA), syntetyczne źródło eventów dla fixingu/zamknięcia/otwarcia, gotobi, luka weekendowa, option-pin, filtry RSI, srebro, złoto-FOMC, US500-NFP, NGAS live.

**Zastrzeżenia:** wszystkie P to subiektywne kalibracje, nie backtesty na kwotowaniach Purple; badania własne używają HistData (bid, jeden LP) i barów Yahoo (energia) — żadne nie widzi pierwszych 10-30 s spreadu/fillu, które decydują o wejściu T-15 s; 12 CPI + 12 NFP + 8 FOMC rocznie nie potwierdzą edge'u 55% w 12-24 mies. (P=0.30 znaczy „prawdziwa EV dodatnia z prawd. ~0.3", nie „udowodnisz to"); reżim jest dominującym ryzykiem (czułość ES na CPI ×13 między 2009-21 a 2021-23; mapowanie złota na CPI odwrócone w 2025).

---

*Plik roboczy planu; źródło prawdy dla wiki po decyzji o starcie: `wiki/pages/index-open-strategy.md` (powstanie w PR3/PR5).*
