# SkyTower-AI v4.0

## AI-Enhanced News Trading System for MT5

Automatyczny system tradingowy oparty o strategię SkyTower-FX z ulepszeniami AI.

---

## Architektura

```
┌─────────────────────────────────────────────────────────────┐
│                    SKYTOWER-AI V.4.0                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │ Economic    │    │    LLM      │    │    MT5      │     │
│  │ Calendar    │───>│  Decision   │───>│   Expert    │     │
│  │    API      │    │   Engine    │    │   Advisor   │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│         │                  │                  │             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │    COT      │    │ Sentiment   │    │   Risk      │     │
│  │  Analyzer   │    │  Analysis   │    │  Manager    │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Instalacja

### 1. Wymagania

- Python 3.10+
- MT5 Terminal (Purple Trading)
- Windows 10/11

### 2. Instalacja Python

1. Uruchom `start_server.bat` - automatycznie:
   - Utworzy wirtualne środowisko
   - Zainstaluje zależności
   - Utworzy plik `.env`

2. (Opcjonalnie) Dodaj klucze API do pliku `python/.env`:
   ```
   ANTHROPIC_API_KEY=twoj_klucz
   # lub
   OPENAI_API_KEY=twoj_klucz
   ```

   **Bez kluczy API system działa w trybie rule-based** (używa reguł zamiast AI)

### 3. Instalacja EA w MT5

1. Skopiuj `mt5/SkyTowerAI_EA.mq5` do:
   ```
   C:\Users\[USER]\AppData\Roaming\MetaQuotes\Terminal\[ID]\MQL5\Experts\
   ```

2. W MT5 otwórz MetaEditor (F4) i skompiluj plik

3. W MT5 włącz WebRequests:
   - Tools → Options → Expert Advisors
   - Zaznacz "Allow WebRequest for listed URL"
   - Dodaj: `http://127.0.0.1:5555`

4. Przeciągnij EA na dowolny wykres M1

---

## Użycie

### Tryb Automatyczny (Zalecany)

1. Uruchom `start_server.bat`
2. Uruchom MT5 z EA
3. System automatycznie:
   - Monitoruje nadchodzące wydarzenia
   - Analizuje dane COT, sentiment, prognozy
   - Podejmuje decyzje (LLM lub rule-based)
   - Wykonuje zlecenia przed wydarzeniami
   - Zamyka pozycje po ustalonym czasie

### Tryb Manualny (API)

Serwer udostępnia REST API:

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/health` | GET | Status serwera |
| `/api/signal` | GET | Sygnał dla MT5 |
| `/api/decision` | GET | Pełna decyzja z uzasadnieniem |
| `/api/events` | GET | Nadchodzące wydarzenia |
| `/api/cot/<currency>` | GET | Dane COT dla waluty |
| `/api/sentiment/<pair>` | GET | Sentiment dla pary |

Przykład:
```bash
curl http://127.0.0.1:5555/api/signal
```

---

## Źródła Danych (Darmowe)

### Kalendarz Ekonomiczny
- Investing.com (scraping)
- Myfxbook (scraping)
- Finnhub API (opcjonalnie, wymaga darmowego klucza)

### COT (Commitments of Traders)
- CFTC Public API
- Dane cotygodniowe o pozycjach instytucji

### Sentiment Retail
- Myfxbook Community Outlook
- FXSSI Current Ratio
- Używane jako **wskaźnik kontrariański**

---

## Strategia

### Waluty (w kolejności skuteczności)
1. **NZD** - najlepsze reakcje
2. **CAD** - stabilne reakcje
3. **AUD** - dobre, ale czasem "szpile"
4. **USD** - główna waluta światowa
5. **GBP** - zmienne reakcje

### Wydarzenia
- Stopy procentowe (Interest Rate Decision)
- CPI (Inflacja)
- NFP (Non-Farm Payrolls) - tylko USD
- PKB (GDP)
- Sprzedaż detaliczna (Retail Sales)

### Logika Decyzji

```python
# Uproszczona logika
if cot_institutional == "BULLISH":
    score += 3  # Podążaj za instytucjami

