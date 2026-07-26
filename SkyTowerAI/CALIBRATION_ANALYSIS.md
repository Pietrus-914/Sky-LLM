# Analiza kalibracyjna LLM — 2026-07-26

Własna analiza przed startem LIVE, wykonana na `knowledge/historical_paths.jsonl.gz`
(44 679 rekordów (event, para), 2021-2026, zbudowanych z archiwum ForexFactory +
histdata M1). Tezy audytu GPT potraktowane wyłącznie jako hipotezy do niezależnej
weryfikacji. Skrypt: analiza jednorazowa (scratchpad); wyniki i metoda poniżej.

## Dane i sanity checks

- Użyteczne rekordy po filtrach (test=false, non_data=false, data_status=ok,
  jest move_5min): **34 546**; rozkład lat równomierny (2021-2026); zero duplikatów
  (event_key, para).
- Zgranie czasowe zweryfikowane siłą reakcji: mediana |ruch T+1| = **6.3 pipsa**
  dla HIGH vs **1.3** dla LOW impact (p90: 30.3 vs 5.6) — wyrównanie do minuty
  publikacji jest realne.
- Kierunki: bias waluty mapowany na kierunek pary po stronie kwotowania
  (jak `_currency_bias_to_direction`); eventy odwrócone (unemployment/jobless/
  claims) korygowane wg `LOWER_IS_BETTER_MARKERS`.
- Koszt wejścia: spread newsowy per para z `TYPICAL_NEWS_SPREADS["news"]`
  (USDCAD 4p, AUDUSD/GBPUSD 5p, NZDUSD 8p, EURUSD 3p), liczony raz na wejściu.

## Weryfikacja tez GPT

| Teza GPT | Werdykt | Mój wynik |
|---|---|---|
| „forecast>previous = bullish" ~50.5% (20 214 obs.) | **POTWIERDZONA** | 50.3% raw @T+5 (n=19 728); z korektą lower-is-better 49.8%; HIGH 48.3%; koszyk TIER1 47.5%. Zero przewagi w każdym cięciu i każdym roku (39.8-54.1%). |
| Ledger kalibracji mierzy T0→T+5 bez podstawy wejścia/spreadu | **POTWIERDZONA** (w kodzie) | `calibration.py` oceniał znak `move_5min_pips` od ceny T0, próg płaskości 1 pip przy spreadach 3-8p. Naprawione: patrz „Wdrożone zmiany". |
| Replay: 0 decyzji z mierzalnym wynikiem | **POTWIERDZONA lokalnie** | Lokalna historia: 10 decyzji (3 forced/fake). Realny ledger rośnie na maszynie produkcyjnej od startu paper-tradingu; per-model dopiero od tego commita. |

## Wyniki własne

### 1. Mechanizm reakcji jest realny — premise strategii stoi

Kierunek zaskoczenia (actual vs forecast, znany w T0) przewiduje ruch pary
(HIGH impact, waluty systemowe): **78.4% @T+1, 71.9% @T+5, 67.8% @T+15,
65.1% @T+30** (n≈5.6-6.0 tys.). Ruch NIE jest fade'owany w skali minut:
P(znak T+30 == znak T+5) = **90%** przy |T+5| ≥ 10 pipsów (77% przy 5-10p) —
momentum sprzyja trzymaniu runnerów, co wspiera architekturę wyjść serwera.

### 2. Ale po spreadzie płacą tylko wybrane rodziny eventów

EV netto (wejście T0 w kierunku zaskoczenia = sufit doskonałej wiedzy,
wyjście T+5, minus spread newsowy pary), HIGH impact, waluty systemowe:

| Rodzina | n | hit@5 | EV@5 netto | P(\|m5\|≥spread) |
|---|---|---|---|---|
| decyzje stóp* | 284 | 76.0% (n=25!) | **+22.5p** | 83% |
| CPI | 1 493 | 81.4% | **+18.9p** | 83% |
| Employment Change | 161 | 77.7% | +5.1p | 63% |
| NFP/jobs US | 577 | 75.6% | +4.9p | 60% |
| wages | 318 | 63.4% | +2.5p | 79% |
| unemployment/claims | 1 114 | 70.9% | +0.9p | 61% |
| PMI/ISM | 1 044 | 70.4% | +0.4p | 56% |
| retail sales | 653 | 66.4% | -0.2p | 61% |
| GDP | 387 | 64.0% | -1.9p | 57% |
| pozostałe | 2 558 | 69.9% | -0.1p | 59% |

*decyzje stóp: mało „zaskoczeń liczbowych" (zwykle actual==forecast → n=25 do
hit-rate), ale te które są, płacą najlepiej; realna przewaga na tych eventach
leży w tonie komunikatu — czyli dokładnie tam, gdzie LLM może coś wnieść.

