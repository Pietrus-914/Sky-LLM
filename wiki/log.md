# Dziennik wiki

Append-only. Nowe wpisy NA KOŃCU pliku, format nagłówka:
`## YYYY-MM-DD INGEST|QUERY|LINT | tytuł` (patrz [schema.md](schema.md)).

## 2026-07-27 INGEST | Bootstrap wiki (wzorzec LLM-wiki Karpathy'ego)

- Przeczytano: gist https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f,
  root `CLAUDE.md`, root `AGENTS.md`, `instruction.md`, `SkyTowerAI/CLAUDE.md`,
  `DOCUMENTATION.md` (nagłówki), `RUNBOOK.md`, nagłówki: `README.md`, `INSTALL.md`,
  `DEPLOYMENT_PLAN.md`, `SESSION_KNOWLEDGE.md`, `SESSION_STATE.md`, `CONTEXT_V5.md`,
  `GPT_REVIEW_PLAN.md`, `CALIBRATION_ANALYSIS.md`, `research/*.md`.
- Utworzono: `schema.md`, `index.md`, strony `system-overview.md`,
  `documentation-map.md`, `learning-loop.md`.
- Dopisano sekcję o wiki do root `CLAUDE.md` i root `AGENTS.md`.

## 2026-07-27 LINT | Pierwszy przegląd spójności istniejącej dokumentacji

Znaleziska (poprawki źródeł = zadania dla operatora, szczegóły w
[documentation-map.md](pages/documentation-map.md)):

- `SkyTowerAI/CLAUDE.md`: Docker opisany jako „primary run mode" — sprzeczne z
  `RUNBOOK.md` (natywnie od 10.07.2026, Docker legacy); liczby linii i drzewo
  modułów mocno nieaktualne; „Anthropic Claude / OpenAI" zamiast OpenRouter.
- `SkyTowerAI/DOCUMENTATION.md`: przykład `.env` z `ANTHROPIC_API_KEY`/
  `OPENAI_API_KEY` — system używa `OPENROUTER_API_KEY`; lista modułów niekompletna.
- `SkyTowerAI/RUNBOOK.md`: nagłówek „379 testów, stan 18.07" — od 24.07 jest 478.
- root `AGENTS.md` (niezacommitowany): artefakty podmiany Claude→Codex —
  nieistniejący katalog `.Codex/`, „Anthropic Codex", odwołanie do nieistniejącego
  `SkyTowerAI/AGENTS.md` (skille dla Codexa są w `.agents/skills/`).
- Martwe zrzuty styczniowe: `SESSION_KNOWLEDGE.md`, `SESSION_STATE.md`,
  `CONTEXT_V5.md`, `INSTALL.md`, `DEPLOYMENT_PLAN.md` — opisują usunięte elementy
  (smart exit TP1/TP2, `/api/zones`, `claude-opus-4`); kandydaci do
  `docs/archive/` lub kasacji (historia gita je zachowa) — decyzja operatora.

## 2026-07-27 INGEST | Porządki dokumentacji — jedna obowiązująca wersja

Realizacja znalezisk LINT z tego samego dnia (polecenie operatora: „zrób
porządek, żeby nie było staroci i trzymamy się najnowszej wersji"):

- Zweryfikowano stan faktyczny: pełny test suite **588 passed** (~12 s, venv),
  realna lista modułów `python/` (26 plików — pamięć projektu była niepełna:
  `mt5_data_exporter.py` i `signal_validator.py` ISTNIEJĄ; są też
  `calibration.py`, `episode_retrieval.py`, `reflections.py`,
  `playbook_distiller.py`, `position_store.py`, `trading_units.py`, `llm_util.py`).
- `SkyTowerAI/CLAUDE.md` przepisany w całości: natywny START.bat (Docker legacy),
  OpenRouter + panel modeli, ryzyko w panelu + `max_loss_usd`, realne drzewo
  plików, pełna tabela API (position/report, calibration, config/risk…),
  learning loop F0–F5, sekcja workflow z pułapką metaeditora.
- `DOCUMENTATION.md`: nagłówek 4.1 + banner stanu, `.env` → `OPENROUTER_API_KEY`,
  moduły uzupełnione skrótem, notka „LLM primary / rule-based fallback",
  limity dzienne → panel, wskaźnik na pełne API w CLAUDE.md, changelog 4.1,
  wersja w przykładzie `/health` 4.1.0.
- `README.md`: 4.1, START.bat, OpenRouter, ForexFactory jako główne źródło
  kalendarza, pytest zamiast test_system.bat, changelog 4.1.
- `RUNBOOK.md`: nagłówek → 588 testów / F0–F5 / Stage 1–2 (27.07).
- root `CLAUDE.md`: RUNBOOK w drzewie, OpenRouter zamiast „Claude/GPT".
- root `AGENTS.md`: naprawione artefakty podmiany (`.Codex/` → `.agents/skills/`,
  `SkyTowerAI/AGENTS.md` → `SkyTowerAI/CLAUDE.md`, sekcja Common Operations
  zamiast slash-commands, OpenRouter).
- Styczniowe zrzuty (`SESSION_KNOWLEDGE`, `SESSION_STATE`, `CONTEXT_V5`,
  `INSTALL`, `DEPLOYMENT_PLAN`) → `SkyTowerAI/docs/archive/` + README archiwum.
  Kasacja (`Remove-Item`/`git rm`) zablokowana przez klasyfikator uprawnień —
  operator może dokończyć usunięcie sam.
- Strony wiki zaktualizowane: `documentation-map.md` (nowe statusy),
  `system-overview.md` (588 testów).
