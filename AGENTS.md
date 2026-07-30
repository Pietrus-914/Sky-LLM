# Sky Tower Project - Codex Context

## Overview
This is the root directory for the Sky Tower forex trading project. The main code is in the `SkyTowerAI/` subdirectory.

## Quick Navigation

```
Sky tower/
├── SkyTowerAI/           # Main project directory
│   ├── CLAUDE.md         # Detailed project context (shared by all agents)
│   ├── RUNBOOK.md        # Operations guide (PL) — start/verify/migrate
│   ├── DOCUMENTATION.md  # Full technical documentation
│   ├── config.py         # Configuration (READ FIRST!)
│   ├── python/           # Python backend (Flask server)
│   └── mt5/              # MetaTrader 5 Expert Advisor
├── .agents/
│   └── skills/           # Skills for Codex (read SKILL.md manually when needed)
│       ├── mql5-indicator-patterns/  # Indicator development patterns
│       ├── log-reader/               # MT5 log analysis
│       ├── article-extractor/        # mql5.com article extraction
│       └── python-workspace/         # MQL5-Python integration
├── wiki/                 # LLM-maintained documentation wiki (start: wiki/index.md)
├── instruction.md        # Token-preserving workflow rules
├── SkyTower-FX_V.3.0.pdf # Original strategy PDF (21MB - too large to read)
└── AGENTS.md             # THIS FILE
```

## Documentation Wiki (wiki/)

`wiki/` is an LLM-maintained knowledge wiki (Karpathy's LLM-wiki pattern:
sources stay immutable, the LLM writes the wiki, the human reads it). Shared by
all agents (Claude Code and Codex alike).

- Questions about how the system works or where a doc lives → start at `wiki/index.md`.
- Conventions and the INGEST/QUERY/LINT workflows → `wiki/schema.md`.
- After any significant architecture/behavior change → update the affected pages
  and append an INGEST entry to `wiki/log.md` (part of "done", like tests).
- `wiki/pages/documentation-map.md` maps every doc file, its freshness, and
  which file is authoritative for what.

## Common Operations

```
curl http://127.0.0.1:5555/health        - Check server health
curl http://127.0.0.1:5555/api/events    - List upcoming events
curl http://127.0.0.1:5555/api/decision  - Current decision
START.bat                                - Start server + MT5 (in SkyTowerAI/)
python\venv\Scripts\python.exe -m pytest -q   - Run tests (in SkyTowerAI/, 691 tests)
```

(The `/sky_tower` slash command exists only for Claude Code in `.claude/`,
which is local and gitignored — Codex uses the direct commands above.)

## What This System Does

SkyTower-AI is an **automated forex news trading system** that:
1. Monitors economic calendar for HIGH-impact events
2. Analyzes COT data (institutional positions)
3. Checks retail sentiment (used contrarian - trade against crowd)
4. Compares forecast vs previous values
5. Uses an LLM panel via OpenRouter (or rule-based fallback) to decide BUY/SELL/SKIP
6. Sends signals to MT5 Expert Advisor via REST API

## Key Files to Read

**Always read in this order:**
1. `SkyTowerAI/CLAUDE.md` - Full context and quick reference (shared by all agents)
2. `SkyTowerAI/python/config.py` - All configuration
3. `instruction.md` - Token-preserving workflow
4. `wiki/index.md` - documentation wiki (for docs/architecture questions)

## Important Rules

1. **Spread is ALWAYS high on events** - this is expected, managed by lot reduction
2. **MQ5 files are large** - use agents or partial reads
3. **Never bypass spread checks** - they protect from slippage losses
4. **Retail sentiment is contrarian** - if retail is 70% long, go SHORT

## Server Communication

**Base URL:** `http://127.0.0.1:5555`

| Endpoint | Purpose |
|----------|---------|
| `/health` | Server status |
| `/api/signal` | Trading signal for MT5 |
| `/api/events` | Upcoming events list |
| `/api/decision` | Full analysis with reasoning |

## Tech Stack

- **Python 3.10+** - Flask server, analysis logic
- **MetaTrader 5** - Trade execution via Expert Advisor
- **LLM via OpenRouter** - model panel for entries + exit model (key in `python/.env`)
- **CFTC** - COT data source
- **ForexFactory** - Economic calendar

## MQL5 Skills (Manual Load)

Read the relevant `SKILL.md` from `.agents/skills/` when the problem matches:

| Skill | Use When |
|-------|----------|
| `.agents/skills/mql5-indicator-patterns/` | Creating indicators, blank window, buffer issues |
| `.agents/skills/log-reader/` | Debugging EA, checking Print() output |
| `.agents/skills/article-extractor/` | Need mql5.com documentation |
| `.agents/skills/python-workspace/` | Data export, indicator translation |
