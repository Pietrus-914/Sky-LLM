# SkyTower-AI Project Context

## Quick Summary
SkyTower-AI is an automated forex trading system that trades high-impact economic news events. It uses AI (Claude/GPT) or rule-based analysis combining COT data, retail sentiment (contrarian), and forecast analysis to decide trade direction.

## Project Location
`C:\Users\pietr\Documents\Sky tower\SkyTowerAI\`

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         SkyTower-AI System                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌─────────────┐    ┌─────────────────┐    ┌─────────────────────┐    │
│   │  Calendar   │───>│   Python        │───>│   MT5 Expert        │    │
│   │  Sources    │    │   Server        │    │   Advisor           │    │
│   └─────────────┘    │   (Flask)       │    │   (MQ5)             │    │
│                      │   port 5555     │    └─────────────────────┘    │
│   ┌─────────────┐    │                 │                               │
│   │  COT Data   │───>│  Decision       │    API Endpoints:            │
│   │  (CFTC)     │    │  Engine         │    - GET /api/signal         │
│   └─────────────┘    │                 │    - GET /api/events         │
│                      │  LLM or         │    - GET /api/decision       │
│   ┌─────────────┐    │  Rule-based     │    - GET /health             │
│   │  Sentiment  │───>│                 │                               │
│   │  Retail     │    └─────────────────┘                               │
│   └─────────────┘                                                       │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## File Structure

```
SkyTowerAI/
├── python/
│   ├── config.py              # All configuration (CRITICAL - read first!)
│   ├── server.py              # Flask REST API (~300 lines)
│   ├── calendar_fetcher.py    # Economic calendar (~600 lines)
│   ├── cot_analyzer.py        # COT data analysis (~400 lines)
│   ├── sentiment_analyzer.py  # Retail sentiment (~500 lines)
│   ├── llm_decision_engine.py # Decision logic (~450 lines)
│   ├── mt5_data_exporter.py   # [NEW] MT5 data export & validation
│   ├── signal_validator.py    # [NEW] Signal validation with spread check
│   ├── requirements.txt       # Python dependencies
│   └── .env.example           # Environment template
├── mt5/
│   └── SkyTowerAI_EA.mq5      # Expert Advisor (~600 lines, with spread logic)
├── tests/                     # [NEW] Test suite
│   ├── conftest.py            # Pytest fixtures
│   ├── unit/                  # Unit tests
│   ├── integration/           # Integration tests
│   ├── e2e/                   # End-to-end tests
│   └── fixtures/              # Sample test data
├── docker/                    # [PLANNED] Docker configs
├── config/                    # [PLANNED] Environment configs
├── CLAUDE.md                  # THIS FILE - project context
├── DOCUMENTATION.md           # Full documentation
├── DEPLOYMENT_PLAN.md         # [NEW] Deployment & testing plan
├── README.md                  # Quick start guide
├── pytest.ini                 # [NEW] Pytest configuration
├── start_server.bat           # Server launcher
└── test_system.bat            # System tests
```

## Key Configuration (config.py)

### Trading Events - What to Trade

**Tier 1 - Best Reactions (Always Trade):**
- Interest Rate Decision
- Official Cash Rate / Cash Rate
- Non-Farm Payrolls (NFP)
- CPI (Consumer Price Index)

**Tier 2 - Good Reactions (Trade with Caution):**
- Employment Change
- Unemployment Rate
- GDP
- Retail Sales

### Currency Pairs Ranking

| Priority | Currency | Best Pair | Spread on News |
|----------|----------|-----------|----------------|
| 1 | NZD | NZD/USD | 5-12 pips |
| 2 | CAD | USD/CAD | 3-8 pips |
| 3 | AUD | AUD/USD | 3-10 pips |
| 4 | USD | USD/CAD | 3-6 pips |
| 5 | GBP | GBP/USD | 3-10 pips |

### Spread Management (CRITICAL!)

Spreads are ALWAYS high during news events:
- Normal spread: 1-2 pips
- News spread: 5-15 pips (can be higher!)

**Lot reduction rules:**
- Spread < 3 pips → 100% lot
- Spread 3-6 pips → 80% lot
- Spread 6-10 pips → 60% lot
- Spread > 15 pips → DO NOT ENTER

### Risk Parameters
```python
max_risk_percent = 10.0     # Max 10% capital per trade
default_lot_percent = 80.0  # Use 80% of max lot
entry_seconds_before = 15   # Enter 15 sec before news
exit_minutes_after = 10     # Exit after 10 minutes
max_spread_pips = 10        # Max spread to enter
```

## Decision Logic

### Rule-based Scoring (when no LLM API key)
```
FORECAST ANALYSIS:
├─ Forecast > Previous  → +2 BULLISH
└─ Forecast < Previous  → +2 BEARISH

