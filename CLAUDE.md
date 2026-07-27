# Sky Tower Project - Claude Code Context

## Overview
This is the root directory for the Sky Tower forex trading project. The main code is in the `SkyTowerAI/` subdirectory.

## Quick Navigation

```
Sky tower/
├── SkyTowerAI/           # Main project directory
│   ├── CLAUDE.md         # Detailed project context
│   ├── RUNBOOK.md        # Operations guide (PL) — start/verify/migrate
│   ├── DOCUMENTATION.md  # Full technical documentation
│   ├── config.py         # Configuration (READ FIRST!)
│   ├── python/           # Python backend (Flask server)
│   └── mt5/              # MetaTrader 5 Expert Advisor
├── .claude/
│   ├── commands/
│   │   └── sky_tower.md  # /sky_tower slash command
│   └── skills/           # MQL5 skills (manual load via /sky_tower skill)
│       ├── mql5-indicator-patterns/  # Indicator development patterns
│       ├── log-reader/               # MT5 log analysis
│       ├── article-extractor/        # mql5.com article extraction
│       └── python-workspace/         # MQL5-Python integration
├── wiki/                 # LLM-maintained documentation wiki (start: wiki/index.md)
├── instruction.md        # Token-preserving workflow rules
├── SkyTower-FX_V.3.0.pdf # Original strategy PDF (21MB - too large to read)
└── CLAUDE.md             # THIS FILE
```

## Documentation Wiki (wiki/)

`wiki/` is an LLM-maintained knowledge wiki (Karpathy's LLM-wiki pattern:
sources stay immutable, the LLM writes the wiki, the human reads it).

- Questions about how the system works or where a doc lives → start at `wiki/index.md`.
- Conventions and the INGEST/QUERY/LINT workflows → `wiki/schema.md`.
- After any significant architecture/behavior change → update the affected pages
  and append an INGEST entry to `wiki/log.md` (part of "done", like tests).
- `wiki/pages/documentation-map.md` maps every doc file, its freshness, and
  which file is authoritative for what.

## Custom Slash Command

Use `/sky_tower` to access the trading system:

```
/sky_tower help              - Show all commands and skills
/sky_tower status            - Check server health
/sky_tower events            - List upcoming events
/sky_tower decision          - Get trading signal
/sky_tower start             - Start Python server
/sky_tower test              - Run system tests
/sky_tower config            - Show configuration
/sky_tower skill [name]      - Load MQL5 skill (indicator/logs/articles/python)
```

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
1. `SkyTowerAI/CLAUDE.md` - Full context and quick reference
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

Skills are loaded on-demand via `/sky_tower skill [name]`:

| Command | Skill | Use When |
|---------|-------|----------|
| `/sky_tower skill indicator` | MQL5 Indicator Patterns | Creating indicators, blank window, buffer issues |
| `/sky_tower skill logs` | MT5 Log Reader | Debugging EA, checking Print() output |
| `/sky_tower skill articles` | Article Extractor | Need mql5.com documentation |
| `/sky_tower skill python` | Python Workspace | Data export, indicator translation |

**Skill Suggestions:** When you encounter MQL5 problems, I will suggest relevant skills to load.
