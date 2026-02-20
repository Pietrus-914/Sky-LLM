# SkyTower-AI v5.0 - Pełny Kontekst Projektu

> Ten plik zawiera kompletny kontekst projektu do kontynuacji pracy w nowej sesji.
> Ostatnia aktualizacja: 2026-01-21

---

## Szybki Start

```bash
# Uruchom serwer Python
cd SkyTowerAI/python
python server.py

# Test endpointów
curl http://127.0.0.1:5555/health
curl http://127.0.0.1:5555/api/zones/NZDUSD
```

---

## Struktura Projektu

```
SkyTowerAI/
├── python/
│   ├── config.py              # Konfiguracja (ZONE_CONFIG, EXIT_CONFIG)
│   ├── server.py              # Flask REST API
│   ├── zone_analyzer.py       # [NEW v5] Wykrywanie stref SMC
│   ├── target_calculator.py   # [NEW v5] Obliczanie TP/SL
│   ├── calendar_fetcher.py    # Pobieranie kalendarza ekonomicznego
│   ├── cot_analyzer.py        # Analiza danych COT z CFTC
│   ├── sentiment_analyzer.py  # Analiza sentymentu retail
│   ├── llm_decision_engine.py # Silnik decyzyjny (LLM/rule-based)
│   └── requirements.txt
├── mt5/
│   ├── SkyTowerAI_EA.mq5      # Expert Advisor v5.0
│   └── SkyTowerAI_Zones.mqh   # [NEW v5] Include file dla Smart Exit
├── logs/
├── DOCUMENTATION.md           # Pełna dokumentacja
├── CONTEXT_V5.md              # TEN PLIK
└── README.md
```

---

## Główne Założenie Systemu

### Hipoteza (potwierdzona w v5.0)

```
Wydarzenia makroekonomiczne służą instytucjom do:
1. Zbierania liquidity (stop lossy retail)
2. Wypełniania imbalance (FVG)
3. Osiągania ukrytych celów cenowych

News = katalizator/alibi dla dużego ruchu
```

### Flow Decyzyjny

```
┌─────────────────────────────────────────────────────────────────────┐
│                      DECISION FLOW v5.0                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1. KALENDARZ → Znajdź HIGH impact event                            │
│     ├─ Tier 1: Interest Rate, NFP, CPI                              │
│     └─ Tier 2: Employment, GDP, Retail Sales                        │
│                                                                      │
│  2. ANALIZA FUNDAMENTALNA                                           │
│     ├─ COT (instytucje): +3 punkty za kierunek                      │
│     ├─ Sentiment (retail, kontrariański): +2 punkty                 │
│     └─ Forecast vs Previous: +2 punkty                              │
│                                                                      │
│  3. [NEW] ANALIZA STREF (zone_analyzer.py)                          │
│     ├─ Liquidity Pools: gdzie są SL retail?                         │
│     ├─ FVG: niewypełnione luki cenowe                               │
│     ├─ Order Blocks: ostatnia świeca przed impulsem                 │
│     └─ Zone Bias: +2 punkty za potwierdzenie                        │
│                                                                      │
│  4. DECYZJA                                                         │
│     ├─ Bullish > Bearish + 2 → BUY                                  │
│     ├─ Bearish > Bullish + 2 → SELL                                 │
│     └─ Otherwise → SKIP                                             │
│                                                                      │
│  5. WEJŚCIE (15s przed newsem)                                      │
│     └─ Spread check → lot reduction jeśli wysoki                    │
│                                                                      │
│  6. [NEW] SMART EXIT (SkyTowerAI_Zones.mqh)                         │
│     ├─ TP1 (liquidity pool) → Partial close 50%                     │
│     ├─ Move SL to break-even                                        │
│     ├─ Trailing stop aktywny                                        │
│     └─ TP2 lub timeout → Full close                                 │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Kluczowe Pliki - Szczegóły

### 1. zone_analyzer.py

**Cel:** Wykrywanie stref Smart Money Concepts

**Klasy:**
- `ZoneType` - enum: LIQUIDITY_HIGH, LIQUIDITY_LOW, FVG_BULLISH, FVG_BEARISH, ORDER_BLOCK_*
- `Zone` - dataclass z price_high, price_low, strength, touches
- `ZoneAnalysisResult` - wynik analizy z liquidity_above/below, fvg_above/below, direction_bias
- `ZoneAnalyzer` - główna klasa

**Metody kluczowe:**
```python
find_liquidity_pools(bars, symbol) -> (highs, lows)  # Equal highs/lows
find_fvg(bars, symbol) -> (bullish, bearish)         # Fair Value Gaps
find_order_blocks(bars, symbol) -> (bullish, bearish) # Order Blocks
analyze(bars, symbol) -> ZoneAnalysisResult          # Pełna analiza
```

**Algorytm Liquidity Pool:**
```python
# Szukaj swing highs/lows
# Grupuj równe poziomy (tolerance = 3 pips)
# Min 2 dotknięcia = liquidity pool
# Siła zależy od liczby dotknięć
```

**Algorytm FVG:**
```python
# Bullish FVG: low[i-1] > high[i+1] (gap up)
# Bearish FVG: high[i-1] < low[i+1] (gap down)
# Min rozmiar: 2 pips
# Sprawdź czy już wypełniony
```

### 2. target_calculator.py

**Cel:** Obliczanie TP1/TP2/SL na podstawie stref

**Klasy:**
- `TradeTargets` - dataclass z tp1, tp2, sl, risk_reward, confidence
- `TargetCalculator` - główna klasa

**Logika:**
```python
# Dla BUY:
TP1 = najbliższy liquidity pool POWYŻEJ ceny
TP2 = następna znacząca strefa (FVG lub większy liquidity)
SL  = PONIŻEJ najbliższego liquidity pool

