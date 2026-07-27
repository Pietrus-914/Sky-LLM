# SkyTower-AI - Wiedza Sesyjna (Session Knowledge)

**Data aktualizacji:** 2026-01-19
**Wersja systemu:** 4.0.0

---

## 1. Przegląd Systemu

### Czym jest SkyTower-AI?
Automatyczny system tradingowy forex, który handluje na wydarzeniach ekonomicznych HIGH impact. Wykorzystuje:
- **Dane COT** (Commitment of Traders) - pozycje instytucji
- **Sentiment retail** - używany kontrariańsko (gramy przeciwko tłumowi)
- **Analiza prognoz** - porównanie forecast vs previous
- **LLM lub Rule-based** - finalna decyzja kierunku

### Architektura
```
┌─────────────────────────────────────────────────────────────────┐
│                    SkyTower-AI System                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐ │
│  │  Kalendarz   │────>│  Python Server  │────>│  MT5 Expert  │ │
│  │  (ForexFactory)    │  (Flask:5555)   │     │  Advisor     │ │
│  └──────────────┘     └─────────────────┘     └──────────────┘ │
│                              │                                   │
│  ┌──────────────┐     ┌─────────────────┐                      │
│  │  COT Data    │────>│  LLM Decision   │                      │
│  │  (CFTC)      │     │  Engine         │                      │
│  └──────────────┘     └─────────────────┘                      │
│                              │                                   │
│  ┌──────────────┐            │                                  │
│  │  Sentiment   │───────────>│                                  │
│  │  (Myfxbook)  │                                               │
│  └──────────────┘                                               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Struktura Plików

```
C:\Users\pietr\Documents\Sky tower\
├── SkyTowerAI/
│   ├── python/
│   │   ├── config.py              # KLUCZOWY - wszystkie parametry
│   │   ├── server.py              # Flask REST API (~300 linii)
│   │   ├── calendar_fetcher.py    # Pobieranie kalendarza (~600 linii)
│   │   ├── cot_analyzer.py        # Analiza COT (~400 linii)
│   │   ├── sentiment_analyzer.py  # Sentiment retail (~500 linii)
│   │   ├── llm_decision_engine.py # Silnik decyzyjny (~450 linii)
│   │   ├── requirements.txt       # Zależności Python
│   │   └── .env.example           # Szablon zmiennych środowiskowych
│   ├── mt5/
│   │   └── SkyTowerAI_EA.mq5      # Expert Advisor (~500 linii)
│   ├── CLAUDE.md                  # Kontekst projektu dla Claude
│   ├── DOCUMENTATION.md           # Pełna dokumentacja techniczna
│   ├── SESSION_KNOWLEDGE.md       # TEN PLIK - wiedza sesyjna
│   ├── README.md                  # Szybki start
│   ├── start_server.bat           # Uruchamianie serwera
│   └── test_system.bat            # Testy systemu
├── .claude/
│   └── commands/
│       └── sky_tower.md           # Slash command /sky_tower
├── instruction.md                 # Reguły oszczędzania tokenów
├── CLAUDE.md                      # Root context
└── SkyTower-FX_V.3.0.pdf          # Oryginalna strategia (21MB)
```

---

## 3. Konfiguracja (config.py)

### Parametry Tradingowe
```python
TRADING_CONFIG = {
    "max_risk_percent": 10.0,       # Max 10% kapitału na pozycję
    "default_lot_percent": 80.0,    # 80% max lota (margines bezpieczeństwa)
    "leverage": 500,                 # Dźwignia 1:500
    "entry_seconds_before": 15,     # Wejście 15 sekund przed newsem
    "exit_minutes_after": 10,       # Wyjście po 10 minutach
    "max_spread_pips": 10,          # Max spread do wejścia
}
```

### Wydarzenia do Tradowania

#### Tier 1 - Najlepsze Reakcje (Zawsze Tradować)
| Wydarzenie | Typowy ruch | Spread na news |
|------------|-------------|----------------|
| Interest Rate Decision | 50-150 pips | 5-15 pips |
| NFP (Non-Farm Payrolls) | 50-100 pips | 3-8 pips |
| CPI (Inflacja) | 30-80 pips | 3-10 pips |

#### Tier 2 - Dobre Reakcje (Z Ostrożnością)
| Wydarzenie | Typowy ruch | Spread na news |
|------------|-------------|----------------|
| Employment Change | 30-60 pips | 3-8 pips |
| GDP | 20-50 pips | 3-8 pips |
| Retail Sales | 20-40 pips | 2-5 pips |

### Ranking Walut
| Priorytet | Waluta | Najlepsza para | Charakterystyka |
|-----------|--------|----------------|-----------------|
| 1 | NZD | NZD/USD | Najczystsze reakcje, niska płynność = duże ruchy |
| 2 | CAD | USD/CAD | Stabilne reakcje, dobra płynność |
| 3 | AUD | AUD/USD | Dobre reakcje, czasem "szpile" |
| 4 | USD | USD/CAD | Główna waluta, najwyższa płynność |
| 5 | GBP | GBP/USD | Zmienne reakcje, wysoka zmienność |

### Zarządzanie Spreadem (KRYTYCZNE!)

**Spready są ZAWSZE wysokie na newsach!**

Typowe zachowanie:
```
T-30s:  Normalny spread (1-2 pips)
T-10s:  Spread rośnie (2-4 pips)
T-5s:   Spread wysoki (4-8 pips)
T-0:    PUBLIKACJA - spread maksymalny (8-20 pips)
T+30s:  Spread wraca do normy (2-4 pips)
```

Redukcja lota w zależności od spreadu:
| Spread | Akcja | Lot % |
|--------|-------|-------|
| < 3 pips | Normalny | 100% |
| 3-6 pips | Uwaga | 80% |
| 6-10 pips | Ostrożność | 60% |
| > 15 pips | NIE WCHODŹ | 0% |

Pary do unikania przy niskiej płynności:
- AUD/NZD, NZD/CAD, GBP/NZD, EUR/NZD

---

## 4. Logika Decyzyjna

### Tryb Rule-based (bez API key)

```
ANALIZA FORECAST:
├─ Forecast > Previous  → +2 BULLISH
└─ Forecast < Previous  → +2 BEARISH

