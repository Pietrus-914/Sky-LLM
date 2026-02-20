# SkyTower-AI - Plan Wdrożenia i Testów

**Data:** 2026-01-20
**Status:** PLAN (do zatwierdzenia)

---

## Spis treści

1. [Przegląd architektury docelowej](#1-przegląd-architektury-docelowej)
2. [Opcje hostingu](#2-opcje-hostingu)
3. [Pakowanie aplikacji](#3-pakowanie-aplikacji)
4. [Plan testów](#4-plan-testów)
5. [Konfiguracja środowisk](#5-konfiguracja-środowisk)
6. [Monitoring i alerting](#6-monitoring-i-alerting)
7. [Harmonogram wdrożenia](#7-harmonogram-wdrożenia)
8. [Checklist przed produkcją](#8-checklist-przed-produkcją)

---

## 1. Przegląd architektury docelowej

### Obecna architektura (lokalna)
```
┌─────────────────────────────────────────────────────────────────┐
│                     LOKALNY KOMPUTER                             │
│                                                                  │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐  │
│  │   Python     │◄────►│   MT5        │◄────►│   Broker     │  │
│  │   Server     │      │   Terminal   │      │   (Purple)   │  │
│  │   :5555      │      │   EA         │      │              │  │
│  └──────────────┘      └──────────────┘      └──────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Docelowa architektura (zdalna)
```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CLOUD/VPS                                      │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     Docker Compose Stack                          │   │
│  │                                                                   │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │   │
│  │  │   Python     │  │    Redis     │  │   Nginx      │           │   │
│  │  │   Server     │  │    Cache     │  │   Proxy      │◄──────────┼───┤
│  │  │   (Gunicorn) │  │              │  │   + SSL      │   HTTPS   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘           │   │
│  │         │                  │                                      │   │
│  │         └──────────────────┴──────────────────────────────────┐  │   │
│  │                                                                │  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │  │   │
│  │  │  PostgreSQL  │  │   Grafana    │  │  Prometheus  │        │  │   │
│  │  │  (history)   │  │  (dashboard) │  │  (metrics)   │        │  │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘        │  │   │
│  │                                                                │  │   │
│  └────────────────────────────────────────────────────────────────┘  │   │
│                                                                       │   │
└───────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                      LOKALNY KOMPUTER (lub VPS z Wine)                   │
│                                                                          │
│  ┌──────────────────┐                          ┌──────────────────┐     │
│  │   MT5 Terminal   │◄────── WebRequest ──────►│   Cloud Server   │     │
│  │   + EA           │         HTTPS            │   API            │     │
│  └──────────────────┘                          └──────────────────┘     │
│           │                                                              │
│           ▼                                                              │
│  ┌──────────────────┐                                                   │
│  │   Broker         │                                                   │
│  │   (Purple)       │                                                   │
│  └──────────────────┘                                                   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Kluczowe decyzje architektoniczne

| Aspekt | Decyzja | Uzasadnienie |
|--------|---------|--------------|
| MT5 Terminal | Lokalnie lub VPS z Wine | MT5 wymaga Windows/Wine, nie działa w kontenerze |
| Python Server | Docker na VPS | Łatwe skalowanie, izolacja |
| Baza danych | PostgreSQL | Lepsza od SQLite dla produkcji |
| Cache | Redis | Szybki cache dla COT/sentiment |
| Reverse Proxy | Nginx + Let's Encrypt | SSL dla WebRequest z MT5 |

---

## 2. Opcje hostingu

### Opcja A: VPS z Docker (Rekomendowana)

**Dostawcy:**
| Dostawca | Spec | Cena/msc | Uwagi |
|----------|------|----------|-------|
| Hetzner | 2 vCPU, 4GB RAM | ~€5-7 | Dobry stosunek cena/jakość |
| DigitalOcean | 2 vCPU, 4GB RAM | ~$24 | Prosty panel |
| Vultr | 2 vCPU, 4GB RAM | ~$24 | Dobre lokalizacje EU |
| OVH | 2 vCPU, 4GB RAM | ~€6 | Tanie, EU data centers |

**Wymagania minimalne:**
- 2 vCPU
- 4 GB RAM
- 40 GB SSD
- Ubuntu 22.04 LTS

### Opcja B: Serverless (AWS Lambda / Google Cloud Functions)

**Zalety:**
- Płacisz tylko za użycie
- Auto-skalowanie
- Brak zarządzania serwerem

**Wady:**
- Cold start (opóźnienie 1-3s)
- Ograniczenia czasowe (15 min max)
- Bardziej skomplikowane wdrożenie

**Nie rekomendowane** dla SkyTower - cold start może spowodować opóźnienie sygnału.

### Opcja C: VPS z MT5 (Wine)

Jeśli nie chcesz uruchamiać MT5 lokalnie:
- VPS Windows (~$15-30/msc)
- Lub VPS Linux z Wine + MT5

**Uwaga:** Wymaga RDP/VNC do konfiguracji MT5.

---

## 3. Pakowanie aplikacji

### 3.1 Struktura Docker

```
SkyTowerAI/
├── docker/
│   ├── Dockerfile              # Główny obraz Python
│   ├── Dockerfile.dev          # Obraz developerski
│   ├── docker-compose.yml      # Produkcja
│   ├── docker-compose.dev.yml  # Development
│   └── docker-compose.test.yml # Testy
├── nginx/
│   ├── nginx.conf              # Konfiguracja Nginx
│   └── ssl/                    # Certyfikaty (gitignore)
├── scripts/
│   ├── deploy.sh               # Skrypt wdrożenia
│   ├── backup.sh               # Backup bazy
│   └── health_check.sh         # Sprawdzenie zdrowia
└── ...
```

### 3.2 Dockerfile

```dockerfile
# Dockerfile (do utworzenia)
FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gunicorn psycopg2-binary

# Application code
COPY python/ ./python/
COPY config/ ./config/

# Environment
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5555/health || exit 1

# Run
EXPOSE 5555
CMD ["gunicorn", "--bind", "0.0.0.0:5555", "--workers", "2", "python.server:app"]
```

### 3.3 Docker Compose (Produkcja)

```yaml
# docker-compose.yml (do utworzenia)
version: '3.8'

services:
  skytower-api:
    build:
      context: .
      dockerfile: docker/Dockerfile
    container_name: skytower-api
    restart: unless-stopped
    environment:
      - FLASK_ENV=production
      - DATABASE_URL=postgresql://skytower:${DB_PASSWORD}@postgres:5432/skytower
      - REDIS_URL=redis://redis:6379/0
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    depends_on:
      - postgres
      - redis
    networks:
      - skytower-network

  postgres:
    image: postgres:15-alpine
    container_name: skytower-postgres
    restart: unless-stopped
    environment:
      - POSTGRES_DB=skytower
      - POSTGRES_USER=skytower
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - skytower-network

  redis:
    image: redis:7-alpine
    container_name: skytower-redis
    restart: unless-stopped
    volumes:
      - redis_data:/data
    networks:
      - skytower-network

  nginx:
    image: nginx:alpine
    container_name: skytower-nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/ssl:/etc/nginx/ssl:ro
      - certbot_data:/var/www/certbot:ro
    depends_on:
      - skytower-api
    networks:
      - skytower-network

networks:
  skytower-network:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  certbot_data:
```

### 3.4 Alternatywa: Python Package (pip install)

Dla prostszego wdrożenia można utworzyć pakiet pip:

```
SkyTowerAI/
├── setup.py
├── pyproject.toml
├── skytower/
│   ├── __init__.py
│   ├── server.py
│   ├── config.py
│   └── ...
└── ...
```

Wtedy instalacja:
```bash
pip install skytower-ai
skytower-server --port 5555 --config /path/to/config.yaml
```

---

## 4. Plan testów

### 4.1 Poziomy testów

```
┌─────────────────────────────────────────────────────────────────┐
│                        PIRAMIDA TESTÓW                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│                         ┌─────────┐                             │
│                         │   E2E   │  ← 10% (Critical paths)     │
│                         └────┬────┘                             │
│                    ┌─────────┴─────────┐                        │
│                    │   Integration     │  ← 20% (API, DB)       │
│                    └─────────┬─────────┘                        │
│           ┌──────────────────┴──────────────────┐               │
│           │            Unit Tests               │  ← 70%        │
│           └─────────────────────────────────────┘               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Struktura testów

```
SkyTowerAI/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Pytest fixtures
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_calendar_fetcher.py
│   │   ├── test_cot_analyzer.py
│   │   ├── test_sentiment_analyzer.py
│   │   ├── test_decision_engine.py
│   │   ├── test_signal_validator.py
│   │   └── test_mt5_data_exporter.py
│   ├── integration/
│   │   ├── test_api_endpoints.py
│   │   ├── test_database.py
│   │   └── test_external_apis.py
│   ├── e2e/
│   │   ├── test_full_signal_flow.py
│   │   └── test_mt5_communication.py
│   └── fixtures/
│       ├── sample_events.json
│       ├── sample_cot_data.json
│       └── sample_sentiment.json
├── pytest.ini
└── .coveragerc
```

### 4.3 Przykładowe testy jednostkowe

```python
# tests/unit/test_decision_engine.py (do utworzenia)

import pytest
from unittest.mock import Mock, patch
from python.llm_decision_engine import LLMDecisionEngine, TradingDecision

class TestDecisionEngine:
    """Test suite for LLM Decision Engine"""

    @pytest.fixture
    def engine(self):
        """Create engine instance without API keys"""
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            return LLMDecisionEngine()

    def test_rule_based_bullish_signal(self, engine):
        """Test bullish signal when COT and forecast align"""
        # Given: COT bullish, forecast improvement
        cot_data = {"signal": "BULLISH", "confidence": 0.7}
        sentiment_data = {"retail_long_percent": 45}  # Neutral
        event_data = {"forecast": 4.25, "previous": 4.0}

        # When
        decision = engine._rule_based_decision(
            event_data, cot_data, sentiment_data, "NZD"
        )

        # Then
        assert decision.direction == "BUY"
        assert decision.confidence >= 0.5

    def test_rule_based_skip_on_conflict(self, engine):
        """Test SKIP when signals conflict"""
        # Given: COT bullish but retail also bullish (contrarian = bearish)
        cot_data = {"signal": "BULLISH", "confidence": 0.6}
        sentiment_data = {"retail_long_percent": 75}  # Contrarian bearish
        event_data = {"forecast": 4.0, "previous": 4.0}  # No change

        # When
        decision = engine._rule_based_decision(
            event_data, cot_data, sentiment_data, "NZD"
        )

        # Then
        assert decision.direction == "SKIP"

    def test_spread_lot_reduction(self, engine):
        """Test lot reduction based on spread"""
        # Given
        spreads_and_expected = [
            (2.0, 1.0),   # Low spread = full lot
            (4.5, 0.8),   # Medium spread = 80%
            (8.0, 0.6),   # High spread = 60%
            (20.0, 0.0),  # Extreme = no trade
        ]

        for spread, expected_multiplier in spreads_and_expected:
            # When
            multiplier = engine._get_spread_multiplier(spread)

            # Then
            assert multiplier == expected_multiplier, \
                f"Spread {spread} should give {expected_multiplier}, got {multiplier}"
```

### 4.4 Testy integracyjne API

```python
# tests/integration/test_api_endpoints.py (do utworzenia)

import pytest
from flask.testing import FlaskClient
from python.server import app

class TestAPIEndpoints:
    """Integration tests for Flask API"""

    @pytest.fixture
    def client(self):
        app.config['TESTING'] = True
        with app.test_client() as client:
            yield client

    def test_health_endpoint(self, client: FlaskClient):
        """Test /health returns OK"""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'

    def test_events_endpoint(self, client: FlaskClient):
        """Test /api/events returns list"""
        response = client.get('/api/events?hours=24')
        assert response.status_code == 200
        data = response.get_json()
        assert 'events' in data or 'status' in data

    def test_signal_endpoint_no_event(self, client: FlaskClient):
        """Test /api/signal when no event"""
        response = client.get('/api/signal')
        assert response.status_code == 200
        data = response.get_json()
        # Should return signal: false or signal data
        assert 'signal' in data

    def test_decision_endpoint(self, client: FlaskClient):
        """Test /api/decision returns analysis"""
        response = client.get('/api/decision')
        assert response.status_code == 200
```

### 4.5 Testy E2E

```python
# tests/e2e/test_full_signal_flow.py (do utworzenia)

import pytest
import requests
import time

@pytest.mark.e2e
class TestFullSignalFlow:
    """End-to-end tests for complete signal flow"""

    BASE_URL = "http://localhost:5555"

    def test_complete_signal_flow(self):
        """Test: Event → Analysis → Signal → Validation"""
        # 1. Check server health
        health = requests.get(f"{self.BASE_URL}/health")
        assert health.status_code == 200

        # 2. Get upcoming events
        events = requests.get(f"{self.BASE_URL}/api/events?hours=168")
        assert events.status_code == 200
        events_data = events.json()

        # 3. If there's an event, get decision
        if events_data.get('events'):
            decision = requests.get(f"{self.BASE_URL}/api/decision")
            assert decision.status_code == 200
            decision_data = decision.json()

            # 4. Verify decision structure
            if decision_data.get('decision'):
                assert 'direction' in decision_data['decision']
                assert 'confidence' in decision_data['decision']
                assert 'pair' in decision_data['decision']

    def test_signal_response_time(self):
        """Test: Signal response under 500ms"""
        start = time.time()
        response = requests.get(f"{self.BASE_URL}/api/signal")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert elapsed < 0.5, f"Signal took {elapsed:.2f}s, expected <0.5s"
```

### 4.6 Konfiguracja pytest

```ini
# pytest.ini (do utworzenia)
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short --strict-markers
markers =
    unit: Unit tests (fast, isolated)
    integration: Integration tests (may need external services)
    e2e: End-to-end tests (full system)
    slow: Slow tests (>1s)

filterwarnings =
    ignore::DeprecationWarning
```

```ini
# .coveragerc (do utworzenia)
[run]
source = python
omit =
    python/venv/*
    tests/*
    */__pycache__/*

[report]
exclude_lines =
    pragma: no cover
    def __repr__
    raise NotImplementedError
    if __name__ == .__main__.:

[html]
directory = coverage_html
```

---

## 5. Konfiguracja środowisk

### 5.1 Środowiska

| Środowisko | Cel | Baza | API Keys | MT5 |
|------------|-----|------|----------|-----|
| **local** | Development | SQLite | Opcjonalne | Opcjonalny |
| **test** | CI/CD, testy | SQLite/PostgreSQL | Mock | Mock |
| **staging** | Pre-produkcja | PostgreSQL | Prawdziwe | Demo account |
| **production** | Produkcja | PostgreSQL | Prawdziwe | Live account |

### 5.2 Pliki konfiguracyjne

```
SkyTowerAI/
├── config/
│   ├── base.yaml           # Wspólna konfiguracja
│   ├── local.yaml          # Development
│   ├── test.yaml           # Testy
│   ├── staging.yaml        # Staging
│   └── production.yaml     # Produkcja
└── .env.example            # Szablon zmiennych
```

### 5.3 Przykład config/base.yaml

```yaml
# config/base.yaml (do utworzenia)
server:
  host: "0.0.0.0"
  port: 5555
  workers: 2

trading:
  max_risk_percent: 10.0
  default_lot_percent: 80.0
  entry_seconds_before: 15
  exit_minutes_after: 10
  max_spread_pips: 10

events:
  tier1:
    - "Interest Rate Decision"
    - "Non-Farm Payrolls"
    - "CPI"
  tier2:
    - "Employment Change"
    - "GDP"
    - "Retail Sales"

currencies:
  primary: ["NZD", "CAD", "AUD", "USD", "GBP"]

logging:
  level: INFO
  format: "{time} | {level} | {message}"
```

### 5.4 Przykład config/test.yaml

```yaml
# config/test.yaml (do utworzenia)
inherit: base

server:
  host: "127.0.0.1"
  debug: true

database:
  url: "sqlite:///:memory:"

cache:
  type: "memory"

mocks:
  calendar_api: true
  cot_api: true
  sentiment_api: true
  llm_api: true

trading:
  # Bezpieczne wartości dla testów
  max_risk_percent: 1.0
  max_spread_pips: 5.0
```

---

## 6. Monitoring i alerting

### 6.1 Stack monitoringu

```
┌─────────────────────────────────────────────────────────────────┐
│                     MONITORING STACK                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Prometheus  │─►│   Grafana    │  │   Loki       │          │
│  │  (metrics)   │  │  (dashboard) │  │   (logs)     │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│         │                                    │                   │
│         ▼                                    ▼                   │
│  ┌──────────────┐                  ┌──────────────┐             │
│  │  AlertManager│                  │   Promtail   │             │
│  │  (alerts)    │                  │  (log ship)  │             │
│  └──────────────┘                  └──────────────┘             │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────┐                                               │
│  │   Discord/   │                                               │
│  │   Telegram   │                                               │
│  └──────────────┘                                               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 Metryki do monitorowania

| Kategoria | Metryka | Alert threshold |
|-----------|---------|-----------------|
| **API** | Response time | > 500ms |
| **API** | Error rate | > 1% |
| **API** | Requests/min | < 1 (no polling) |
| **Trading** | Signals/day | > 5 (unusual) |
| **Trading** | Spread average | > 10 pips |
| **Trading** | Confidence average | < 50% |
| **System** | CPU usage | > 80% |
| **System** | Memory usage | > 85% |
| **System** | Disk usage | > 90% |
| **External** | Calendar API errors | > 3/hour |
| **External** | COT API errors | > 3/hour |

### 6.3 Alerty krytyczne

```yaml
# alerting/rules.yml (do utworzenia)
groups:
  - name: skytower-critical
    rules:
      - alert: ServerDown
        expr: up{job="skytower"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "SkyTower server is down"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.01
        for: 5m
        labels:
          severity: warning

      - alert: SignalMissed
        expr: skytower_signals_missed_total > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Trading signal was missed"
```

---

## 7. Harmonogram wdrożenia

### Faza 1: Przygotowanie (Tydzień 1)

| Dzień | Zadanie | Rezultat |
|-------|---------|----------|
| 1 | Utworzenie struktury testów | `tests/` folder |
| 2 | Napisanie testów jednostkowych | 70% coverage |
| 3 | Napisanie testów integracyjnych | API tests |
| 4 | Konfiguracja CI/CD (GitHub Actions) | `.github/workflows/` |
| 5 | Utworzenie Dockerfile i docker-compose | `docker/` folder |

### Faza 2: Staging (Tydzień 2)

| Dzień | Zadanie | Rezultat |
|-------|---------|----------|
| 1 | Provisioning VPS | Serwer gotowy |
| 2 | Deployment staging | Staging działa |
| 3 | Konfiguracja SSL/HTTPS | Certyfikat Let's Encrypt |
| 4 | Testy MT5 ↔ Cloud | Połączenie działa |
| 5 | Monitoring setup | Grafana dashboard |

### Faza 3: Testowanie na demo (Tydzień 3-4)

| Tydzień | Zadanie | Rezultat |
|---------|---------|----------|
| 3 | Testy na MT5 demo account | Min. 10 sygnałów |
| 4 | Analiza wyników, poprawki | Raport testowy |

### Faza 4: Produkcja (Tydzień 5)

| Dzień | Zadanie | Rezultat |
|-------|---------|----------|
| 1 | Deployment produkcyjny | Produkcja działa |
| 2 | Przełączenie MT5 na live | Live trading |
| 3 | Monitoring 24h | Brak alertów |
| 4-5 | Obserwacja | System stabilny |

---

## 8. Checklist przed produkcją

### Bezpieczeństwo
- [ ] API keys w secrets manager (nie w kodzie)
- [ ] HTTPS enabled (certyfikat SSL)
- [ ] Firewall skonfigurowany (tylko porty 80, 443)
- [ ] Rate limiting włączony
- [ ] Input validation dla wszystkich endpointów

### Testy
- [ ] Unit tests przechodzą (>80% coverage)
- [ ] Integration tests przechodzą
- [ ] E2E tests przechodzą
- [ ] Load test (100 req/min przez 10 min)
- [ ] Testy na MT5 demo (min. 7 dni)

### Infrastruktura
- [ ] Docker images zbudowane i przetestowane
- [ ] Backup bazy danych skonfigurowany
- [ ] Log rotation skonfigurowany
- [ ] Health checks działają
- [ ] Auto-restart po crashu

### Monitoring
- [ ] Grafana dashboard gotowy
- [ ] Alerty skonfigurowane
- [ ] Discord/Telegram notifications
- [ ] Log aggregation (Loki/ELK)

### Dokumentacja
- [ ] README zaktualizowany
- [ ] API documentation (Swagger/OpenAPI)
- [ ] Runbook dla on-call
- [ ] Disaster recovery plan

### Trading
- [ ] Limity dzienne ustawione (max 3 trades)
- [ ] Max risk % zweryfikowany
- [ ] Spread limits przetestowane
- [ ] Kill switch gotowy (wyłącz trading)

---

## Następne kroki (do zatwierdzenia)

1. **Czy potwierdzasz architekturę Docker na VPS?**
2. **Który dostawca VPS preferujesz?**
3. **Czy chcesz rozpocząć od Fazy 1 (testy)?**
4. **Czy potrzebujesz wsparcia przy MT5 na VPS (Wine)?**

---

*Plan przygotowany: 2026-01-20*
*Status: Do zatwierdzenia*
