# SkyTower-AI - Stan Sesji (2026-01-23)

## Co działa

### Serwer Python (server.py)
- Flask server na `http://127.0.0.1:5555`
- Endpointy: `/health`, `/api/signal`, `/api/decision`, `/api/test-signal`, `/api/events`
- OpenRouter LLM z modelem `anthropic/claude-opus-4` - **DZIAŁA** (zweryfikowane bezpośrednim testem)
- COT data, sentiment analysis - działają
- Kalendarz ekonomiczny - ForexFactory czasem zwraca 429, wtedy fallback na static calendar

### EA (SkyTowerAI_EA.mq5)
- Multi-instance architecture
- Pobiera sygnały z serwera
- **NAPRAWIONE**: Spread calculation - zmienione z błędnej formuły na `spreadPoints / 10.0`
- Zone indicator integration
- Smart Exit z TP1/TP2/SL

## Co zostało zmodyfikowane w tej sesji

### 1. Spread calculation fix (EA linia ~870)
**Problem**: EA pokazywał "Spread EXTREME: 210.0 pips" dla GBPJPY
**Przyczyna**: Błędna formuła konwersji punktów na pipsy
**Fix**:
```mql5
// PRZED (błędne):
double spread = (double)SymbolInfoInteger(symbol, SYMBOL_SPREAD) * SymbolInfoDouble(symbol, SYMBOL_POINT) / 0.0001;

// PO (poprawne):
long spreadPoints = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
double spread = (double)spreadPoints / 10.0;
```

### 2. UTC time fix w test-signal (server.py linia ~953)
**Problem**: `time_until_event` pokazywał ~3600s zamiast ~20s
**Przyczyna**: `datetime.now()` (czas lokalny) zamiast `datetime.utcnow()`
**Fix**: Zmieniono na `datetime.utcnow()`

### 3. Dodano SL/TP w pipsach do LLM decision (llm_decision_engine.py)
**Zmiany**:
- Dataclass `TradingDecision` - dodano pola `stop_loss_pips` i `take_profit_pips`
- SYSTEM_PROMPT - zmieniono format z `stop_loss_percent` na `stop_loss_pips` i `take_profit_pips`
- Parsowanie odpowiedzi LLM - dodano ekstrakcję nowych pól

### 4. Dodano SL/TP pips do EA (SkyTowerAI_EA.mq5)
**Zmiany**:
- Globalne zmienne: `g_eventSLPips`, `g_eventTPPips`
- Parsowanie sygnału: `slPips = ExtractJsonDouble(result, "stop_loss_pips")`
- Logika SL/TP: Priorytet LLM > Zone indicator > Default 25 pips

### 5. Endpoint /api/signal - dodano pola (server.py linia ~846-850)
```python
"stop_loss_pips": getattr(next_decision, 'stop_loss_pips', 0),
"take_profit_pips": getattr(next_decision, 'take_profit_pips', 0),
```

## Obecne problemy

### 1. Pola stop_loss_pips/take_profit_pips nie pojawiają się w /api/signal
**Status**: Nierozwiązany
**Obserwacja**: Mimo że kod jest w pliku, odpowiedź JSON nie zawiera tych pól
**Możliwe przyczyny**:
- Cache Pythona (__pycache__)
- Serwer nie przeładowuje kodu poprawnie
- Problem z Flask jsonify

### 2. Loguru rotation error (PermissionError)
**Status**: Nieistotny dla działania
**Błąd**: `PermissionError: [WinError 32]` przy rotacji logów
**Rozwiązanie**: Usunąć stary plik `logs/server.log` lub zignorować

### 3. ForexFactory 429 Too Many Requests
**Status**: Nieistotny - system używa static calendar jako fallback

## Jak uruchomić

### Serwer Python
```bash
cd "C:/Users/pietr/Documents/Sky tower/SkyTowerAI/python"
./venv/Scripts/python.exe server.py
```

### Test LLM bezpośrednio (działa!)
```bash
cd "C:/Users/pietr/Documents/Sky tower/SkyTowerAI/python"
./venv/Scripts/python.exe -c "
from llm_decision_engine import LLMDecisionEngine
engine = LLMDecisionEngine()
print(f'Provider: {engine.provider}')
print(f'Model: {engine.model}')
decision = engine.get_next_trade_recommendation()
if decision:
    print(f'Direction: {decision.direction}')
    print(f'Pair: {decision.pair}')
    print(f'SL pips: {decision.stop_loss_pips}')
    print(f'TP pips: {decision.take_profit_pips}')
"
```

### Test signal
```bash
curl -X POST http://127.0.0.1:5555/api/test-signal -H "Content-Type: application/json" -d "{\"pair\":\"GBPJPY\",\"direction\":\"SELL\",\"seconds_until\":60}"
curl http://127.0.0.1:5555/api/signal
```

## Struktura kluczowych plików

```
SkyTowerAI/
├── python/
│   ├── server.py              # Flask server, endpointy API
│   ├── llm_decision_engine.py # LLM decision making, TradingDecision dataclass
│   ├── config.py              # Konfiguracja (OPENROUTER_API_KEY, LLM_CONFIG)
│   ├── calendar_fetcher.py    # Pobieranie eventów ekonomicznych
│   ├── cot_analyzer.py        # Analiza COT data
│   └── sentiment_analyzer.py  # Analiza sentymentu
├── mt5/
│   └── SkyTowerAI_EA.mq5      # Expert Advisor
```

## Co trzeba zrobić

1. **Debugować dlaczego stop_loss_pips nie jest w odpowiedzi** - może trzeba zrestartować kompletnie Python environment
2. **Zweryfikować że EA poprawnie parsuje nowe pola** - po naprawieniu serwera
3. **Przetestować pełny flow**: Serwer -> Signal -> EA -> Trade z SL/TP z LLM

## Komendy diagnostyczne

```bash
# Sprawdź health
curl http://127.0.0.1:5555/health

# Sprawdź sygnał
curl http://127.0.0.1:5555/api/signal

# Wymuś refresh decyzji (z LLM)
curl -X POST http://127.0.0.1:5555/api/decision/refresh

# Utwórz test signal
curl -X POST http://127.0.0.1:5555/api/test-signal -H "Content-Type: application/json" -d "{\"pair\":\"GBPJPY\",\"direction\":\"SELL\",\"seconds_until\":60,\"stop_loss_pips\":50,\"take_profit_pips\":75}"
```

## Notatki

- `stop_loss_percent` w oryginalnym kodzie to procent balansu (nie pipsy!) - używane do kalkulacji lot size
- EA ma priorytet: LLM SL/TP > Zone indicator > Default 25 pips
- Spread dla JPY pairs powinien być ~20-30 pips (210 points / 10 = 21 pips)
- LLM działa przez OpenRouter - zweryfikowane bezpośrednim wywołaniem
