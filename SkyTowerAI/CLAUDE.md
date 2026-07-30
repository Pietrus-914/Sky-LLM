# SkyTower-AI Project Context

## Quick Summary
SkyTower-AI is an automated forex trading system that trades high-impact economic news events. A Flask server analyzes each event (COT data, retail sentiment used contrarian, forecast vs previous, market context pushed by MT5) and decides BUY/SELL/SKIP via an LLM panel (OpenRouter) with a rule-based fallback. The MT5 Expert Advisor executes; **the server also manages the exit** (EA keeps only technical guardrails).

State: server **4.1.0**, **679 tests green** (30.07.2026), running natively on Windows. Active branch `gpt_review` (see `GPT_REVIEW_PLAN.md`). Docs wiki: `../wiki/index.md`.

## Project Location
`C:\Users\pietr\Documents\Sky tower\SkyTowerAI\`

## Architecture Overview

```
Calendar (FF feed = GMT!) ─┐
COT (CFTC)                 ├─> Flask server :5555 ───────> MT5 EA (Purple Trading)
Sentiment (contrarian)     │    - background updater          - polls /api/signal
Market data from EA ───────┘    - LLM entry panel             - executes, sizes lot from SL
                                - server-side EXIT engine     - pushes M1/M5/M15/H1
                                - dashboard + panel (risk!)   - reports position/reactions