**Koszyk „TIER1-like" (CPI + decyzje stóp + NFP + Employment Change):
EV = +12.4p (T+1) / +13.3p (T+5) / +14.6p (T+30) netto przy 79.2% hit.**
Cała reszta ledwo wychodzi na zero NAWET przy doskonałej wiedzy o zaskoczeniu —
handlowanie ich PRZED publikacją jest ściśle gorsze.

### 3. Stabilność w czasie (pseudo walk-forward, per rok)

Surprise hit@5 (HIGH+syscur): 2021: 62.3%, 2022: 71.5%, 2023: 75.7%,
2024: 75.7%, 2025: 71.7%, 2026 (do lipca): 67.8%. CPI dodatnie EV w każdym
pełnym roku (2022: +35.5p, 2024: +21.0p; 2026 częściowy: -0.9p na n=76).
Reguła pre-release ujemna w każdym roku. Mechanizm nie jest artefaktem
jednego reżimu, ale amplituda faluje — kalibracja musi być krocząca.

### 4. Gradient wielkości reakcji

Tercyle |ruchu T+1| (proxy siły zaskoczenia): mały → hit 54.2%, EV -7.7p;
średni → 72.6%, -4.0p; duży → **86.2%, +14.5p**. Selekcja wielkości (playbooki,
progi surprise) to druga — obok selekcji eventów — dźwignia EV.

### 5. Co z tego wynika dla WEJŚCIOWEGO LLM (kalibracja)

Dwa zmierzone punkty pracy na koszyku TIER1 (T+5, netto):
47.5% trafności → -5.5p (reguła pre-release) i 79.2% → +13.3p (doskonała
wiedza). Interpolując: **break-even ≈ 56.9% trafności kierunku; każdy punkt
procentowy powyżej ≈ +0.6 pipsa/event**. To jest liczba, z którą trzeba
porównywać hit-rate ledgera od poniedziałku.

## Wdrożone zmiany (warstwa pomiarowa)

1. **`model` + `prompt_version` na każdej decyzji** (`TradingDecision`,
   `decision_history`): single-call = model wpisu, panel = `panel:<modele>`,
   fallback = `rule-based`; `ENTRY_PROMPT_VERSION` (bump ręczny przy każdej
   zmianie promptu). Bez tego zmiana modelu/promptu po cichu miesza reżimy
   w jednym ledgerze — kalibracja per model była niemożliwa do policzenia.
2. **Ledger spread-aware** (`calibration.py`): wiersz dostaje `net_pips`
   (podpisany ruch minus spread newsowy pary) i `playable` (|ruch|≥spread);
   summary dostaje `net_ev_pips`, `playable.hit_rate` i `by_model` (n≥10).
   Sam hit-rate kierunkowy > 50% może wciąż tracić pieniądze — teraz to widać.
3. Bez zmian w logice decyzyjnej ani promptach (dzień przed live).

## Rekomendacje operacyjne

1. **Start live na koszyku TIER1**: w panelu (checkboxy działają od fixu
   26.07) zostawić włączone tylko: Interest Rate/Cash Rate/Official Cash Rate,
   CPI/Consumer Price Index, NFP/Non-Farm Payrolls, Employment Change.
   TRADE_ALL_EVENTS = OFF. GDP/Retail Sales/Unemployment Rate wyłączyć —
   dane pokazują, że nie płacą za spread nawet przy doskonałej trafności.
2. **Cel kalibracyjny**: hit-rate ledgera (koszyk) musi przekroczyć ~57%,
   nim można mówić o przewadze; `net_ev_pips` > 0 to właściwe kryterium.
3. **Po n≥100 decyzjach nie-forced per rodzina**: dopasować kalibrację
   isotonic/Platt confidence→P(hit) per (model, prompt_version, rodzina),
   na splicie czasowym (ucz do daty t, testuj po t) — nigdy losowym.
4. Reguła forecast-vs-previous pozostaje w prompt/rule-score jako KONTEKST,
   ale nie wolno jej traktować jako źródła kierunku (50%). Przy najbliższej
   iteracji promptu: obniżyć jej rangę narracyjną (bump ENTRY_PROMPT_VERSION).

## Ograniczenia metody

Wejście T0 to przybliżenie wejścia T-15s (dryf ostatnich 15 s pomija);
spread stały wg tabeli (realny bywa szerszy w pierwszej sekundzie); brak
slippage/commission; ścieżki liczone z mid histdata; decyzje stóp mają mało
liczbowych zaskoczeń (n=25) — tam ceiling jest niedoszacowany. Wszystkie te
błędy działają W OBIE strony podobnie dla porównań względnych (rodziny,
progi), ale bezwzględne EV traktować jako ±kilka pipsów.