# Dla SELL: odwrotnie
```

**Walidacja:**
- Min TP distance: 10 pips
- Max SL distance: 50 pips
- Min Risk/Reward: 1.5

### 3. SkyTowerAI_Zones.mqh

**Cel:** Include file MQL5 dla Smart Exit

**Struktury:**
```cpp
struct SZone {
   ENUM_ZONE_TYPE type;
   double price_high, price_low, midpoint;
   int strength, touches;
   bool is_filled;
};

struct STradeTargets {
   double tp1, tp2, sl;
   double tp1_pips, tp2_pips, sl_pips;
   double risk_reward;
   int tp1_close_percent;
   bool move_sl_to_be_at_tp1;
   bool valid;
};

struct SPositionState {
   ulong ticket;
   string direction;
   double entry_price;
   STradeTargets targets;
   bool tp1_hit, sl_moved_to_be;
};
```

**Klasa CSmartExitManager:**
```cpp
Init(host, port, strategy, ...)           // Inicjalizacja
OnNewPosition(ticket, symbol, direction, price, lots)  // Nowa pozycja
OnTick(bid, ask)                          // Update na każdy tick
ShouldClosePartial(price) -> bool         // Czy zamknąć częściowo?
ShouldMoveSLToBreakeven(price) -> bool    // Czy przesunąć SL?
ShouldTrailStop(price, &new_sl) -> bool   // Czy trailing?
ShouldExitByTime() -> bool                // Czy timeout?
ShouldExitByTarget(price) -> bool         // Czy TP2?
GetTargetsFromServer(symbol, direction, &targets)  // Pobierz z API
```

### 4. SkyTowerAI_EA.mq5 v5.0

**Nowe inputy:**
```cpp
input group "=== Smart Exit Settings ==="
input ENUM_EXIT_STRATEGY InpExitStrategy = EXIT_HYBRID;
input bool     InpUseZoneTargets = true;
input bool     InpPartialCloseTP1 = true;
input int      InpTP1ClosePercent = 50;
input bool     InpMoveSLToBreakeven = true;
input bool     InpTrailAfterTP1 = true;
input double   InpTrailDistancePips = 10.0;
input int      InpMaxHoldMinutes = 30;
input int      InpFallbackExitMinutes = 15;
```

**Globalne zmienne dodane:**
```cpp
CSmartExitManager g_smartExit;
bool g_tp1Hit = false;
bool g_slMovedToBE = false;
double g_originalLots = 0;
```

**Zmodyfikowane funkcje:**
- `OnInit()` - inicjalizacja g_smartExit
- `ExecuteEventTrade()` - wywołanie g_smartExit.OnNewPosition()
- `ManageOpenPositions()` - pełna logika Smart Exit

---

## API Endpoints

### Istniejące (v4.0)
| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/health` | GET | Status serwera |
| `/api/signal` | GET | Sygnał dla MT5 |
| `/api/decision` | GET | Pełna decyzja |
| `/api/events` | GET | Lista wydarzeń |
| `/api/cot/<currency>` | GET | Dane COT |
| `/api/sentiment/<pair>` | GET | Sentiment |