```

All times UTC internally (server `utcnow`, EA `TimeGMT`); dashboard shows CET/CEST. MT5 bar times are BROKER time (offset inferred with a residual guard; candle labels in prompts corrected to UTC). 24/7 deployment machine (US) runs on port **5556**, deployed via ZIP, not git.

## File Structure (real, 27.07.2026)

```
SkyTowerAI/
├── python/
│   ├── config.py                 # All configuration (READ FIRST!)
│   ├── server.py                 # Flask REST API + background updater (~2100 lines)
│   ├── calendar_fetcher.py       # Calendar aggregation (per-key cache TTL)
│   ├── cot_analyzer.py           # CFTC COT analysis
│   ├── sentiment_analyzer.py     # Retail sentiment (contrarian)
│   ├── llm_decision_engine.py    # Entry decisions (OpenRouter panel / rule-based fallback)
│   ├── exit_decision_engine.py   # Server-side exit management
│   ├── position_manager.py       # Positions, daily limits, trade_history.jsonl
│   ├── position_store.py         # Position persistence/recovery (gpt_review Stage 1)
│   ├── decision_history.py       # Decision audit + track record (SHARED instance with engine)
│   ├── market_context.py         # EA-pushed OHLC -> LLM market context + cross-pair
│   ├── event_reaction_history.py # Post-event reaction records
│   ├── event_path_recorder.py    # Price paths of ALL monitored events (+ measure_path)
│   ├── regime_tracker.py         # Auto monetary-policy regimes per currency
│   ├── calibration.py            # Calibration ledger (per-model keys, spread-aware)
│   ├── episode_retrieval.py      # Learning loop F5: episodes
│   ├── reflections.py            # Learning loop F5: n=1 reflections
│   ├── playbook_distiller.py     # Learning loop F5: playbook distillation (operator-approved)
│   ├── zone_analyzer.py          # Liquidity pools / FVG / order blocks
│   ├── target_calculator.py      # TP/SL targets from zones
│   ├── trading_units.py          # Centralized pip/volume units (gpt_review Stage 2)
│   ├── timeutil.py               # UTC helpers (utcnow, to_naive_utc, utc_epoch)
│   ├── llm_util.py               # LLM plumbing
│   ├── mt5_data_exporter.py      # MT5 data export utility
│   ├── signal_validator.py       # Signal validation utility
│   ├── tools/                    # OFFLINE tools (build_learned_stats, fetch_ff_calendar,
│   │                             #   fetch_histdata, parse_ff_calendar, build_historical_paths)
│   │                             #   — the server does NOT import these
│   ├── knowledge/                # TRACKED: event_playbooks.json (curated, hot-reload),
│   │                             #   learned_stats.json (GENERATED — never edit by hand),
│   │                             #   historical_paths.jsonl.gz (44 679 paths 2021-26)
│   ├── templates/dashboard.html  # Dashboard incl. PL guide tab ("Przewodnik")
│   ├── logs/                     # GITIGNORED runtime: *.jsonl, server.log,
│   │                             #   currency_regimes.json, runtime_overrides.json,
│   │                             #   decision_context/<decision_id>.json (cap 2000)
│   └── .env                      # GITIGNORED: OPENROUTER_API_KEY + env overrides
├── mt5/
│   ├── SkyTowerAI_EA.mq5         # Expert Advisor (~1950 lines — use offset/limit reads!)
│   └── SkyTower_Zones.mq5        # Zone indicator
├── tests/                        # 679 tests: unit/ integration/ e2e/ (pytest)
├── START.bat                     # PRIMARY launcher: server (auto-restart) + MT5, idempotent
├── start_server.bat              # Server only (creates venv on first run)
├── start_server_24_7.bat         # 24/7 variant with watchdog loop
├── install_autostart.ps1         # Autostart after reboot (+ power settings)
├── RUNBOOK.md                    # Operations guide (PL) — authoritative for anything operational
├── CLAUDE.md                     # THIS FILE
├── DOCUMENTATION.md              # Technical documentation (PL)
├── GPT_REVIEW_PLAN.md            # Active plan for branch gpt_review
├── CALIBRATION_ANALYSIS.md       # Pre-live calibration study (26.07.2026)
├── research/                     # Playbook research, BTMM, calendar notes, screens workflow
└── docs/archive/                 # Historical January 2026 snapshots — do NOT trust
```

## Runtime & LLM

**Primary run mode: NATIVE Windows Python via `START.bat`** (since 10.07.2026). Docker compose files exist but are LEGACY/unused. Full operational procedures (start, verify, MT5 setup, EA recompile, 24/7 migration): `RUNBOOK.md`.

LLM access is via **OpenRouter** (`OPENROUTER_API_KEY` in `python/.env` — NEVER in code):

- **Entry**: mixed panel `SKYTOWER_ENSEMBLE_MODELS` — anchor fable-5 + gpt-5.6-sol-pro + gemini-3.1-pro-preview (~$0.21/event). Empty panel falls back to `SKYTOWER_ENTRY_MODEL` × `SKYTOWER_ENSEMBLE_K` samples (K>1: unanimity = trade, split = SKIP).
- **Exit**: `SKYTOWER_EXIT_MODEL` (code default sonnet-5; the 24/7 machine deliberately runs gemini-3.1-pro-preview).
- Operator budget: ~$0.5–1/event.

### Environment variables (python/.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SKYTOWER_HOST` / `SKYTOWER_PORT` | `127.0.0.1` / `5555` | Flask bind (24/7 machine: 5556) |
| `SKYTOWER_FORCE_DECISION` | `false` | TEST MODE: SKIP disabled, decisions get `forced:true` (filtered from learning). **DEMO ONLY** |
| `SKYTOWER_PRELOAD_SECONDS` | `150` | How early the updater analyzes an event (entry is at T-15s) |
| `SKYTOWER_CHECK_INTERVAL` | `15` | Updater scan interval (s) |
| `SKYTOWER_FAKE_EVENT_IN_SECONDS` | unset | Dry-run: inject synthetic event (reactions get `test:true`); REMOVE after test! |
| `SKYTOWER_EXTRA_EVENTS` | unset | Extra event names for the whitelist |
| `SKYTOWER_TRADE_ALL_EVENTS` | `false` | ON = trade every event ≥ MIN_IMPACT (whitelist ignored); panel switch, persisted |
| `SKYTOWER_ENTRY_MODEL` / `SKYTOWER_EXIT_MODEL` | see config | Model overrides |
| `SKYTOWER_ENSEMBLE_K` / `SKYTOWER_ENSEMBLE_MODELS` | `1` / panel | Ensemble config (cost = K × entry price) |

## Trading Rules & Risk