ANALIZA COT (Pozycje instytucji):
├─ Instytucje LONG      → +3 BULLISH
└─ Instytucje SHORT     → +3 BEARISH

SENTIMENT (KONTRARIAŃSKI - retail zwykle się myli):
├─ Retail 70%+ LONG     → +2 BEARISH (gramy przeciwko)
└─ Retail 70%+ SHORT    → +2 BULLISH (gramy przeciwko)

FINALNA DECYZJA:
├─ Bullish > Bearish + 2  → BUY
├─ Bearish > Bullish + 2  → SELL
└─ W przeciwnym razie     → SKIP (brak transakcji)
```

### Progi Pewności (Confidence)
| Confidence | Akcja | Lot % |
|------------|-------|-------|
| < 50% | SKIP | 0% |
| 50-60% | TRADE | 60% |
| 60-70% | TRADE | 70% |
| > 70% | TRADE | 80% |

### Przykład Decyzji
```
Wydarzenie: NZD Interest Rate Decision
Forecast: 4.25% (wzrost z 4.00%)

COT Analysis:
  - Instytucje: 65% LONG NZD
  - Sygnał: BULLISH (+3)

Sentiment Analysis:
  - Retail: 70% LONG NZD
  - Sygnał kontrariański: BEARISH (+2)

Forecast Analysis:
  - Poprawa: BULLISH (+2)

SCORING:
  Bullish: 3 (COT) + 2 (Forecast) = 5
  Bearish: 2 (Contrarian) = 2

  Bullish > Bearish + 2? → 5 > 4? → TAK

