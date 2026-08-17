# Mapa dokumentacji

**TL;DR:** Po porządkach 27.07.2026 dokumentacja ma jedną obowiązującą generację:
wszystkie główne pliki opisują stan bieżący (4.1, natywny start, OpenRouter,
panel ryzyka), a styczniowe zrzuty sesji wylądowały w `SkyTowerAI/docs/archive/`.

Uwaga: daty modyfikacji plików na dysku bywają mylące (checkout gita je nadpisuje) —
oceny po TREŚCI, stan 2026-07-27.

## Źródła prawdy (co czytać, gdy chcesz…)

| Pytanie | Autorytatywne źródło |
|---------|----------------------|
| Jak uruchomić / operować / migrować na 24/7 | [RUNBOOK.md](../../SkyTowerAI/RUNBOOK.md) |
| Konfiguracja systemu | [config.py](../../SkyTowerAI/python/config.py) + panel (Risk & Daily Limits, Event Config) + `python/.env` |
| Jak system działa teraz | kod + [CLAUDE.md](../../SkyTowerAI/CLAUDE.md) + [system-overview.md](system-overview.md) |
| Kontekst startowy agenta | root `CLAUDE.md` (Claude) / root `AGENTS.md` (Codex) → oba kierują do `SkyTowerAI/CLAUDE.md` |
| Plan bieżącego brancha | [GPT_REVIEW_PLAN.md](../../SkyTowerAI/GPT_REVIEW_PLAN.md) |
| Wiedza badawcza (playbooki, BTMM, kalendarz) | `SkyTowerAI/research/` |

## Inwentarz plików (stan po porządkach 27.07.2026)

### Dokumenty żywe

| Plik | Język | Rola | Stan |
|------|-------|------|------|
| [RUNBOOK.md](../../SkyTowerAI/RUNBOOK.md) | PL | Operacje krok po kroku, migracja 24/7, rekompilacja EA, learning loop ops | ✅ aktualny (nagłówek odświeżony 27.07: 588 testów, F0–F5) |
| [research/DAX_OPEN_PLAN.md](../../SkyTowerAI/research/DAX_OPEN_PLAN.md) | PL | Analiza + plan: DAX na otwarciu 09:00 (odrzucony jako produkt), alternatywy na tezie newsowej (złoto/US500 na eventach USD), architektura profili instrumentów | ✅ 16.08.2026; wdrożenie: [multi-instrument.md](multi-instrument.md) |
| [SkyTowerAI/CLAUDE.md](../../SkyTowerAI/CLAUDE.md) | EN | Główny kontekst projektu (wspólny dla agentów) | ✅ przepisany 27.07: natywny start, OpenRouter, panel ryzyka, realne drzewo plików, pełne API, learning loop |
| [DOCUMENTATION.md](../../SkyTowerAI/DOCUMENTATION.md) | PL | Dokumentacja techniczna (4.1) | ✅ odświeżona 27.07 (OpenRouter w .env, wskaźniki na CLAUDE.md, changelog 4.1); sekcja Smart Exit oznaczona jako historyczna |
| [README.md](../../SkyTowerAI/README.md) | PL | Skrócony przegląd / landing | ✅ odświeżony 27.07 (4.1, START.bat, OpenRouter, pytest) |
| [GPT_REVIEW_PLAN.md](../../SkyTowerAI/GPT_REVIEW_PLAN.md) | EN | Plan 4 etapów brancha `gpt_review` | ✅ aktywny; Stage 1–2 done (26.07) |
| [CALIBRATION_ANALYSIS.md](../../SkyTowerAI/CALIBRATION_ANALYSIS.md) | PL | Analiza kalibracyjna na 44 679 ścieżkach przed LIVE | ✅ świeży (26.07) |
| root [CLAUDE.md](../../CLAUDE.md) | EN | Kontekst startowy Claude Code | ✅ odświeżony 27.07 (wiki, RUNBOOK w drzewie, OpenRouter) |
| root [AGENTS.md](../../AGENTS.md) | EN | Kontekst startowy Codex (**wciąż niezacommitowany**) | ✅ naprawiony 27.07 (artefakty `.Codex/` usunięte, wskazuje `SkyTowerAI/CLAUDE.md` i `.agents/skills/`) |
| root [instruction.md](../../instruction.md) | EN | Reguły oszczędzania tokenów | ✅ ponadczasowe |
| `research/screens/README.md`, `research/EVENT_PLAYBOOKS_2018-2019.md`, `research/BTMM_summary.md`, `research/calendar_2026_notes.md` | PL/EN | Materiały badawcze | ✅ stabilne (14–15.07) |

### Archiwum — NIE używać jako wiedzy

`SkyTowerAI/docs/archive/` (+ [README](../../SkyTowerAI/docs/archive/README.md)
z opisem) — styczniowe zrzuty kontekstu sesji: `SESSION_KNOWLEDGE.md`,
`SESSION_STATE.md`, `CONTEXT_V5.md`, `INSTALL.md`, `DEPLOYMENT_PLAN.md`.
Przeniesione 27.07.2026 (pełna kasacja była zablokowana przez uprawnienia sesji;
operator może je usunąć — zostają w historii gita).

### Poza gitem (świadomie)

- `SkyTower-FX_V.3.0.pdf` — strategia źródłowa, 21 MB, gitignored (`*.pdf`).
- `.claude/` (komenda `/sky_tower`, skille MQL5) — gitignored, lokalne.
- `dane historyczne/` — surowe dane bootstrapu, gitignored.
- `python/logs/` — dane runtime (jsonl, decision_context), gitignored.

_Aktualizacja: 2026-07-27 · stan: branch gpt_review (po f8d617d, working tree z porządkami)_