COT ANALYSIS (Institutional positions):
├─ Institutions LONG    → +3 BULLISH
└─ Institutions SHORT   → +3 BEARISH

SENTIMENT (CONTRARIAN - retail is usually wrong):
├─ Retail 70%+ LONG     → +2 BEARISH (trade against)
└─ Retail 70%+ SHORT    → +2 BULLISH (trade against)

FINAL DECISION:
├─ Bullish > Bearish + 2  → BUY
├─ Bearish > Bullish + 2  → SELL
└─ Otherwise              → SKIP (no trade)
```

### Confidence Thresholds
- < 50% → SKIP trade
- 50-60% → Trade with 60% lot
- 60-70% → Trade with 70% lot
- > 70% → Trade with 80% lot

## Docker Deployment (primary run mode)

The Python server runs in a Linux Docker container; MT5 + EA stay native on Windows.

```bash
cd SkyTowerAI
docker compose up -d --build     # build + start (port 5555 published to host)
docker compose logs -f           # watch logs
```

- `docker-compose.yml` sets `SKYTOWER_FORCE_DECISION=true` (TEST MODE — demo only!)
- `.env` (in `python/`) is injected via `env_file`, never baked into the image
- `python/logs/` is bind-mounted — `decision_history.jsonl` / `event_reactions.jsonl` survive rebuilds
- EA keeps `InpServerHost=127.0.0.1`, `InpServerPort=5555` (published port) — WebRequest allowlist needs `http://127.0.0.1:5555`
- Native fallback: `start_server.bat` (installs `requirements-windows.txt`, which adds the Windows-only `MetaTrader5` package)

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SKYTOWER_HOST` | `127.0.0.1` | Flask bind host (Docker sets `0.0.0.0`) |
| `SKYTOWER_PORT` | `5555` | Flask port |
| `SKYTOWER_FORCE_DECISION` | `false` | TEST MODE: LLM must pick BUY/SELL, SKIP disabled (all 3 origins). Decisions get `forced:true` in the audit log |
| `SKYTOWER_PRELOAD_SECONDS` | `150` | How early the updater analyzes an event (LLM needs 20-60s; entry is at T-15s) |
| `SKYTOWER_CHECK_INTERVAL` | `15` | Updater scan interval |
| `SKYTOWER_FAKE_EVENT_IN_SECONDS` | unset | Dry-run: inject one synthetic HIGH-impact USD event N seconds ahead to exercise the whole pipeline |

### Test-mode checklist (demo account)

1. `docker compose up -d` (FORCE_DECISION on; port bound to 127.0.0.1 only — the API has no auth)
2. MT5: EA on charts matching `DEFAULT_PAIRS` (NZDUSD, USDCAD, AUDUSD, GBPUSD) — `/api/signal` only fires for the decision's pair (broker suffixes like `.pro` are handled automatically)
3. EA inputs `InpPushMarketData=true`, `InpReportReactions=true` (defaults). `InpMinConfidence` can stay at 0.5 — signals with `forced:true` bypass the confidence gate automatically
4. Compiled `SkyTowerAI_EA.ex5` sits next to the source (metaeditor64 CLI: 0 errors)

## API Reference

**Base URL:** `http://127.0.0.1:5555`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server status check |
| `/api/signal` | GET | **Main endpoint for MT5** - returns trading signal (incl. `event_currency`, `forced`) |
| `/api/events` | GET | List upcoming events (?hours=168&currencies=NZD,CAD) |
| `/api/decision` | GET | Active decision (no eager analysis — the background updater owns decision making) |
| `/api/market-data` | POST | EA pushes OHLC (M1/M5/M15/H1) per pair → LLM market context + cross-pair picture |
| `/api/event-reaction` | POST | EA reports post-release price snapshots (T0/T+60s/T+300s) |
| `/api/event-reactions` | GET | Recorded reactions (?event=CPI&currency=USD) |
| `/api/event-paths` | GET | Server-measured post-event price paths, ALL monitored events (?limit=50) |
| `/api/regimes` | GET/POST | Auto-tracked monetary-policy regime per currency; POST {currency, regime} = manual override |
| `/api/cot/{currency}` | GET | COT data for currency |
| `/api/sentiment/{pair}` | GET | Sentiment for pair |

