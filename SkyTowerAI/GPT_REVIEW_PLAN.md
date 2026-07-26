# GPT review implementation plan

Branch: `gpt_review`

Started: 2026-07-24

## Scope and decisions

- The system has been running paper trades on a remote Windows server for about one week.
- `SKYTOWER_FORCE_DECISION` is intentionally left unchanged because the connected MT5 account is a demo account.
- Changes are split into four reviewable stages. Every stage must pass focused tests, the full Python test suite where applicable, a diff review, and an independent agent review before it is committed.
- Existing uncommitted changes present before this work must be preserved and excluded from GPT review commits unless a stage necessarily overlaps them.

## Pre-existing uncommitted files

- `SkyTowerAI/python/llm_decision_engine.py`
- `SkyTowerAI/python/market_context.py`
- `SkyTowerAI/python/server.py`
- `SkyTowerAI/tests/integration/test_server.py`
- `SkyTowerAI/tests/unit/test_market_context.py`
- `.agents/`
- `AGENTS.md`

## Stages

- [x] Stage 1 — Recover and reconcile an open position after EA/server restart.
- [x] Stage 1 review — focused tests, full regression suite, manual diff review, independent agent review.
- [x] Stage 2 — Centralize pip/volume units, finalize SL before sizing, reject zero risk for trades, and validate broker execution results.
- [x] Stage 2 review — focused tests, full regression suite, manual diff review, independent agent review.
- [ ] Stage 3 — Add ticket/state versioning, durable commands with ACK/NACK, and idempotent EA/server lifecycle messages.
- [ ] Stage 3 review — focused tests, full regression suite, manual diff review, independent agent review.
- [ ] Stage 4 — Correct event semantics and quote-aware sentiment; reject simulated production sentiment.
- [ ] Stage 4 review — focused tests, full regression suite, manual diff review, independent agent review.
- [ ] Final review — full regression suite, cross-stage safety audit, documentation and commit verification.

## Verification log

### Baseline

- Branch created from commit `cb72b97`.
- Baseline Python suite from the preceding audit: `489 passed`.
- Repository virtual environment points to a missing interpreter. Tests are run with the bundled Python runtime and the repository's installed packages.

### Stage 1

- Added atomic server-side active-position snapshots, restart recovery, broker reconciliation, close tombstones and identity-based idempotency.
- Added EA recovery by account/magic/symbol, durable position metadata, complete-history close reporting, fail-closed ownership checks and recovered-position local guardrails.
- Focused recovery/position suite: `65 passed`.
- Full Python suite after final review fixes: `514 passed`.
- Exact staged snapshot (excluding pre-existing user changes): `503 passed` with a local test-only OpenAI key.
- MetaEditor compilation of the current EA and includes: `0 errors, 0 warnings`.
- Independent backend review: `APPROVE`, no P0/P1/P2.
- Independent MT5/risk review: `APPROVE`, no P0/P1.
- Deferred P2 items: durable cross-instance entry lease and atomic EA metadata replacement. These are assigned to Stage 3.

### Stage 2

- Added shared pip, price, broker-spread and volume helpers for Python and MT5, including 2/3/4/5-digit feeds and quote-currency-aware JPY handling.
- Finalized and normalized the protective SL before lot sizing. `OrderCalcProfit()` now values the exact stop in account currency and includes configured adverse slippage.
- BUY/SELL signals now require finite positive `lot_percent` and `max_loss_usd`; invalid, expired or unknown-direction decisions are rejected before served lineage is recorded.
- Open, SL/TP modification, partial close and full close now require execution retcodes plus broker-state postconditions. Live positions are bound from canonical `POSITION_TICKET` / `POSITION_IDENTIFIER`, never `ResultOrder()`.
- Ambiguous open outcomes enter a 30-second fail-closed recovery window. Existing SLs can only be tightened; incomplete partial fills are not acknowledged as complete.
- Corrected break-even, trailing-stop and BE-tolerance pip multipliers, including JPY coverage. Corrected JPY zone sizes and target conversions.
- Focused Stage 2 suite: `99 passed`; signal-contract follow-up: `17 passed`.
- Full Python suite after final review fixes: `563 passed` (offline network guard enabled).
- Exact-source MetaEditor compilation of the EA and includes: `0 errors, 0 warnings`; indicator compilation: `0 errors, 0 warnings`.
- Independent Python review: `APPROVE`, no P0/P1/P2.
- Independent MT5/risk review: `APPROVE`, no P0/P1; nonblocking netting partial-close limitation remains fail-closed and command ACK/retry stays assigned to Stage 3.

