# Multi-instrument: profile instrumentów i routing event → instrument

_Stan na 2026-08-17 (branch `feature/multi-instrument`, commity e312e37 → 91c1413 → e787417 + docs)._

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
| Rejestr profili | `python/instrument_profiles.py` | `InstrumentProfile` (jednostka pipsa na drucie, klampy SL/TP/exit/lot, max hold, spread awaryjny, spread newsowy, bufor BE/trail, prompt wyjść) dla **XAUUSD** (1 pip = $0.10), **GER40** i **US500** (1 pip = 1 pkt). `profile_for(symbol)` → `None` dla KAŻDEJ pary FX (`register()` odrzuca pary FX). Zero importów projektu. |
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
EA z `InpPipSizeOverride` na wykresie. To konwencja weryfikowana przy dry-runie
(print SPEC), nie egzekwowana w runtime. Twarde ograniczenia EA nadal obowiązują
w tej jednostce: bramka EXTREME ≥15 pipsów i sanity SL 20-100 pipsów — dlatego
profile mają `sl_range` z sufitem 100 (złoto $10, DAX 100 pkt).

## Czego celowo NIE zrobiono (v1)

- Brak drugiego slotu decyzji / drugiej pozycji: jeden event → jeden instrument
  (najlepszy dostępny). Porównanie FX vs złoto = scoring `decision_id` na
  ścieżkach obu instrumentów (recorder mierzy XAUUSD dla każdego eventu USD).
- Brak osobnego budżetu/limitów dla złota — wspólny envelope panelu.
- Otwarcie DAX 09:00 nie jest handlowane (kierunek losowy, patrz
  `SkyTowerAI/research/DAX_OPEN_PLAN.md`); GER40/US500 mają profile „na zapas".

## Jak włączyć (operator)

RUNBOOK → „Instrumenty nie-FX (od 17.08.2026)": wykres XAUUSD z EA ≥ 17.08
(`InpPipSizeOverride=0.10`, spread 15/40, slippage 30, margin cap 50), odczyt
`SkyTower SPEC:`, potem panel → Instrument Routing → `USD:XAUUSD`.

## Testy

`tests/unit/test_instrument_profiles.py` (85: piny FX dla każdej pary, profile
z sufiksami/aliasami, klampy złota/DAX w obu ścieżkach, guardraile, jednostki
exit engine, kalibracja, recorder), `tests/integration/test_instrument_routing.py`
(16: parser env, OFF = identyczne, świeży routowany wygrywa, brak/stare dane =
fallback, kolejność bez fallbacku po prefiksie, blok INSTRUMENT tylko dla złota
i re-renderowalny, sygnał tylko do wykresu złota, endpoint). Łącznie 842 zielone.
