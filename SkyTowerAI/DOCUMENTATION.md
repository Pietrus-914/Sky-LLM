# SkyTower-AI 4.1 — Dokumentacja Techniczna

> Stan: 30.07.2026 · serwer 4.1.0 · 691 testów zielonych. Operacje i uruchamianie:
> [RUNBOOK.md](RUNBOOK.md) (autorytatywny). Mapa całej dokumentacji:
> `../wiki/pages/documentation-map.md`.

## Spis treści
1. [Przegląd systemu](#przegląd-systemu)
2. [Wymagania](#wymagania)
3. [Architektura](#architektura)
4. [Instalacja](#instalacja)
5. [Konfiguracja](#konfiguracja)
6. [Wydarzenia ekonomiczne](#wydarzenia-ekonomiczne)
7. [Analiza płynności i spreadów](#analiza-płynności-i-spreadów)
8. [Logika decyzyjna](#logika-decyzyjna)
9. [API Reference](#api-reference)
10. [Rozwiązywanie problemów](#rozwiązywanie-problemów)

---

## Przegląd systemu

SkyTower-AI to automatyczny system tradingowy oparty o strategię SkyTower-FX V.3.0, wzbogacony o analizę AI. System wykonuje transakcje na wydarzeniach ekonomicznych wysokiego wpływu (HIGH impact), wykorzystując:

- **Dane COT (Commitment of Traders)** - pozycjonowanie instytucji
- **Sentiment retail** - jako wskaźnik kontrariański
- **Analiza prognoz** - porównanie forecast vs previous
- **LLM/Rule-based** - finalna decyzja o kierunku

### Przepływ danych

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Kalendarz      │────>│   Decision       │────>│   MT5 Expert     │
│   Ekonomiczny    │     │   Engine         │     │   Advisor        │
└──────────────────┘     └──────────────────┘     └──────────────────┘
        │                        │
┌──────────────────┐     ┌──────────────────┐
│   COT Data       │────>│                  │
│   (CFTC)         │     │                  │
└──────────────────┘     │                  │
        │                │                  │
┌──────────────────┐     │                  │
│   Sentiment      │────>│                  │
│   Retail         │     │                  │
└──────────────────┘     └──────────────────┘
```

---

## Wymagania

### Wymagania systemowe
| Komponent | Wymaganie |
|-----------|-----------|
| System operacyjny | Windows 10/11 (64-bit) |
| Python | 3.10 lub nowszy |
| RAM | Minimum 4 GB |
| Internet | Stałe połączenie |

### Wymagania brokerskie
| Parametr | Wartość |
|----------|---------|
| Broker | Purple Trading (lub inny z MT5) |
| Leverage | 1:500 (zalecany) |
| Typ konta | ECN lub Pro (niższe spready) |
| Minimalne saldo | 500-1000 USD (zalecane) |

### Zależności Python
```
flask>=3.0.0
flask-cors>=4.0.0
requests>=2.31.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
pandas>=2.1.0
python-dotenv>=1.0.0
loguru>=0.7.0
pytz>=2024.1
anthropic>=0.18.0  # opcjonalne
openai>=1.12.0     # opcjonalne
```

---

## Architektura

### Struktura plików

Pełne, aktualne drzewo utrzymywane jest w [CLAUDE.md](CLAUDE.md) (sekcja
"File Structure") — poniżej tylko rdzeń:

```
SkyTowerAI/
├── python/           # serwer Flask + ~25 modułów (analiza, decyzje, learning loop)
│   ├── config.py     # konfiguracja globalna (CZYTAJ NAJPIERW)
│   ├── server.py     # REST API + updater w tle (~2100 linii)
│   ├── knowledge/    # playbooki + learned stats (trackowane w git)
│   ├── tools/        # narzędzia offline (serwer ich NIE importuje)
│   └── .env          # OPENROUTER_API_KEY (gitignored)
├── mt5/SkyTowerAI_EA.mq5   # Expert Advisor (~1950 linii)
├── tests/            # 691 testów (pytest)
├── START.bat         # główny launcher (serwer + MT5)
└── RUNBOOK.md        # instrukcja operacyjna (autorytatywna)
```

### Moduły

#### 1. Calendar Fetcher (`calendar_fetcher.py`)
Agreguje dane z wielu źródeł:
- **ForexFactory** (XML feed) - główne źródło
- **TradingEconomics** - backup
- **Finnhub API** - wymaga klucza (opcjonalne)
- **Static Calendar** - fallback gdy API niedostępne

#### 2. COT Analyzer (`cot_analyzer.py`)
Pobiera dane z CFTC (Commodity Futures Trading Commission):
- Pozycje non-commercial (hedge funds, spekulanci)
- Pozycje commercial (hedging)
- Tygodniowe zmiany pozycji
- Generuje sygnał BULLISH/BEARISH

#### 3. Sentiment Analyzer (`sentiment_analyzer.py`)
Zbiera dane o pozycjonowaniu retail:
- **Myfxbook** - Community Outlook
- **FXSSI** - Current Ratio
- **Symulowane dane** - fallback

**WAŻNE:** Sentiment retail używany jest **kontrariańsko** - gdy retail jest 70%+ long, system szuka okazji SHORT.

#### 4. LLM Decision Engine (`llm_decision_engine.py`)
Dwa tryby pracy:
- **LLM Mode (podstawowy)** - panel modeli przez **OpenRouter**
  (`SKYTOWER_ENSEMBLE_MODELS`; wymaga `OPENROUTER_API_KEY`)
- **Rule-based Mode (fallback)** - algorytm punktowy (bez API key)

#### 5. Pozostałe moduły (skrót)
`exit_decision_engine.py` (wyjścia po stronie SERWERA), `position_manager.py`
(pozycje + limity dzienne), `event_path_recorder.py` / `regime_tracker.py` /
`calibration.py` (learning loop), `zone_analyzer.py` + `target_calculator.py`
(strefy płynności i cele), `market_context.py` (dane rynkowe pushowane przez EA).
Pełna lista z opisami: [CLAUDE.md](CLAUDE.md).

---

## Instalacja

### Krok 1: Uruchom instalator
```batch
start_server.bat
```
Skrypt automatycznie:
- Sprawdzi Python
- Utworzy wirtualne środowisko
- Zainstaluje zależności
- Uruchomi serwer

### Krok 2: Konfiguracja MT5
1. Skopiuj `mt5/SkyTowerAI_EA.mq5` do:
   ```
   C:\Users\[USER]\AppData\Roaming\MetaQuotes\Terminal\[ID]\MQL5\Experts\
   ```

2. W MetaEditor (F4) skompiluj plik

3. Włącz WebRequests w MT5:
   - Tools → Options → Expert Advisors
   - ✓ Allow WebRequest for listed URL
   - Dodaj: `http://127.0.0.1:5555`

4. Przeciągnij EA na wykres M1

### Krok 3: Opcjonalne - klucze API
Edytuj `python/.env`:
```env
# LLM przez OpenRouter (tryb podstawowy; bez klucza system działa rule-based)
OPENROUTER_API_KEY=sk-or-xxxxx

# Dla więcej źródeł kalendarza (opcjonalne)
FINNHUB_API_KEY=xxxxx
```

Pełna lista zmiennych środowiskowych (modele, ensemble, tryb testowy
FORCE_DECISION): [CLAUDE.md](CLAUDE.md) → "Environment variables".

---

## Konfiguracja

### Parametry tradingowe (`config.py`)

```python
TRADING_CONFIG = {
    "max_risk_percent": 10.0,      # Max % kapitału na pozycję
    "default_lot_percent": 80.0,   # % max lota do użycia
    "leverage": 500,               # Dźwignia
    "entry_seconds_before": 15,    # Wejście X sekund przed newsem
    "exit_minutes_after": 10,      # Wyjście po X minutach
    "max_spread_pips": 10,         # Max spread do wejścia
}
```

### Parametry EA (MT5)

| Parametr | Domyślna | Opis |
|----------|----------|------|
| InpRiskPercent | 10.0 | Ryzyko % na pozycję |
| InpMaxLotPercent | 80.0 | Max lot % |
| InpMinConfidence | 0.5 | Min pewność do wejścia |
| InpMaxSpreadPips | 10.0 | Max spread (pips) |

Limity ryzyka (max tradów/dzień, max strata dzienna, max strata na trade)
ustawia się WYŁĄCZNIE w panelu (Event Config → Risk & Daily Limits) —
serwer egzekwuje limity dzienne sam, a "max loss per trade" wysyła do EA
w każdym sygnale (`max_loss_usd`).

---

## Wydarzenia ekonomiczne

### Ranking walut (wg skuteczności reakcji)

| Ranking | Waluta | Charakterystyka | Zalecane wydarzenia |
|---------|--------|-----------------|---------------------|
| 1 | **NZD** | Najczystsze reakcje, niska płynność = duże ruchy | Interest Rate, CPI, Employment |
| 2 | **CAD** | Stabilne reakcje, dobra płynność | Interest Rate, Employment, CPI |
| 3 | **AUD** | Dobre reakcje, czasem "szpile" | Interest Rate, Employment, CPI |
| 4 | **USD** | Główna waluta, najwyższa płynność | NFP, CPI, Interest Rate |
| 5 | **GBP** | Zmienne reakcje, wysoka zmienność | Interest Rate, CPI, GDP |

### Szczegółowa analiza wydarzeń

#### Tier 1 - Najwyższy wpływ (zawsze tradować)

| Wydarzenie | Waluty | Typowy ruch | Spread w momencie | Uwagi |
|------------|--------|-------------|-------------------|-------|
| **Interest Rate Decision** | Wszystkie | 50-150 pips | 5-15 pips | Najlepsze reakcje |
| **Non-Farm Payrolls (NFP)** | USD | 50-100 pips | 3-8 pips | Tylko USD, duża płynność |
| **CPI (Inflacja)** | Wszystkie | 30-80 pips | 3-10 pips | Ważne dla polityki monetarnej |

#### Tier 2 - Wysoki wpływ (tradować z ostrożnością)

| Wydarzenie | Waluty | Typowy ruch | Spread w momencie | Uwagi |
|------------|--------|-------------|-------------------|-------|
| **Employment Change** | CAD, AUD, NZD | 30-60 pips | 3-8 pips | Stabilne reakcje |
| **GDP** | Wszystkie | 20-50 pips | 3-8 pips | Często wycenione wcześniej |
| **Retail Sales** | Wszystkie | 20-40 pips | 2-5 pips | Mniejszy wpływ niż CPI |

#### Tier 3 - Średni wpływ (opcjonalnie)

| Wydarzenie | Uwagi |
|------------|-------|
| Trade Balance | Rzadko tradowane |
| Building Permits | Tylko USD |
| Manufacturing PMI | Może być zaszumione |

### Harmonogram wydarzeń (UTC)

| Waluta | Typowa godzina | Dzień tygodnia |
|--------|----------------|----------------|
| NZD | 21:00-22:00 | Wt-Śr |
| AUD | 00:30-01:30 | Wt-Czw |
| JPY | 23:30-00:30 | Nd-Pt |
| GBP | 07:00-12:00 | Wt-Czw |
| EUR | 10:00-14:00 | Pn-Pt |
| CAD | 13:30-15:00 | Śr-Pt |
| USD | 13:30-19:00 | Pn-Pt |

---

## Analiza płynności i spreadów

### Problem spreadów na newsach

**KRYTYCZNE:** Spready dramatycznie rosną w momencie publikacji danych!

```
Normalny spread NZDUSD: 1.2 pips
Spread na Interest Rate: 8-15 pips
Spread na CPI:          5-12 pips
```

### Typowe zachowanie spreadu

```
T-30s:  Spread normalny (1-2 pips)
T-10s:  Spread zaczyna rosnąć (2-4 pips)
T-5s:   Spread wysoki (4-8 pips)
T-0:    PUBLIKACJA - spread maksymalny (8-20 pips)
T+5s:   Spread spada (5-10 pips)
T+30s:  Spread wraca do normy (2-4 pips)
```

### Strategie zarządzania spreadem

#### 1. Wejście przed spreadem (strategia SkyTower)
```
Wejście: 15-20 sekund PRZED newsem
Korzyść: Spread jeszcze normalny
Ryzyko:  Cena może się ruszyć przed newsem
```

#### 2. Maksymalny akceptowalny spread
```python
# config.py
"max_spread_pips": 10  # Nie wchodź jeśli spread > 10 pips
```

#### 3. Wybór par o niższych spreadach

| Para | Normalny spread | Spread na newsie | Zalecenie |
|------|-----------------|------------------|-----------|
| EUR/USD | 0.5-1.0 | 2-5 | ✓ Najlepszy |
| USD/CAD | 1.0-1.5 | 3-6 | ✓ Dobry |
| GBP/USD | 1.0-1.5 | 3-8 | ✓ Dobry |
| AUD/USD | 1.0-1.5 | 3-8 | ✓ Dobry |
| NZD/USD | 1.5-2.0 | 5-12 | ⚠ Uważaj |
| GBP/NZD | 3.0-5.0 | 10-25 | ✗ Unikaj |
| AUD/NZD | 2.5-4.0 | 8-18 | ⚠ Tylko duże wydarzenia |

### Korekta wielkości pozycji na spread

System automatycznie zmniejsza pozycję gdy spread wysoki:

```python
# Logika w EA
if spread > 5 pips:
    lot_size *= 0.8  # Zmniejsz o 20%
if spread > 8 pips:
    lot_size *= 0.6  # Zmniejsz o 40%
if spread > max_spread:
    SKIP TRADE      # Nie wchodź
```

---

## Logika decyzyjna

### Tryb Rule-based (bez API key)

> Trybem podstawowym jest LLM (panel OpenRouter) — scoring poniżej to wyłącznie
> fallback bez klucza API. W trybie LLM confidence/lot/exit zwraca model,
> a pola liczbowe są clampowane po stronie serwera.

System przyznaje punkty na podstawie:

```
┌─────────────────────────────────────────────────────────────┐
│                    SCORING SYSTEM                            │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  FORECAST ANALYSIS                                          │
│  ├─ Forecast > Previous  → +2 BULLISH                       │
│  └─ Forecast < Previous  → +2 BEARISH                       │
│                                                              │
│  COT ANALYSIS (Institutional)                               │
│  ├─ Institutions LONG    → +3 BULLISH                       │
│  └─ Institutions SHORT   → +3 BEARISH                       │
│                                                              │
│  SENTIMENT (Contrarian!)                                    │
│  ├─ Retail 70%+ LONG     → +2 BEARISH (graj przeciwko)     │
│  └─ Retail 70%+ SHORT    → +2 BULLISH (graj przeciwko)     │
│                                                              │
│  FINAL DECISION                                             │
│  ├─ Bullish > Bearish + 2  → BUY                           │
│  ├─ Bearish > Bullish + 2  → SELL                          │
│  └─ Otherwise              → SKIP                           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Wymagana pewność (confidence)

| Confidence | Akcja | Lot % |
|------------|-------|-------|
| < 50% | SKIP | 0% |
| 50-60% | TRADE | 60% |
| 60-70% | TRADE | 70% |
| > 70% | TRADE | 80% |

### Przykład decyzji

```
Wydarzenie: NZD Interest Rate Decision
Forecast: 4.25% (up from 4.00%)

COT Analysis:
  - Institutions: 65% LONG NZD
  - Signal: BULLISH (+3)

Sentiment Analysis:
  - Retail: 70% LONG NZD
  - Contrarian Signal: BEARISH (+2)

Forecast Analysis:
  - Improvement: BULLISH (+2)

SCORING:
  Bullish: 3 (COT) + 2 (Forecast) = 5
  Bearish: 2 (Contrarian) = 2

  Bullish > Bearish + 2? → 5 > 4? → YES

DECISION: BUY NZD/USD
Confidence: 65%
Lot: 70%
```

---

## API Reference

> Poniżej endpointy rdzeniowe. PEŁNA aktualna tabela (position/report,
> position/status, regimes, calibration, config/risk, event-paths, targets…):
> [CLAUDE.md](CLAUDE.md) → "API Reference".

### Base URL
```
http://127.0.0.1:5555
```

### Endpointy

#### GET /health
Sprawdza status serwera.
```json
{
  "status": "ok",
  "timestamp": "2026-01-16T22:00:00",
  "version": "4.1.0"
}
```

#### GET /api/signal
**Główny endpoint dla MT5** - zwraca sygnał tradingowy.
```json
// Gdy jest sygnał:
{
  "signal": true,
  "direction": "BUY",
  "pair": "NZDUSD",
  "lot_percent": 70,
  "confidence": 0.65,
  "entry_seconds_before": 15,
  "exit_minutes": 10,
  "time_until_event": 3600,
  "event_name": "Interest Rate Decision",
  "reasoning": "COT bullish; Forecast improvement"
}

// Gdy brak sygnału:
{
  "signal": false,
  "message": "No trade signal"
}
```

#### GET /api/decision
Pełna decyzja z danymi analitycznymi.
```json
{
  "status": "ok",
  "decision": {
    "event": "Interest Rate Decision",
    "currency": "NZD",
    "pair": "NZD/USD",
    "direction": "BUY",
    "confidence": 0.65,
    "lot_percent": 70,
    "reasoning": "...",
    "data_summary": {
      "cot_analysis": {...},
      "sentiment_analysis": {...},
      "forecast_info": {...}
    }
  }
}
```

#### GET /api/events
Lista nadchodzących wydarzeń.
```
GET /api/events?hours=168&currencies=NZD,CAD,AUD
```

#### GET /api/cot/{currency}
Dane COT dla waluty.
```
GET /api/cot/CAD
```

#### GET /api/sentiment/{pair}
Sentiment dla pary walutowej.
```
GET /api/sentiment/NZDUSD
```

#### POST /api/decision/refresh
Wymuś odświeżenie decyzji.

#### POST /api/trade-executed
Informuje serwer o wykonaniu transakcji (resetuje decyzję).

---

## Rozwiązywanie problemów

### Serwer nie startuje

**Problem:** `ModuleNotFoundError: No module named 'flask'`
```batch
cd python
venv\Scripts\pip install -r requirements.txt
```

**Problem:** Python nie znaleziony
```batch
winget install Python.Python.3.12
```

### MT5 nie łączy się z serwerem

1. Sprawdź czy serwer działa:
   ```
   curl http://127.0.0.1:5555/health
   ```

2. Sprawdź ustawienia WebRequest w MT5:
   - Tools → Options → Expert Advisors
   - URL musi być: `http://127.0.0.1:5555`

3. Sprawdź logi EA w MT5 (zakładka Experts)

### Brak wydarzeń w kalendarzu

**Problem:** `No future events from APIs`

System automatycznie używa statycznego kalendarza. Jest to normalne gdy:
- ForexFactory rate-limited (429)
- Brak wydarzeń HIGH impact w danym okresie

### Brak danych COT

**Problem:** `No COT data found for NZD`

Dane COT:
- Publikowane w piątki po zamknięciu rynku US
- Mogą być opóźnione o 3 dni
- Nie wszystkie waluty mają kontrakty futures (np. SEK, NOK)

### Wysokie spready

**Problem:** Transakcje pomijane z powodu spreadu

1. Sprawdź ustawienie `max_spread_pips` w config
2. Rozważ zmianę brokera na ECN
3. Traduj tylko główne pary (EUR/USD, USD/CAD, GBP/USD)

### Niskie confidence

**Problem:** Wszystkie sygnały to SKIP

Możliwe przyczyny:
- Konfliktujące sygnały (COT vs Sentiment)
- Brak danych COT dla waluty
- Forecast = Previous (brak zmiany)

Rozwiązanie: Poczekaj na lepszą okazję lub dodaj klucz API dla lepszej analizy LLM.

---

## Bezpieczeństwo i ryzyko

### Zasady bezpieczeństwa

1. **ZAWSZE testuj na demo** - minimum 2-3 miesiące
2. **Nigdy nie ryzykuj więcej niż 10%** kapitału na pozycję
3. **Monitoruj spread** przed każdą transakcją
4. **Ogranicz dzienny drawdown** - limity (trady/dzień, strata dzienna) ustawiane w panelu; domyślnie 5 tradów i 300 USD/dzień
5. **Nie traduj przed ważnymi świętami** (niska płynność)

### Znane ryzyka

| Ryzyko | Mitygacja |
|--------|-----------|
| Slippage na newsie | Wejście 15s przed |
| Spread rozjazd | Max spread check |
| Fałszywy sygnał | Multi-factor analysis |
| API niedostępne | Fallback do static data |
| Flash crash | Max hold time 30 min |

---

## Smart Exit System (v5.0)

### Koncepcja

System Smart Exit bazuje na koncepcjach ICT/Smart Money:

1. **Liquidity Pools** - równe highs/lows gdzie retail umieszcza SL
2. **Fair Value Gaps (FVG)** - luki cenowe które rynek "chce" wypełnić
3. **Order Blocks** - ostatnia świeca przed impulsywnym ruchem

### Jak to działa

```
PRZED WEJŚCIEM:
┌────────────────────────────────────────────────────┐
│ 1. EA wysyła dane OHLC do Python server            │
│ 2. zone_analyzer.py wykrywa strefy                 │
│ 3. target_calculator.py oblicza TP1/TP2/SL         │
│ 4. EA otrzymuje inteligentne cele                  │
└────────────────────────────────────────────────────┘

PO WEJŚCIU:
┌────────────────────────────────────────────────────┐
│ Phase 1: Trzymaj pozycję (0-60s)                   │
│ Phase 2: Sprawdzaj TP1 → Partial close 50%         │
│          + Move SL to break-even                   │
│ Phase 3: Trailing stop aktywny                     │
│ Phase 4: TP2 lub timeout → Full close              │
└────────────────────────────────────────────────────┘
```

### Parametry EA (Smart Exit)

> **Aktualizacja 18.07.2026:** inputy InpExitStrategy, InpPartialCloseTP1,
> InpTP1ClosePercent, InpMoveSLToBreakeven, InpTrailAfterTP1,
> InpTrailDistancePips i InpFallbackExitMinutes zostały USUNIĘTE — metody,
> które je czytały, nigdy nie były wywoływane (martwe przełączniki).
> Zarządzanie wyjściem należy do serwera (komendy MODIFY_SL / PARTIAL_CLOSE /
> CLOSE zwracane na /api/position/report). Poniższy opis faz jest historyczny.

| Parametr | Domyślna | Opis |
|----------|----------|------|
| InpUseZoneTargets | true | Pobieraj cele ze stref (/api/targets przy otwarciu) |
| InpMaxHoldMinutes | 30 | Twardy limit czasu po stronie EA — jedyny EA-owy guardrail wyjścia |

### Strategie wyjścia

| Strategia | Opis |
|-----------|------|
| EXIT_ZONE_BASED | Wyjście tylko na strefach |
| EXIT_TIME_BASED | Wyjście po czasie (legacy) |
| EXIT_HYBRID | Strefy z timeout fallback (zalecane) |
| EXIT_PARTIAL_TP | Wielokrotne częściowe zamknięcia |

### API Endpoints (Zone)

#### POST /api/zones
Analiza stref dla symbolu.
```json
// Request
{
  "symbol": "NZDUSD",
  "direction": "BUY",
  "ohlc": [{"time":..., "open":..., "high":..., "low":..., "close":...}, ...]
}

// Response
{
  "status": "ok",
  "analysis": {
    "liquidity_above": [...],
    "liquidity_below": [...],
    "fvg_above": [...],
    "fvg_below": [...],
    "direction_bias": "BUY",
    "bias_strength": 2
  },
  "targets": {
    "tp1": 0.6250,
    "tp2": 0.6280,
    "sl": 0.6180,
    "risk_reward": 1.75
  }
}
```

#### POST /api/targets
Oblicz cele dla pozycji.
```json
// Request
{
  "symbol": "NZDUSD",
  "direction": "BUY",
  "entry_price": 0.6200,
  "ohlc": [...]
}
```

### Konfiguracja stref (`config.py`)

```python
ZONE_CONFIG = {
    "equal_level_tolerance_pips": 3.0,   # Tolerancja dla równych poziomów
    "min_touches_for_liquidity": 2,       # Min dotknięcia dla liquidity pool
    "lookback_bars": 50,                  # Ile świec analizować
    "min_fvg_size_pips": 2.0,            # Minimalny rozmiar FVG
    "min_rr_ratio": 1.5,                  # Minimalny Risk/Reward
    "zone_bias_weight": 2,                # Punkty za potwierdzenie strefami
}

EXIT_CONFIG = {
    "exit_strategy": "hybrid",
    "use_zone_targets": True,
    "partial_close_at_tp1": True,
    "move_sl_to_be_at_tp1": True,
    "trail_after_tp1": True,
    "trail_distance_pips": 10,
}
```

---

## Changelog

### 4.1 (2026-02 → 2026-07) — wersja bieżąca
(numeracja wróciła do wersji raportowanej przez serwer w `/health`: 4.1.0)
- Wyjścia sterowane przez SERWER (`exit_decision_engine`); martwe inputy smart exit usunięte z EA (18.07)
- Całe ryzyko w panelu (Risk & Daily Limits); `max_loss_usd` w każdym sygnale — EA odrzuca sygnał bez niego
- LLM przez OpenRouter: panel modeli na wejściach (`SKYTOWER_ENSEMBLE_MODELS`), osobny model wyjść
- Learning loop F0–F5: decision_id lineage, ścieżki wszystkich eventów, learned stats, kalibracja (per-model), reżimy walut, playbooki eventów
- Tryb natywny Windows (`START.bat`) jako podstawowy; Docker legacy
- Filtr eventów: wspólny predykat + switch `TRADE_ALL_EVENTS`; wypowiedzi bankierów nigdy nie handlowane
- Statystyki dzienne trwałe (`trade_history.jsonl`), realized P/L z historii dealów
- Branch gpt_review: Stage 1 (recovery pozycji) + Stage 2 (jednostki pip/volume, finalizacja SL, retcody)

### v5.0.0 (2026-01)
- **Smart Exit System** - inteligentne wyjście z pozycji
- Wykrywanie stref: Liquidity Pools, FVG, Order Blocks
- Partial close na TP1 z automatycznym BE
- Trailing stop po osiągnięciu TP1
- Zone bias - dodatkowy czynnik decyzyjny
- Nowe endpointy API: `/api/zones`, `/api/targets`
- Nowy plik MQH: `SkyTowerAI_Zones.mqh`

### v4.0.0 (2025-01)
- Pełna automatyzacja systemu
- Integracja LLM (Anthropic/OpenAI)
- Analiza COT z CFTC
- Analiza sentymentu retail (kontrariańska)
- REST API dla komunikacji z MT5
- Multi-source calendar aggregation
- Fallback mechanisms dla wszystkich źródeł danych

---

*Dokumentacja SkyTower-AI 4.1 (aktualizacja 27.07.2026)*
*Bazuje na strategii SkyTower-FX V.3.0 + ICT/Smart Money Concepts*