### Nowe (v5.0)
| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/api/zones` | POST | Analiza stref z OHLC |
| `/api/zones/<symbol>` | GET | Test z mock data |
| `/api/targets` | POST | Oblicz TP/SL |
| `/api/config` | GET | Konfiguracja (rozszerzona) |

### Przykład /api/zones

**Request:**
```json
{
  "symbol": "NZDUSD",
  "direction": "BUY",
  "ohlc": [
    {"time": 1705600000, "open": 0.62, "high": 0.621, "low": 0.619, "close": 0.6205},
    ...
  ]
}
```

**Response:**
```json
{
  "status": "ok",
  "analysis": {
    "symbol": "NZDUSD",
    "current_price": 0.6205,
    "liquidity_above": [
      {"type": "liquidity_high", "price_high": 0.6252, "price_low": 0.6248, "midpoint": 0.6250, "strength": 2, "touches": 3}
    ],
    "liquidity_below": [...],
    "fvg_above": [...],
    "fvg_below": [...],
    "direction_bias": "BUY",
    "bias_strength": 2
  },
  "targets": {
    "direction": "BUY",
    "entry_price": 0.6205,
    "tp1": 0.6250,
    "tp2": 0.6280,
    "sl": 0.6175,
    "tp1_pips": 45.0,
    "sl_pips": 30.0,
    "risk_reward_tp1": 1.5,
    "confidence": 0.75
  },
  "targets_valid": true
}
```

---

## Konfiguracja (config.py)

### ZONE_CONFIG
```python
ZONE_CONFIG = {
    # Detection
    "equal_level_tolerance_pips": 3.0,
    "min_touches_for_liquidity": 2,
    "lookback_bars": 50,
    "min_fvg_size_pips": 2.0,
    "min_impulse_multiplier": 2.0,

    # Targets
    "min_rr_ratio": 1.5,
    "max_sl_pips": 50,
    "min_tp_pips": 10,
    "default_sl_pips": 30,
    "default_tp_pips": 40,
    "tp1_close_percent": 50,

    # Decision
    "zone_bias_weight": 2,
    "enable_zone_bias": True,
}
```

### EXIT_CONFIG
```python
EXIT_CONFIG = {
    "exit_strategy": "hybrid",
    "use_zone_targets": True,
    "partial_close_at_tp1": True,
    "move_sl_to_be_at_tp1": True,
    "max_hold_minutes": 30,
    "fallback_exit_minutes": 15,
    "trail_after_tp1": True,
    "trail_distance_pips": 10,
}
```

---

## Ważne Reguły Tradingowe

### Spread Management
```
< 3 pips  → 100% lot
3-6 pips  → 80% lot
6-10 pips → 60% lot
10-15 pips → 40% lot
> 15 pips → SKIP
```

### Currency Priority
```
NZD > CAD > AUD > USD > GBP
```

### Tier 1 Events (zawsze tradować)
- Interest Rate Decision
- Non-Farm Payrolls (NFP)
- CPI (Consumer Price Index)

### Retail Sentiment
**ZAWSZE kontrariański!**
- Retail 70%+ LONG → szukaj SHORT
- Retail 70%+ SHORT → szukaj LONG

---

## Znane Problemy / TODO

1. **Zone bias nie zintegrowany z llm_decision_engine.py**
   - Obecnie zone_bias jest zwracany przez API ale nie dodawany do scoringu
   - TODO: Dodać do `LLMDecisionEngine.analyze_event()`

2. **Brak backtestingu stref**
   - Algorytmy napisane na podstawie teorii SMC
   - TODO: Backtest na historycznych danych M1

3. **MT5 WebRequest limit**
   - Pamiętaj dodać URL do MT5: Tools → Options → Expert Advisors
   - URL: `http://127.0.0.1:5555`

---

## Testowanie

### Test zone_analyzer.py
```bash
cd SkyTowerAI/python
python zone_analyzer.py
# Powinien wyświetlić mock analysis
```

### Test target_calculator.py
```bash
python target_calculator.py
# Powinien wyświetlić BUY i SELL targets
```

### Test server endpoints
```bash
python server.py &
curl http://127.0.0.1:5555/health
curl http://127.0.0.1:5555/api/zones/NZDUSD
curl http://127.0.0.1:5555/api/config
```

### Kompilacja EA
1. Skopiuj `SkyTowerAI_EA.mq5` i `SkyTowerAI_Zones.mqh` do MQL5/Experts/
2. Otwórz MetaEditor (F4)
3. Kompiluj (F7)
4. Sprawdź błędy

---

## Changelog

### v5.0.0 (2026-01-21)
- Smart Exit System z zone-based targets
- Wykrywanie: Liquidity Pools, FVG, Order Blocks
- Partial close na TP1 (50%)
- Auto break-even po TP1
- Trailing stop po TP1
- Zone bias scoring (+2 punkty)
- Nowe endpointy: /api/zones, /api/targets
- Nowy plik: SkyTowerAI_Zones.mqh
- EA v5.0 z nowymi inputami Smart Exit

### v4.0.0 (2025-01)
- Pełna automatyzacja
- LLM integration (Anthropic/OpenAI)
- COT + Sentiment + Forecast analysis
- REST API dla MT5

---

## Kontynuacja Pracy

Aby kontynuować w nowej sesji:

1. Przeczytaj ten plik: `SkyTowerAI/CONTEXT_V5.md`
2. Załaduj skill: `/sky_tower skill indicator` (dla MQL5)
3. Główne pliki do modyfikacji:
   - `python/zone_analyzer.py` - logika wykrywania stref
   - `python/target_calculator.py` - logika celów
   - `mt5/SkyTowerAI_Zones.mqh` - MQL5 Smart Exit
   - `mt5/SkyTowerAI_EA.mq5` - główny EA

---

*Wygenerowano: 2026-01-21*
*Wersja: SkyTower-AI v5.0*