### Signal Response Example
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
  "event_name": "Interest Rate Decision"
}
```

## Workflow for Claude Code

### Token-Preserving Rules (from instruction.md)
1. **Read .md files FIRST** - config.py and CLAUDE.md contain all key info
2. **Use agents for deep analysis** - they work in separate context
3. **Localized reads only** - use `Read(file, offset=X, limit=Y)` for large files
4. **MQ5 files are large (500+ lines)** - avoid reading entire files

### Common Tasks

**Check system status:**
```bash
curl http://127.0.0.1:5555/health
```

**Start server:**
```bash
cd python && python server.py
# or use start_server.bat
```

**Test full system:**
```bash
test_system.bat
```

**Add new event type:**
Edit `config.py` → `TIER1_EVENTS` or `TIER2_EVENTS`

**Adjust spread limits:**
Edit `config.py` → `TYPICAL_NEWS_SPREADS` and `SPREAD_LOT_REDUCTION`

**Export MT5 data for analysis:**
```python
from mt5_data_exporter import export_for_analysis
import MetaTrader5 as mt5
df = export_for_analysis("NZDUSD", mt5.TIMEFRAME_M1, 5000, "data/nzdusd.csv")
```

**Validate signal before trade:**
```python
from signal_validator import validate_signal_for_server
result = validate_signal_for_server("NZDUSD", "BUY", 0.65)
if result["validation_passed"]:
    # Execute trade with result["recommended_lot_multiplier"]
```

## Data Sources

| Data | Source | Free? |
|------|--------|-------|
| Calendar | ForexFactory, TradingEconomics | Yes |
| COT | CFTC (publicreporting.cftc.gov) | Yes |
| Sentiment | Myfxbook, FXSSI | Yes |
| LLM | Anthropic Claude / OpenAI | API key required |

## Common Issues

| Issue | Solution |
|-------|----------|
| Server won't start | Check Python 3.10+, run `pip install -r requirements.txt` |
| MT5 can't connect | Enable WebRequest in MT5 Tools→Options→Expert Advisors |
| No events found | Check currency filter, try static calendar fallback |
| Always SKIP decisions | Check if COT/sentiment data available, may need LLM API key |
| High spread errors | Use major pairs (EURUSD, USDCAD), avoid exotics |

## Quick Reference - Event Schedule (UTC)

| Currency | Typical Time | Days |
|----------|-------------|------|
| NZD | 21:00-22:00 | Tue-Wed |
| AUD | 00:30-01:30 | Tue-Thu |
| GBP | 07:00-12:00 | Tue-Thu |
| EUR | 10:00-14:00 | Mon-Fri |
| CAD | 13:30-15:00 | Wed-Fri |
| USD | 13:30-19:00 | Mon-Fri |

---

**Remember:** This system trades NEWS events only. Spread is ALWAYS high on events - this is expected and managed by lot reduction logic. Never bypass spread checks!