DECYZJA: BUY NZD/USD
Confidence: 65%
Lot: 70%
```

---

## 5. API Reference

**Base URL:** `http://127.0.0.1:5555`

### Endpointy

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/health` | GET | Status serwera |
| `/api/signal` | GET | **Główny dla MT5** - sygnał tradingowy |
| `/api/events` | GET | Lista nadchodzących wydarzeń |
| `/api/decision` | GET | Pełna decyzja z danymi analizy |
| `/api/cot/{currency}` | GET | Dane COT dla waluty |
| `/api/sentiment/{pair}` | GET | Sentiment dla pary |

### Przykład odpowiedzi `/api/signal`
```json
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
```

---

## 6. Źródła Danych

| Dane | Źródło | Bezpłatne? |
|------|--------|------------|
| Kalendarz | ForexFactory, TradingEconomics | Tak |
| COT | CFTC (publicreporting.cftc.gov) | Tak |
| Sentiment | Myfxbook, FXSSI | Tak |
| LLM | Anthropic Claude / OpenAI GPT | Wymaga API key |

---

## 7. Harmonogram Wydarzeń (UTC)

| Waluta | Typowa godzina | Dni tygodnia |
|--------|----------------|--------------|
| NZD | 21:00-22:00 | Wt-Śr |
| AUD | 00:30-01:30 | Wt-Czw |
| JPY | 23:30-00:30 | Nd-Pt |
| GBP | 07:00-12:00 | Wt-Czw |
| EUR | 10:00-14:00 | Pn-Pt |
| CAD | 13:30-15:00 | Śr-Pt |
| USD | 13:30-19:00 | Pn-Pt |

---

## 8. Rozwiązywanie Problemów

| Problem | Rozwiązanie |
|---------|-------------|
| Serwer nie startuje | Sprawdź Python 3.10+, `pip install -r requirements.txt` |
| MT5 nie łączy się | Włącz WebRequest w MT5 Tools→Options→Expert Advisors |
| Brak wydarzeń | Sprawdź filtr walut, użyj static calendar |
| Zawsze SKIP | Sprawdź dane COT/sentiment, rozważ API key LLM |
| Błędy spreadu | Używaj głównych par (EUR/USD, USD/CAD) |

---

## 9. Polecenie /sky_tower

Dostępne akcje:
```
/sky_tower status    - Sprawdź status serwera
/sky_tower events    - Lista nadchodzących wydarzeń
/sky_tower decision  - Pobierz sygnał tradingowy
/sky_tower start     - Uruchom serwer Python
/sky_tower test      - Uruchom testy systemu
/sky_tower config    - Pokaż konfigurację
/sky_tower analyze   - Analizuj wydarzenie/walutę
/sky_tower help      - Pokaż pomoc
```

---

## 10. Reguły Oszczędzania Tokenów

1. **Czytaj pliki .md najpierw** - CLAUDE.md i config.py mają wszystkie kluczowe info
2. **Używaj agentów do głębokiej analizy** - pracują w osobnym kontekście
3. **Zlokalizowane odczyty** - `Read(file, offset=X, limit=Y)` dla dużych plików
4. **Pliki MQ5 są duże (500+ linii)** - unikaj czytania całości
5. **Zaufaj edycjom** - nie czytaj ponownie po edycji

---

## 11. Zasady Bezpieczeństwa

1. **ZAWSZE testuj na demo** - minimum 2-3 miesiące
2. **Nigdy nie ryzykuj więcej niż 10%** kapitału na pozycję
3. **Monitoruj spread** przed każdą transakcją
4. **Ogranicz dzienny drawdown** - max 3 transakcje/dzień
5. **Nie traduj przed ważnymi świętami** (niska płynność)
6. **Nigdy nie omijaj sprawdzania spreadu** - chroni przed stratami

---

*Dokument wygenerowany: 2026-01-19*
*Wersja: 4.0.0*
*Projekt: SkyTower-AI*
