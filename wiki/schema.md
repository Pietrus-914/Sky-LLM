# Schema wiki — konwencje i workflow

Ta wiki realizuje wzorzec **LLM-maintained wiki** Andreja Karpathy'ego
(https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
zamiast za każdym razem szukać wiedzy od zera w kodzie i rozproszonych plikach,
LLM przyrostowo buduje i utrzymuje trwałą, spójną wiki. Cross-referencje robi się
raz; sprzeczności są flagowane, a nie odkrywane w kółko.

## Trzy warstwy

| Warstwa | Co to jest w tym projekcie | Kto pisze |
|---------|----------------------------|-----------|
| **Źródła** | Kod (`SkyTowerAI/python/`, `SkyTowerAI/mt5/`), historia gita, dokumenty operacyjne (`RUNBOOK.md`, `config.py`), badania (`SkyTowerAI/research/`), logi (`python/logs/`) | Człowiek / system |
| **Wiki** | `wiki/pages/*.md` + `index.md` + `log.md` | **Tylko LLM** (Claude Code / Codex) |
| **Schema** | Ten plik | LLM za zgodą operatora |

Operacje na wiki **nie modyfikują źródeł**. Jeśli lint wykryje błąd w źródle
(np. nieaktualny CLAUDE.md), zapisuje znalezisko w `log.md` — poprawka źródła
to osobna, normalna praca deweloperska.

## Hierarchia prawdy

Przy sprzeczności: **kod > świeży dokument operacyjny (RUNBOOK) > wiki > starszy dokument**.
Wiki nigdy nie jest pretekstem, żeby nie sprawdzić kodu — jest mapą, nie terytorium.

## Struktura katalogu

```
wiki/
├── schema.md      # TEN PLIK — konwencje i workflow
├── index.md       # katalog stron (jedna linia na stronę, wg kategorii)
├── log.md         # append-only dziennik operacji (INGEST/QUERY/LINT)
└── pages/         # właściwe strony wiki
    └── *.md
```

## Konwencje stron

- Nazwy plików: `kebab-case.md`, jedna strona = jeden temat.
- Struktura strony:
  1. `# Tytuł`
  2. **TL;DR** — 1–2 zdania.
  3. Treść: synteza i wskaźniki do źródeł, **nie kopie źródeł**. Linki do plików
     repo relatywne (`../../SkyTowerAI/RUNBOOK.md`), do innych stron wiki proste
     (`documentation-map.md`).
  4. Stopka: `_Aktualizacja: YYYY-MM-DD · stan: <branch/commit>_`
- Fakty liczbowe (liczba testów, wersje, ceny) zawsze z datą pomiaru — bez daty
  liczba jest bezwartościowa przy lincie.
- Język: polski (spójnie z RUNBOOK i dokumentami operacyjnymi); terminy
  techniczne i nazwy operacji po angielsku.

## Operacje

### INGEST — wprowadzenie nowej wiedzy

Kiedy: po istotnej zmianie architektury/zachowania systemu (merge większego
brancha, nowy podsystem), po nowym badaniu w `research/`, po ważnej decyzji
operatora.

1. Przeczytaj źródło (diff, dokument, wyniki badania).
2. Zaktualizuj dotknięte strony w `pages/` (typowo 1–5); utwórz nowe, jeśli temat
   nie ma strony.
3. Nowa strona → dopisz do `index.md` we właściwej kategorii.
4. Dopisz wpis `INGEST` w `log.md`.

### QUERY — odpowiadanie z wiki

Przy pytaniu o system zacznij od `index.md`, potem właściwe strony, dopiero potem
kod. Jeśli odpowiedź wymagała głębszego researchu i jest wartościowa na przyszłość
— zapisz ją jako nową stronę (filed back) i odnotuj `QUERY` w `log.md`.
Rutynowych odpowiedzi nie loguje się.

### LINT — przegląd zdrowia

Kiedy: okresowo (np. po każdym większym mergu do maina) albo na życzenie operatora.

Szukaj: sprzeczności między stronami a kodem/źródłami, przeterminowanych liczb,
stron-sierot (bez linku z `index.md`), brakujących cross-linków, luk (podsystem
bez strony). Znaleziska → wpis `LINT` w `log.md` (z listą), oczywiste poprawki
stron wiki od razu; poprawki źródeł zostają jako zadania dla operatora.

## Format log.md

Append-only, wpisy chronologicznie (nowe NA KOŃCU pliku), nigdy nie edytować
wstecz. Nagłówek wpisu ma stały, grep-owalny format:

```
## YYYY-MM-DD INGEST | krótki tytuł
## YYYY-MM-DD QUERY | krótki tytuł
## YYYY-MM-DD LINT  | krótki tytuł
```

Pod nagłówkiem bullet-lista: co przeczytano, które strony zmieniono, co wykryto.

## Zasady dla agentów (Claude Code / Codex)

- Sesja dotycząca dokumentacji lub „jak działa X" → najpierw `wiki/index.md`.
- Kończysz pracę, która zmienia architekturę/zachowanie → zrób INGEST
  (to część definicji „done", jak testy).
- Wiki nie zastępuje `CLAUDE.md`/`AGENTS.md` (kontekst startowy sesji) ani
  `RUNBOOK.md` (operacje) — jest warstwą syntezy i skorowidzem ponad nimi.
- Zmiany w tym pliku (schema) — tylko po uzgodnieniu z operatorem.

_Aktualizacja: 2026-07-27 · stan: branch gpt_review (po f8d617d)_