- **Event filter**: impact threshold (`MIN_IMPACT_LEVEL`) + name whitelist (TIER1/TIER2 + extras), or every event ≥ MIN_IMPACT when `TRADE_ALL_EVENTS` is ON. Speeches/testimony/press conferences (`NON_DATA_EVENT_MARKERS`) are NEVER traded in any mode. Shared predicate: `CalendarAggregator._event_is_tradeable`.
- **Pairs**: signal is served only to the EA asking about the decision's pair; EA runs on `DEFAULT_PAIRS` charts (NZDUSD, USDCAD, AUDUSD, GBPUSD; broker suffixes like `.pro` handled).
- **Risk lives ONLY in the dashboard panel** (Risk & Daily Limits → `/api/config/risk`, persisted in `logs/runtime_overrides.json`; precedence default < env < panel). Defaults: max loss **$100/trade**, **$300/day** (block until midnight UTC), **5 trades/day**, **30 min** max hold. Every `/api/signal` carries `max_loss_usd` — **the EA REJECTS signals without it** (old server = no trading, by design). Daily limits are enforced server-side; closed trades persist in `logs/trade_history.jsonl` and counters rebuild after restart.
- **Lot sizing (EA)**: from SL distance; budget = min(balance × `InpRiskPercent`%, `max_loss_usd`). Optional lot reductions (`InpUseConfidenceLot`, `InpUseSpreadLotReduction`) — operator prefers **false**. Spread ENTRY blocks always active (`InpMaxSpreadPips`; >15 pips never enter).
- **Risk limits are cross-checked**: `max_loss_usd` may never exceed `max_daily_loss_usd` (`config.risk_limit_conflicts`) — the panel rejects such a write with 400, import clamps it, and startup logs the EFFECTIVE limits. Panel values outrank `.env` permanently, so that log line is the only reliable answer to "what budget is actually armed?".
- **Exit is server-owned**: `exit_decision_engine` returns MODIFY_SL / PARTIAL_CLOSE / CLOSE on `/api/position/report`. The LLM call runs on a **background worker** and its command is **queued in `pending_command` for the next report** (5-15s later) — the EA's POST timeout (10s) is shorter than the model's (30s), so answering in the triggering response used to lose commands outright. The rule-based fallback has no network client and still answers inline. Delivery is **confirm-then-retire**: the served command is kept until a broker report shows its effect (volume drop / matching SL / further reports = a CLOSE did not execute), re-sent once if it is missing, and `PARTIAL_CLOSE` is **never** re-sent (`REDELIVERABLE_ACTIONS`) because a duplicate would close a second slice. `partial_closed` and `sl_moved_to_be` are derived from broker reports, never from having sent a command. EA-side guardrails only: `InpMaxHoldMinutes` (default 30) + emergency spread. `InpUseZoneTargets` fetches `/api/targets` at open. On external close (SL/TP/manual) the EA computes realized P/L from deal history (`profit_source: history`).
- Spread is ALWAYS high on events — expected; never bypass spread checks. The **emergency-spread exit needs confirmation** (server: 2 consecutive reports, EA: 3 consecutive ticks; ≥2× the threshold exits at once) because entry is capped just under the same value.

## Decision Logic

