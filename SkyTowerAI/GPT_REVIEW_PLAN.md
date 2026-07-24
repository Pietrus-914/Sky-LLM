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

- [ ] Stage 1 — Recover and reconcile an open position after EA/server restart.
- [ ] Stage 1 review — focused tests, full regression suite, manual diff review, independent agent review.
- [ ] Stage 2 — Centralize pip/volume units, finalize SL before sizing, reject zero risk for trades, and validate broker execution results.
- [ ] Stage 2 review — focused tests, full regression suite, manual diff review, independent agent review.
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

## Commit log

- Pending.