### Stage 1+2 independent verification (Claude, 2026-07-26)

- Full working-tree suite re-run: `563 passed`.
- Exact staged snapshot re-run (`git write-tree` archive, offline, dummy API key): `552 passed` — the missing 11 tests live in the pre-existing unstaged user files, so the commit content is green standalone.
- Fresh MetaEditor compile of the current sources (stale-log-safe procedure): `0 errors, 0 warnings`. The previous `SkyTowerAI_EA.ex5` predated the Stage 1/2 sources and was rebuilt.
- Invariants checked and preserved: `_mark_decision_event_analyzed` on real opens only, guardrails on floating+realized, `GetRealizedPnL` complete = IN/OUT volume balance, direction whitelist, max-hold and double spread entry blocks.
- Known minor gaps accepted and assigned to Stage 3: `/api/position/reconcile` is never called by the EA (a stale server snapshot without matching EA metadata needs an operator `has_position:false` POST or deleting `logs/active_position.json`); a 503 from `/api/position/opened` after in-memory registration makes the EA fall back to `/api/trade-executed`, double-counting one trade against the daily limit (conservative direction).
- `docker-compose.yml` hardcoded `SKYTOWER_FORCE_DECISION=true`; changed in a follow-up commit so test mode is opt-in via `python/.env` (compose `environment:` overrides `env_file`, so the hardcode also could not be disabled from `.env`).

### Pre-live multi-agent review (Claude, 2026-07-26)

Full-code review before the planned Monday live start: 7 domain finders +
adversarial verification (3 verifier agents hit a session limit; their
findings were verified manually against the source). 21 findings confirmed,
1 refuted, all confirmed items fixed the same day:

- P0 sentiment quote-side inversion (`get_currency_sentiment` counted pair
  votes without base/quote inversion — CAD bias inverted 100% of the time);
  fabricated sentiment removed (TradingView technical rating dressed as
  retail positioning, hardcoded SimulatedSentiment fallback) — empty
  sources now surface as NO_DATA.
- P0 RUNBOOK go-live procedure pointed at docker-compose for FORCE_DECISION
  while the native server reads python/.env — rewritten with a two-step
  verification (no banner in server.log + forced:false in /api/decision).
- P1 `open_time` sent as broker-time epoch and parsed as UTC — minutes_open
  was negative for a trade's whole life, so server max-hold and time-phased
  exits never fired. EA now sends UTC; the server clamps future epochs.
- P1 `_compare_values` treated higher unemployment/claims as IMPROVEMENT —
  config.LOWER_IS_BETTER_MARKERS swaps the label for inverse indicators.
- P1 CFTC `'%BRITISH POUND%'` LIKE also matched the EUR/GBP cross contract
  (verified live: weekly change computed main-vs-cross of the same week) —
  prefix match + defensive post-filter.
- P1 panel event whitelist: the dashboard's `{events:[...]}` payload was
  silently dropped AND three call sites pinned the import-time
  HIGH_IMPACT_EVENTS list — the whitelist now applies at call time and
  persists via runtime_overrides (immutable *_ALL rosters as the base).
- P1 EA max-loss guard used floating-only P/L (invariant 7) — realized legs
  now tracked (refresh from deal history on volume drops) and included.
- P2 hardening: guardrails survive snapshot-store write failures (no more
  permanent HOLD), fresh-open reports no longer muted by a failed metadata
  persist, NO_CHANGES accepted for SL/TP modifies (entry postcondition can
  no longer market-close a healthy protected position), corrupt recovery
  metadata quarantined to visible BLOCKED instead of a silent forever-
  PENDING stall, WebRequestPost UTF-8 byte truncation fixed, TE naive
  datetimes localized, /api/decision/refresh runs the LLM outside
  decision_lock, /api/trade-executed requires `pair`.
- Deferred (latent, unused in the live path): inverted FVG labels + fill
  check in zone_analyzer.find_fvg — live entries consume only liquidity
  pools; fix scheduled separately.

Verification: full suite `582 passed` (19 new pinning tests);
MetaEditor compile of the EA: `0 errors, 0 warnings`.

## Commit log

- `docs: add staged GPT review plan` (`2fa29cb`)
- Stage 1: `fix: recover and reconcile active positions` (see branch history).