**Primary: LLM panel** (reasoning-FIRST response schema; numeric fields clamped: conf 0–1, lot ≤ 85, exit 5–15 min, SL 25–80, TP 30–120 pips). Prompt context includes: COT, contrarian sentiment, forecast vs previous, M1 tail (20 candles) + cross-pair picture, EVENT PLAYBOOK (`knowledge/event_playbooks.json`), learned stats, calibration line (n≥50 **for the current model + prompt_version only** — it speaks in the second person, so it is never computed from another configuration's history), currency regimes, liquidity-pool stop clusters, RECENT TRADE OUTCOMES (realized P/L; `forced` and FAKE TEST rows filtered out).

**Panel degradation is explicit**: a vote that fails or returns unparseable content is logged with the model name and reason, retried once (if >90s to the release), and — if still missing — marks the decision `ensemble.degraded` with `PANEL DEGRADED n/k` in the reasoning. Quorum is 2 valid votes (`ENSEMBLE_MIN_QUORUM`); in force mode a lone survivor trades but is labelled `NO QUORUM`, never "majority". The panel has a wall-clock deadline (`T-20s`, floor 10s) so one hung vendor cannot push the decision past the release.

**Fallback: rule-based scoring** (no API key): forecast>previous +2, COT ±3, retail ≥70% contrarian ±2; BUY/SELL needs a 2-point margin, else SKIP.

EA confidence gate: `InpMinConfidence` (0.5); `forced:true` signals bypass it.

## Learning Loop (F0–F5, all deployed)

`decision_id` (uuid) links decision → signal → trade → reaction; full prompt+response in `logs/decision_context/<id>.json`. Server measures post-event price paths of ALL monitored events (`logs/event_paths.jsonl`, `GET /api/event-paths`). Calibration ledger (`GET /api/calibration`, per-model keys, spread-aware). RegimeTracker auto-derives regimes from rate decisions (LLM adjudicates only ambiguous holds; EUR/JPY/CHF have no charts = seed/manual only). Stats regeneration: `python tools/build_learned_stats.py` (hot-reload, no restart). Details: `../wiki/pages/learning-loop.md` + RUNBOOK section "Learning loop".

## API Reference

**Base URL:** `http://127.0.0.1:5555`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server status |
| `/api/signal` | GET | **Main MT5 endpoint** — signal incl. `max_loss_usd` (required by EA), `decision_id`, `event_currency`, `forced` |
| `/api/events` | GET | Upcoming events (`?hours=168&currencies=NZD,CAD`); past events filtered out |
| `/api/decision` | GET | Active decision (background updater OWNS decision making — no eager analysis) |
| `/api/position/report` | POST | EA position heartbeat → server exit commands (MODIFY_SL/PARTIAL_CLOSE/CLOSE) |
| `/api/position/status` | GET | Live position + daily limits + recent_trades(10) |
| `/api/market-data` | POST | EA pushes OHLC (M1/M5/M15/H1) per pair |
| `/api/event-reaction` | POST | EA post-release snapshots (T0/T+60s/T+300s) |
| `/api/event-reactions` | GET | Recorded reactions (`?event=CPI&currency=USD`) |
| `/api/event-paths` | GET | Server-measured price paths, ALL monitored events (`?limit=50`) |
| `/api/regimes` | GET/POST | Auto regimes per currency; POST = manual override |
| `/api/calibration` | GET | Calibration ledger |
| `/api/config/risk` | GET/POST | Panel-owned risk limits (persisted) |
| `/api/datasources/status` | GET | Health of calendar/COT/sentiment sources |
| `/api/targets` | POST | Zone-based TP/SL targets (EA at position open) |
| `/api/trade-executed` | POST | Fallback trade notification (also counts toward daily limit) |
| `/api/cot/{currency}` / `/api/sentiment/{pair}` | GET | Raw analysis data |

### Signal Response Example
```json
{
  "signal": true,
  "direction": "BUY",
  "pair": "NZDUSD",
  "lot_percent": 70,
  "confidence": 0.65,
  "max_loss_usd": 100.0,
  "decision_id": "a1b2c3d4-...",
  "forced": false,
  "event_currency": "NZD",
  "entry_seconds_before": 15,
  "exit_minutes": 10,
  "time_until_event": 3600,
  "event_name": "Interest Rate Decision"
}
```

## Workflow for Claude Code

1. **Read .md files FIRST** — this file, `config.py`, `RUNBOOK.md`; docs questions → `../wiki/index.md`.
2. **MQ5 and server.py are large** — use `Read(file, offset=X, limit=Y)` or agents; never read whole files repeatedly.
3. **EA recompile**: metaeditor64 is a GUI app — ALWAYS delete the old log, use `Start-Process -Wait`, then check the `.ex5` date, or you'll read last week's "0 errors" (exact commands in RUNBOOK).
4. After significant architecture/behavior changes: update the wiki + append an INGEST entry to `../wiki/log.md`.

**Common tasks:**
```powershell
curl http://127.0.0.1:5555/health          # status
# START.bat = server + MT5; start_server.bat = server only
python\venv\Scripts\python.exe -m pytest -q   # run tests (679, ~18 s)
```
Add tradeable event names: `config.py` → TIER1/TIER2 or `SKYTOWER_EXTRA_EVENTS`.

## Data Sources (known state)

| Data | Source | Status |
|------|--------|--------|
| Calendar | ForexFactory feed (**GMT** — verify timezone of any new source!) | Works; occasional 429 — do NOT hammer the feed |
| COT | CFTC | Frequently missing data |
| Sentiment | Myfxbook (403), FXSSI (0 pairs) | Unreliable — LLM compensates with EA market context |
| LLM | OpenRouter | `OPENROUTER_API_KEY` in `python/.env` |

## Common Issues

| Issue | Solution |
|-------|----------|
| Server won't start | Python 3.10+; `start_server.bat` creates venv and installs deps |
| MT5 can't connect (4014) | MT5 WebRequest allowlist: `http://127.0.0.1:5555` |
| EA ignores signals | Old server without `max_loss_usd` in signal → update server (EA rejects by design) |
| Always SKIP | Check `OPENROUTER_API_KEY`, data source health (`/api/datasources/status`) |
| Nightly trade "missing" from Trades Today | Daily counters reset at midnight UTC (02:00 PL summer); see Recent Trades |
| High spread errors | Expected on news; check `InpMaxSpreadPips`, stick to DEFAULT_PAIRS |

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

**Remember:** This system trades NEWS events only. Spread is ALWAYS high on events — expected and managed. Never bypass spread checks. Risk config lives in the dashboard panel, not in EA inputs. When EA/server parameters change, update the dashboard "Przewodnik" tab too.
