# SkyTower-AI — przegląd systemu

**TL;DR:** Automatyczny system tradingu newsów forex: serwer Python analizuje
eventy makro (kalendarz + COT + sentyment + kontekst rynkowy z MT5) i przez
LLM-y podejmuje decyzję BUY/SELL/SKIP; Expert Advisor w MT5 wykonuje i raportuje.
Wyjściem z pozycji steruje serwer, nie EA.

## Komponenty

| Komponent | Gdzie | Rola |
|-----------|-------|------|
| Serwer Flask (v4.1.0) | [server.py](../../SkyTowerAI/python/server.py) (~2100 linii) + ~20 modułów | Kalendarz, analiza, decyzje LLM, zarządzanie pozycją, dashboard, REST API na `127.0.0.1:5555` (maszyna 24/7 w USA: port 5556) |
| Expert Advisor | [SkyTowerAI_EA.mq5](../../SkyTowerAI/mt5/SkyTowerAI_EA.mq5) (~1950 linii) | Polling `/api/signal`, egzekucja, push M1/M5/M15/H1, raporty pozycji i reakcji; guardraile tylko techniczne (spread, max hold) |
| LLM (OpenRouter) | [llm_decision_engine.py](../../SkyTowerAI/python/llm_decision_engine.py), [exit_decision_engine.py](../../SkyTowerAI/python/exit_decision_engine.py) | Wejścia: panel mieszany `SKYTOWER_ENSEMBLE_MODELS` (kotwica fable-5 + gpt-5.6-sol-pro + gemini-3.1-pro-preview, ~$0.21/event); wyjścia: gemini-3.1-pro-preview. Klucz w `python/.env` |
| Dashboard | [dashboard.html](../../SkyTowerAI/python/templates/dashboard.html) | Eventy, decyzje, Risk & Daily Limits (jedyne miejsce konfiguracji ryzyka), kalibracja, reżimy walut, przewodnik PL |

Uruchamianie natywnie na Windowsie przez `START.bat` — **Docker to wariant
legacy** (nieużywany od 10.07.2026). Szczegóły operacyjne: [RUNBOOK.md](../../SkyTowerAI/RUNBOOK.md).

## Przepływ sygnału (jeden event)

1. Updater (skan co 15 s) wybiera nadchodzący tradeable event
   (`CalendarAggregator._event_is_tradeable`; wypowiedzi bankierów nigdy).
   Uwaga: feed ForexFactory nazywa decyzje stóp per bank centralny — USD to
   „Federal Funds Rate", GBP „Official Bank Rate", CAD „Overnight Rate" — te
   nazwy muszą być na whitliście TIER1 w `config.py` (dodane 29.07.2026 po
   tym, jak selekcja pominęła decyzję FOMC).
2. ~150 s przed publikacją: analiza + decyzja LLM (COT, sentyment kontrariańsko,
   forecast vs previous, kontekst M1 z EA, playbooki, learned stats, kalibracja).
3. EA na wykresie pary decyzji (NZDUSD/USDCAD/AUDUSD/GBPUSD) odbiera sygnał
   z `max_loss_usd` (bez tego pola EA odrzuca sygnał) i wchodzi ~15 s przed eventem.
4. Serwer prowadzi pozycję (MODIFY_SL / PARTIAL_CLOSE / CLOSE przez
   `/api/position/report`); EA ma awaryjny limit czasu i spreadu.
5. Zamknięcie → realized P/L z historii dealów → `logs/trade_history.jsonl`;
   ścieżka ceny eventu → `logs/event_paths.jsonl` (patrz [learning-loop.md](learning-loop.md)).

## Guardraile (stan 2026-07-27)

Max 100 $/trade, 300 $/dzień (blok do północy UTC), 5 tradów/dzień, max 30 min
w pozycji. Całe ryzyko konfiguruje się **wyłącznie w panelu** (persist w
`logs/runtime_overrides.json`); EA nie ma inputów ryzyka.

## Stan projektu

- 696 testów zielonych (pełny przebieg 30.07.2026, ~19 s), wersja serwera 4.1.0.
- Branch `gpt_review`: Stage 1 (recovery pozycji) i Stage 2 (jednostki/SL/retcody)
  zacommitowane i zweryfikowane 26.07; Stage 3–4 otwarte
  ([GPT_REVIEW_PLAN.md](../../SkyTowerAI/GPT_REVIEW_PLAN.md)).
- System ~tydzień na paper tradingu na maszynie 24/7 (USA); przygotowania do LIVE
  ([CALIBRATION_ANALYSIS.md](../../SkyTowerAI/CALIBRATION_ANALYSIS.md)).
- Źródła danych bywają zawodne (COT często brak, Myfxbook 403, FF sporadycznie
  429 — nie hammerować feedu); kontekst rynkowy ratuje push danych z EA.

_Aktualizacja: 2026-07-27 · stan: branch gpt_review (po f8d617d)_