if retail_sentiment > 70%_long:
    score -= 2  # Graj przeciwko retailowi (kontrariański)

if forecast > previous:
    score += 2  # Poprawa = umocnienie waluty
```

---

## Ulepszenia vs Oryginał

| Oryginalna Strategia | SkyTower-AI |
|----------------------|-------------|
| Ręczna analiza prognoz | Automatyczna analiza trendów |
| VLC/WLC jako jedyny sygnał | Multi-factor: COT + sentiment + prognozy |
| Stały czas wyjścia | Dynamiczny exit (5-15 min) |
| Brak detekcji anomalii | Ochrona przed wysokim spreadem |
| Ręczna konfiguracja | Pełna automatyzacja |
| Brak analizy instytucji | Analiza COT (CFTC) |

---

## Konfiguracja

### Plik `config.py`

```python
TRADING_CONFIG = {
    "max_risk_percent": 10.0,      # Max 10% kapitału na pozycję
    "default_lot_percent": 80.0,   # 80% max lota
    "leverage": 500,               # 1:500
    "entry_seconds_before": 15,    # Wejście 15s przed
    "exit_minutes_after": 10,      # Wyjście po 10 min
    "max_spread_pips": 10,         # Max spread
}
```

### Parametry EA

| Parametr | Domyślna | Opis |
|----------|----------|------|
| InpRiskPercent | 10.0 | Ryzyko % na pozycję |
| InpMaxLotPercent | 80.0 | Max lot % |
| InpMinConfidence | 0.5 | Min pewność AI do wejścia |
| InpMaxSpreadPips | 10.0 | Max spread |

Limity ryzyka (trady/dzień, strata dzienna, strata na trade) ustawia się
wyłącznie w dashboardzie (Event Config → Risk & Daily Limits) — serwer
przekazuje `max_loss_usd` do EA z każdym sygnałem.

---

## Testowanie

```bash
# Uruchom testy wszystkich komponentów
test_system.bat
```

---

## Bezpieczeństwo

- **NIGDY** nie grasz więcej niż 10% kapitału
- **ZAWSZE** sprawdzaj spread przed wejściem
- **TESTUJ** na koncie demo minimum 2 miesiące
- System automatycznie pomija sygnały z niską pewnością (<50%)

---

## Rozwiązywanie Problemów

### Serwer nie startuje
```
pip install -r requirements.txt
```

### MT5 nie łączy się z serwerem
1. Sprawdź czy serwer działa (`http://127.0.0.1:5555/health`)
2. Dodaj URL do dozwolonych w MT5

### Brak danych COT
- Dane COT publikowane są w piątki
- Mogą być opóźnione o 3 dni

### Brak wydarzeń
- Sprawdź filtry walut w konfiguracji
- Niektóre tygodnie mają mało wydarzeń HIGH impact

---

## Struktura Plików

```
SkyTowerAI/
├── python/
│   ├── config.py              # Konfiguracja
│   ├── calendar_fetcher.py    # Pobieranie kalendarza
│   ├── cot_analyzer.py        # Analiza COT
│   ├── sentiment_analyzer.py  # Analiza sentymentu
│   ├── llm_decision_engine.py # Silnik decyzyjny
│   ├── server.py              # Flask API
│   └── requirements.txt       # Zależności
├── mt5/
│   └── SkyTowerAI_EA.mq5     # Expert Advisor
├── start_server.bat           # Uruchomienie serwera
├── test_system.bat            # Testy
└── README.md                  # Ta dokumentacja
```

---

## Licencja

Prywatne użycie. Bazuje na strategii SkyTower-FX V.3.0.

---

## Changelog

### v4.0.0 (2025-01)
- Pełna automatyzacja
- Integracja LLM (Anthropic/OpenAI)
- Analiza COT z CFTC
- Analiza sentymentu retail
- REST API dla MT5
- Multi-source calendar aggregation
