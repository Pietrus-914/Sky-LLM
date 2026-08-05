# Learning loop (F0–F5)

**TL;DR:** System sam zbiera dane do nauki z każdego eventu (nie tylko
tradowanego) i wstrzykuje wnioski do promptu LLM. Cały plan F0–F5 wdrożony
17–18.07.2026 (branch `learning-loop-f3-f2`, merge do maina ręcznie przez
operatora); nic nie wymaga klikania.

## Fazy i gdzie leżą

| Faza | Co robi | Pliki |
|------|---------|-------|
| F0/F1 | Lineage: `decision_id` (uuid) spina decyzję → sygnał → trade → reakcję; pełny prompt+odpowiedź w `logs/decision_context/<id>.json` (cap 2000) | [decision_history.py](../../SkyTowerAI/python/decision_history.py), [position_manager.py](../../SkyTowerAI/python/position_manager.py) |
| Rejestrator ścieżek | Mierzy ścieżki cen WSZYSTKICH monitorowanych eventów z pushowanych M1 (T+31 → give-up T+50); + 44 679 ścieżek historycznych 2021–26 | [event_path_recorder.py](../../SkyTowerAI/python/event_path_recorder.py), `logs/event_paths.jsonl`, `knowledge/historical_paths.jsonl.gz` (trackowany) |
| F3 | Maszynowe statystyki per event w prompcie (hot-reload) | `knowledge/learned_stats.json` — **nie edytować ręcznie**; regeneracja: `python tools/build_learned_stats.py` |
| F2 | EA echo'uje `decision_id` w raportach (wymaga zrekompilowanego `.ex5` na maszynie 24/7) | [SkyTowerAI_EA.mq5](../../SkyTowerAI/mt5/SkyTowerAI_EA.mq5) |
| F4a | Ledger kalibracji (od n≥50 zmierzonych, bez forced, linia w prompcie; klucze per-model, spread-aware od f8d617d) | `GET /api/calibration` + karta Calibration |
| F4b | Ensemble: `SKYTOWER_ENSEMBLE_K` (default 1 = wyłączony; koszt K×) oraz panel mieszany `SKYTOWER_ENSEMBLE_MODELS` (aktywny od 22.07) | [llm_decision_engine.py](../../SkyTowerAI/python/llm_decision_engine.py) |
| F5 | Epizody + refleksje n=1 + destylacja playbooków za zgodą operatora + replay harness | `knowledge/event_playbooks.json` (kuratorski, hot-reload) |
| Reżimy | RegimeTracker: auto-reżimy z decyzji o stopach, LLM adjudykuje tylko niejednoznaczne holdy; EUR/JPY/CHF bez wykresów = tylko seed/manual | [regime_tracker.py](../../SkyTowerAI/python/regime_tracker.py), `GET/POST /api/regimes` |

## Wiedza kuratorska vs maszynowa

- `knowledge/event_playbooks.json` — 30 wpisów z badania screenów 2017–20
  (klucz: „fade ostatnich świec", 78%); edytowalny ręcznie, trackowany w git.
  Workflow badania: `research/screens/README.md`.
- `knowledge/learned_stats.json` — generowany skryptem, nie ruszać ręcznie.
  Od 05.08.2026 zawiera też `favorable_run_5min`/`favorable_run_30min`
  (ekskursja Z kierunkiem ruchu, mediana/p75/**p80**/p90) — kotwica dla
  `take_profit_pips`: prompt każe mieścić TP między medianą a p80 favorable
  run dla okna wyjścia (minus bieżący spread — statystyka mierzy travel bidu).
- Pola liczbowe odpowiedzi LLM są clampowane (conf 0–1, lot≤85, exit 5–15,
  SL 25–80, TP **8**–120 — floor obniżony z 30, bo dla małych eventów p75
  ruchu 30-min bywa < 30 pipsów i TP nigdy nie był osiągalny).
- `ENTRY_PROMPT_VERSION = 2026-08-05.1` (zmiana promptu TP) — kalibracja
  liczy się per wersja, więc linia kalibracji zamilknie do n≥50 nowej wersji.

## Operacje i pułapki

- Komendy dzienne i regeneracja statystyk: sekcja „Learning loop (F0-F4) —
  operacje" w [RUNBOOK.md](../../SkyTowerAI/RUNBOOK.md).
- `forced:true` (tryb FORCE_DECISION) jest filtrowane z track recordu i trade
  outcomes — dane testowe nie zatruwają nauki; fake eventy mają `test:true`.
- Czasy barów MT5 = czas brokera; offset inferowany z guardem, etykiety świec
  w prompcie korygowane do UTC.
- Analiza przed LIVE na danych historycznych: [CALIBRATION_ANALYSIS.md](../../SkyTowerAI/CALIBRATION_ANALYSIS.md).

_Aktualizacja: 2026-08-05 · stan: branch gpt_review (favorable run + TP 8–120, prompt 2026-08-05.1)_
