# Multi-instrument: profile instrumentów i routing event → instrument

_Stan na 2026-08-17 (branch `feature/multi-instrument`, commity e312e37 → 91c1413 → e787417 → e7c4efb → runda review)._

## TL;DR

Ta sama teza newsowa (zaplanowana publikacja + forecast/previous + pozycjonowanie
+ decyzja panelu LLM przed printem), ale instrument dobierany do eventu, a nie
sztywno „waluta → para FX". Pierwszy przypadek: **eventy USD → XAUUSD** (złoto
reaguje w % jak pary FX, a koszt spreadu to 2-8% ruchu zamiast 40-100%; próg
opłacalności trafności spada z ~65% do ~52%). Zero zmian w zachowaniu FX, gdy
routing jest wyłączony (domyślnie).

## Komponenty

| Element | Plik | Rola |
|---|---|---|
| Rejestr profili | `python/instrument_profiles.py` | `InstrumentProfile` (jednostka pipsa na drucie, klampy SL/TP/exit/lot, `default_sl_pips`, spread awaryjny, spread newsowy, bufor BE/trail, prompt wyjść, `ea_inputs`) dla **XAUUSD** (1 pip = $0.10), **GER40** i **US500** (1 pip = 1 pkt). `profile_for(symbol)` → `None` dla KAŻDEJ pary FX (`register()` odrzuca pary FX). Helpery: `symbol_carries_currency`, `same_asset_class`, `validate_routing_symbol`, `normalize_root` (jedyna definicja rootu — `market_context.normalize_pair` deleguje tu). Zero importów projektu. Hold pozycji zostaje własnością panelu (profile nie nadpisują `max_hold_minutes`). |
| Hook jednostek | `trading_units.forex_pip_size` | pierwsza linia: profil → `pip_size`; inaczej reguła FX (0.0001 / 0.01 JPY). Jedyny punkt, przez który idą: `market_context`, `position_manager` (BE/SL/TP tolerancje), `exit_decision_engine` (pasmo MODIFY_TP, BE, trail), `event_path_recorder`, strefy/targety (`pips_to_price`). |
| Klampy silnika | `llm_decision_engine.LLMDecisionEngine` | literały → atrybuty klasy (`LOT_MAX 85`, `EXIT_RANGE 5-15`, `SL_RANGE 25-80`, `TP_RANGE 8-120`, identyczne) + `_limits_for(pair)` (profil dla nie-FX) w obu ścieżkach (single-call i ensemble). |
| Guardraile | `position_manager` | `max_hold_minutes`, `emergency_spread_pips`, cushion prowizji, kadencja LLM wyjść przez `profile_value(symbol, pole, <dzisiejsza wartość>)`. |
| Rejestrator ścieżek | `event_path_recorder._fresh_pairs` | symbole z profilem dopasowywane po walucie kwotowania (XAUUSD/US500 → USD, GER40 → EUR); FX bez zmian. |
| Kalibracja | `calibration.news_spread_pips` | najpierw `typical_news_spread_pips` profilu. |
| Routing | `config.INSTRUMENT_ROUTING` (+ `parse_instrument_routing`, `routing_candidates`), `server._pick_routed_market_entry` w `_build_market_context_for_event` | dla waluty eventu próbuje instrumentów z listy PO KOLEI, bierze pierwszy z **świeżymi** danymi z EA (≤30 min, dopasowanie po roocie, bez fallbacku po walucie bazowej); brak = dotychczasowy przepływ `DEFAULT_PAIRS`. `/api/signal`, lineage, PositionManager nietknięte — decyzja routowana JEST `next_decision`, filtr pary serwuje ją EA na `?pair=XAUUSD.pro`. |
| Prompt | `llm_decision_engine._instrument_brief/_instrument_section` | sekcja INSTRUMENT tylko dla nie-FX (jednostki, zakresy, semantyka kierunku „niespodzianka pro-USD ⇒ SELL XAUUSD", ostrzeżenie że playbooki FX nie przenoszą się 1:1); zapisana w `data_summary['instrument']` → replay renderuje to samo. Prompt FX bajt w bajt identyczny. |
| Panel / API | `/api/config/routing` GET/POST, karta „Instrument Routing" (Event Config) | tabela + status świeżości danych per symbol; POST odrzuca symbol nie-FX bez profilu; persist w `runtime_overrides.json` (`instrument_routing`), env `SKYTOWER_INSTRUMENT_ROUTING="USD:XAUUSD;GBP:XAUUSD,GBPUSD"`. |
| EA | `mt5/SkyTowerAI_Units.mqh`, `SkyTowerAI_EA.mq5` | `InpPipSizeOverride` (0 = reguła FX; XAUUSD 0.10; GER40/US500 1.0) konsultowany jako pierwszy w `SkyPipSize` → wszystkie bramki/raporty w tej jednostce; print `SkyTower SPEC:` w OnInit (digits, point, pip, tick, contract size, wolumeny, dźwignia, margin za 1 lot); root-guard w `ConvertPairToSymbol`; `InpMaxMarginUsePercent` (0 = brak capu = dziś; ~50 na 1:100) przez `OrderCalcMargin`; retcode 10019 logowany. |
| Wiedza | `knowledge/historical_paths.jsonl.gz` (+4 721 ścieżek XAUUSD 2023-26), `knowledge/learned_stats.json` (bloki `XAUUSD` przy eventach USD) | z HistData `XAUUSD_M1_2023..202607` (w `dane historyczne/histdata/`, gitignored). CPI: n=40, \|m30\| mediana 78 pipsów = $7.8, favorable run p80 223 pipsów. |

## Niezmiennik jednostek

„1 pip serwera == 1 pip EA" dla danego symbolu: serwer bierze go z profilu,
EA z `InpPipSizeOverride` na wykresie. **Egzekwowane w runtime (od rundy review
17.08):** EA echo'uje `pip_size` w każdym pushu `/api/market-data` i raporcie
pozycji; `server._unit_mismatch` porównuje z `forex_pip_size(symbol)` (profil lub
reguła FX) — wykres z rozjazdem nie jest routowany (`_pick_routed_market_entry`)
ani nie dostaje sygnału (`/api/signal` odpowiada „Unit mismatch"), a karta
Instrument Routing pokazuje `unit_ok`. Fail-closed dla instrumentów z profilem: brak echa (stary build EA) = jednostka nieznana = brak routingu i sygnału; pary FX bez echa pozostają kompatybilne (`unit_ok: null`). Raport pozycji z rozjazdem: `logger.error` raz na ticket. Twarde ograniczenia EA w tej jednostce: bramka EXTREME ≥15 pipsów (stała) i sanity SL
`InpMinSLPips..InpMaxSLPips` (inputy, default 20-100 — profil podaje wartości w
`ea_inputs`, np. US500 8/60). Decyzja nie-FX bez SL od modelu dostaje
`default_sl_pips` profilu (złoto $8) — nigdy 25-pipsowy fallback EA ($2.50).
Izolacja klas aktywów: cross-pair, epizody, learned-stats fallback i podsumowania
reakcji nie mieszają magnitud złota (0.1 $/pip) z pipsami FX (`same_asset_class`).

## Audyt ścieżki złota (23.08.2026) — co się zmieniło

Pełny audyt kodu ścieżki XAUUSD (routing → prompt → EA → wyjścia → statystyki;
25 potwierdzonych ustaleń po adwersaryjnej weryfikacji) + laboratorium
strategii na 4 721 historycznych ścieżkach (`SkyTowerAI/research/GOLD_STRATEGY_LAB.md`).

| Obszar | Problem | Naprawa |
|---|---|---|
| EA spread | próg EXTREME **zaszyty na 15 pipsów** ($1.50 na złocie = zwykły spread przed publikacją); `InpMaxSpreadPips` > 15 bez znaczenia | input `InpExtremeSpreadPips` (default 15 = FX bez zmian) skaluje tabelę 3/6/10/15; XAUUSD 30 ($3), `InpMaxSpreadPips` 25 |
| EA margin | `InpMaxMarginUsePercent=0` = brak capu → na 1:100 retcode 10019 i stracony event | 0 = auto = dokładnie wolny margin (redukowany tylko lot, który broker odrzuciłby); cap nadal zalecany 50 |
| EA ryzyko | serwer liczył progi od panelowego `max_loss_usd` ($1 000), a margin-capped lot złota ma na stopie ~$160 → profit-protection nieuzbrajalne, model wyjścia okłamywany | EA echo `risk_usd` + `margin_capped` (push, raport, metadane v3); `OpenPosition.effective_risk_usd()` = min(budżet, risk) → próg floor, prompt wyjścia, skalowane reguły fallbacku (30/60/40/−20/15% ryzyka — identyczne przy $100) |
| EA wejście | REQUOTE/PRICE_CHANGED/PRICE_OFF = stracony event | 1 retry po świeżej cenie z 2× tolerancją |
| EA blokada | obca pozycja na symbolu blokowała EA na stałe (do re-attach) | re-check co 30 s, odblokowanie po zamknięciu |
| EA zegar | serwer zgadywał offset brokera ze świec; przy nieświeżych świecach (przerwa złota 21-22 UTC) mylił się o 30/60 min → błędne ścieżki/etykiety | echo `broker_utc_offset_sec` w pushu (autorytatywne); inferencja odrzuca pas 23-29 min; detekcja **zatrzymanych świec** (`bars_advanced_at`) → wykres nie jest świeży |
| Prompt wyjścia | forexowe „$30 → BE", brak legendy jednostek | blok INSTRUMENT (pip, spread w $, bufor BE, ryzyko na stopie) + linia „Entry panel planned the exit around T+X min" (`exit_minutes` z decyzji wchodzi do `OpenPosition.planned_exit_minutes`) |
| Klampy | SL floor 50 ($5) < p80 knotu niekorzystnego CPI (65); sufit 100 < p90 FOMC (101) | `sl_range (60, 120)`; `ea_inputs` InpMinSLPips 60 / InpMaxSLPips 120; ciche klampowanie nie-FX logowane jako WARNING (model odpowiedział w $ zamiast pipsów) |
| Eventy | whitelist forexowa: Home Sales (na złocie −14 pipsów) grane, Core PCE/PPI (+28/+14) nie | `extra_events` / `skip_events` w profilu, `config.routed_event_policy`, predykat `_event_is_tradeable` stosuje je tylko dla waluty routowanej na instrument |
| Strefy | tolerancja 3 pipsy = $0.30 → liquidity pools puste w ~70% okien | `zone_equal_level_tolerance_pips 15`, `zone_min_fvg_pips 10` przez `zone_config_for` |
| Statystyki | bramki 2/1 pipsa = $0.20/$0.10 na złocie; „big move" 15 pipsów = $1.50 | profile: 10 / 5 / 50; 68 bloków XAUUSD przeliczone, FX bajt w bajt; stempel `pip_size` w rekordach ścieżek + filtr rozjazdu jednostek w builderze |
| Epizody | decyzja USDCAD oceniana CORRECT/WRONG na ścieżce XAUUSD (odwrócony werdykt) | werdykt tylko dla tej samej pary, inaczej „(different instrument, not scored)" |
| Routing | alias `USD:GOLD` walidowany, ale nigdy nie pasował do pushu XAUUSD | kanonizacja do nazwy profilu przy zapisie; recorder/reakcje też kanonizują |
| Track record | reakcje bez pair-gate (magnituda złota w prompcie FX) | `get_matching(..., pair=)` |
| `/api/targets` | GET z pipsem 0.0001 dla złota (martwy endpoint) | `forex_pip_size` |

**Przegląd adwersaryjny zmian (16 agentów) — 9 ustaleń naprawionych przed zamknięciem:** echo offsetu wyrównane do siatki 15 min (inaczej rekorder nie trafiał w świecę T0), `_fresh_pairs` zwraca (klucz, nazwa kanoniczna) — alias nie wywala ticku rekordera, `effective_risk_usd` bramkowane do `margin_capped`/profilu (forex z lot_percent 70% zachowuje budżet), pierwsze echo `risk_usd` wygrywa (re-adopcja ze stopem na BE nie nadpisze), auto-cap marginu 100% zamiast 90% (forex nietknięty), metadane v1/v2 nie kasują estymaty, prawdziwe ryzyko liczone po fillu (retry/slippage), flagi zachowane przy adopcji po niejednoznacznym otwarciu. Świadome zmiany widoczne na FX: skalowane progi fallbacku wyjść (identyczne przy $100), linia planowanego horyzontu w prompcie wyjścia, `InpUseSpreadLotReduction` default false.

**Czego laboratorium NIE potwierdziło:** wejścia po publikacji (za 1. świecą
lub przeciw niej) nie mają na złocie spójnej przewagi — jedyny edge to trafny
kierunek PRZED publikacją na CPI/NFP/PCE (+64…+100 pipsów/decyzję w suficie
wyroczni) i wyjście do 15 min. Żadna strategia potwierdzeniowa nie została wdrożona.

## Czego celowo NIE zrobiono (v1)

- Brak drugiego slotu decyzji / drugiej pozycji: jeden event → jeden instrument
  (najlepszy dostępny). Porównanie FX vs złoto = scoring `decision_id` na
  ścieżkach obu instrumentów (recorder mierzy XAUUSD dla każdego eventu USD).
- Brak osobnego budżetu/limitów dla złota — wspólny envelope panelu.
- Otwarcie DAX 09:00 nie jest handlowane (kierunek losowy, patrz
  `SkyTowerAI/research/DAX_OPEN_PLAN.md`); GER40/US500 mają profile „na zapas".

## Jak włączyć (operator)

RUNBOOK → „Instrumenty nie-FX": wykres XAUUSD z EA ≥ 23.08
(`InpPipSizeOverride=0.10`, `InpMaxSpreadPips=25`, `InpExtremeSpreadPips=30`,
`InpEmergencySpreadPips=40`, slippage 30, margin cap 50, SL 60/120, spread-lot
reduction off), odczyt `SkyTower SPEC:`, potem panel → Instrument Routing →
`USD:XAUUSD`. Stary EA (17.08) nadal działa z nowym serwerem — bez echa
`risk_usd`/offsetu serwer zachowuje się jak przed 23.08.

## Testy

`tests/unit/test_instrument_profiles.py` (85: piny FX dla każdej pary, profile
z sufiksami/aliasami, klampy złota/DAX w obu ścieżkach, guardraile, jednostki
exit engine, kalibracja, recorder), `tests/integration/test_instrument_routing.py`
(28: parser env + walidacja strict/non-strict, OFF = identyczne, świeży routowany
wygrywa, brak/stare dane = fallback, kolejność bez fallbacku po prefiksie, blok
INSTRUMENT tylko dla złota i re-renderowalny, sygnał tylko do wykresu złota,
endpoint, rozjazd jednostek blokuje routing i sygnał, izolacja klas aktywów w
cross-pair/statystykach/epizodach/reakcjach). Od 23.08: `tests/unit/test_gold_risk_reality.py`
(22: echo ryzyka, floor profit-protection, prompt wyjścia, skalowanie reguł,
alias routingu, polityka eventów, strefy, klampy) i `tests/unit/test_gold_data_integrity.py`
(14: offset brokera — echo/asymetria/zatrzymane świece, epizody per para,
bramki statystyk i kalibracji, stempel jednostki, `/api/targets`). Łącznie 962 zielone (23.08.2026).

_Aktualizacja: 2026-08-23 · stan: main (audyt ścieżki złota)_
