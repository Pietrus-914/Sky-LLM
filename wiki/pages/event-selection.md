# Selekcja eventów: whitelista nazw i klaster jednej minuty

_Stan na 2026-08-18 (branch `feature/multi-instrument`). Dwa bugi znalezione
w przeglądzie logów produkcji 17–18.08.2026 i ich naprawa._

## TL;DR

Co system w ogóle bierze do handlu, zależy od trzech niezależnych filtrów:
**próg impactu** (`MIN_IMPACT_LEVEL`), **lista nazw** (TIER1/TIER2, chyba że
`TRADE_ALL_EVENTS`) i **lista walut** (`config.CURRENCY_PAIRS`). Dwie rzeczy
w tym łańcuchu były zepsute:

1. **panel wycinał nazwy z whitelisty** — decyzje Fed/BoE/BoC przestawały być
   handlowane po każdym Zapisz, bez śladu w interfejsie;
2. **eventy tej samej minuty szły seryjnie** — jedna publikacja kupowała do
   4 paneli LLM, a trade lądował pod nazwą najsłabszego członka klastra.

## Bug 1 — panel przycinał whitelistę (od 29.07.2026, naprawione 18.08.2026)

`dashboard.html` miał **13 nazw wpisanych na sztywno** w JS, a roster w
`config.py` urósł 29.07 do 19 nazw, bo feed ForexFactory nazywa decyzje stóp
`Federal Funds Rate` / `Official Bank Rate` / `Overnight Rate`, a nie
`Interest Rate Decision`. Zapisz wysyłał nazwy **zaznaczone**, serwer trzymał
je jako `enabled_events` (lista WŁĄCZONYCH) i filtrował roster do tej listy —
więc każda nazwa, której panel nie znał, wypadała z `HIGH_IMPACT_EVENTS`.

Efekt: w trybie whitelisty (`TRADE_ALL_EVENTS = OFF`) **FOMC, BoE i BoC nie
były handlowane**, a operator nie miał czego odznaczyć ani gdzie tego zobaczyć.

### Naprawa

| Warstwa | Zmiana |
|---|---|
| `config.py` | Zapisywana jest **dopełnienie: `disabled_events`** (nazwy WYŁĄCZONE). Nazwa dodana do rostera w nowszej wersji serwera jest domyślnie aktywna. `set_enabled_events(enabled, known_roster)`, `disabled_event_names()`, `_apply_disabled_events()`. |
| migracja | Stary klucz `enabled_events` jest tłumaczony przy starcie: za świadomie wyłączone uznajemy tylko nazwy z `LEGACY_PANEL_EVENT_ROSTER` (to, co stary panel umiał wyświetlić); reszta wraca do gry, a `CONFIG_NOTES` zgłasza migrację. |
| `server.py` | GET `/api/config/events` oddaje `tier1_events_all`, `tier2_events_all`, `disabled_events`. Usunięta martwa gałąź POST `tier1_events`/`tier2_events` (bez walidacji i bez zapisu; string w body robił z `HIGH_IMPACT_EVENTS` napis, po którym predykat iterował **znak po znaku**). |
| `dashboard.html` | Checkboxy renderowane z rostera serwera (`renderEventRoster`), nigdy z listy w HTML. Zapis wysyła też `roster` — nazwy, które ta karta faktycznie pokazała — więc karta otwarta sprzed restartu nie wyłączy nazw, których nie widziała. Gdy roster się nie wczytał, Zapis **nie rusza** whitelisty, a panel pokazuje czerwony komunikat (wcześniej odpowiedź 200 bez rostera zostawiała siatki na wieczne „loading…”). Usunięty martwy odczyt z `localStorage` (nikt tam nie pisał). |
| POST bez `roster` | Klient, który nie deklaruje rostera (panel sprzed 18.08.2026, skrypt), jest zawężany do `LEGACY_PANEL_EVENT_ROSTER` + ostrzeżenie w logu. Bez tego Zapis ze starej karty wyłączyłby 6 nowszych nazw i utrwalił je w **nowym** kluczu `disabled_events`, którego migracja legacy już nie naprawia — bug samoleczący zamieniłby się w trwały. `"roster": "*"` (`cfg.ROSTER_ALL`) = świadome „wyłącz całe dopełnienie”. |

**Sprawdzenie stanu:** `curl http://127.0.0.1:5556/api/config/events` →
`tier1_events`/`tier2_events` to efektywna whitelista, `disabled_events` to
wyłączone. Panel nie jest źródłem prawdy — serwer jest.

## Bug 2 — klaster tej samej minuty (naprawione 18.08.2026)

Urzędy publikują całą rodzinę w jednym timestampie: CAD `CPI m/m` (HIGH) razem
z `Median`/`Trimmed CPI y/y` (HIGH) i `Common CPI y/y` (MEDIUM); NFP razem
z `Average Hourly Earnings` i `Unemployment Rate`. Rynek wycenia **łączną
niespodziankę** — jest jedna ścieżka ceny, więc może być tylko jeden trade.

Updater traktował je jak niezależnych kandydatów: analizował pierwszego,
a przy SKIP oznaczał jako przeanalizowaną **tylko tę nazwę**, więc kolejny skan
(15 s) kupował panel dla następnego rodzeństwa.

Skutki (produkcja, 17.08.2026, CAD CPI 12:30 UTC):
- do **4 płatnych paneli** na jedną publikację (~$0.85 zamiast ~$0.21);
- każda kolejna analiza startowała później, a deadline panelu to
  `max(T-20 s, 10 s)` — ostatni sibling głosuje pod 10-sekundowym zegarem albo
  spada do silnika regułowego;
- trade dostał etykietę **najsłabszego** członka (`Common CPI y/y`, MEDIUM),
  więc kuratorski playbook `CPI m/m` i historia reakcji trafiały pod nazwę,
  której nikt nie szuka;
- prompt pokazywał modelowi tylko dane tego słabszego printu — nie wiedział,
  że w tej samej sekundzie wyszedł nagłówek HIGH.

### Naprawa — `python/event_cluster.py`

- `release_key(event)` = `(waluta, minuta)` — ta sama precyzja co klucz
  `_analyzed_event_key` w serwerze.
- `pick_dominant(events)` — porządek **total**: impact → rodzina
  (`FAMILY_ORDER`, decyzje stóp rangą 0 przez `is_rate_decision`) → wariant
  zmodyfikowany (`core/final/flash/prelim/revised/trimmed` ustępuje czystej
  nazwie) → nazwa. Kolejność w feedzie nie ma znaczenia.
- **Ten sam porządek** importuje offline'owy `tools/build_learned_stats.py`
  (miał własną kopię) — etykieta decyzji i bundling statystyk muszą się
  zgadzać, inaczej prompt cytuje base-rate'y dla innej nazwy niż trade.
- `co_release_brief(dominant, candidates)` — rodzeństwo do promptu (nazwa,
  impact, forecast, previous), max 6, bez duplikatów.

W updaterze: dominant jest analizowany raz, rodzeństwo od razu oznaczane jako
przeanalizowane, a lista trafia do promptu jako blok
`CO-RELEASED AT THE SAME MINUTE`. Źródłem rodzeństwa jest
`calendar.peek_cached_events()` — **nigdy nie sięga do sieci**, bo stall
w oknie preload zjadłby czas potrzebny panelowi.

Prompt pojedynczego eventu zostaje **bajt w bajt** taki sam (blok pojawia się
tylko przy klastrze), więc `ENTRY_PROMPT_VERSION` nie był podbijany — wersja
pinuje instrukcje i schemat, a opcjonalne bloki danych (playbook, epizody,
learned stats) i tak pojawiają się warunkowo.

## Znane ograniczenia (świadome)

- **Klaster jest per waluta.** Gdy tę samą minutę dzielą USD i CAD (NFP-Friday:
  CAD Employment Change + NFP — obie decyzje celują w USDCAD), powstają dwie
  decyzje i dwa panele, a blok CO-RELEASED każdej z nich nie wspomina o drugiej
  walucie. To ~11 minut rocznie. Grupowanie po instrumencie byłoby gorsze:
  SKIP na USD po cichu zamknąłby temat CAD bez żadnej analizy, a etykiety
  (`analyzed_events`, reakcje, reżimy, learned-stats) są kluczowane
  `waluta|nazwa`. Bezpieczna połowa na przyszłość: **poszerzyć prompt**, nie
  grupowanie — dopisać do bloku printy innych walut, które trafiają na ten sam
  wykres, oznaczone jako obca gospodarka.
- `/api/decision/refresh` (ręczny) nie klastruje — bierze pierwszy handlowalny
  event. To zachowanie sprzed zmiany; endpoint jest ręcznym force'em.
- Karta **Active Currencies** w panelu jest **poglądowa** — serwer ignoruje te
  checkboxy, waluty wynikają z `config.CURRENCY_PAIRS`.

## Pliki

`python/event_cluster.py` · `python/config.py` (whitelista + migracja) ·
`python/server.py` (`_select_release_group`, `/api/config/events`) ·
`python/llm_decision_engine.py` (blok CO-RELEASED) ·
`python/templates/dashboard.html` · `python/tools/build_learned_stats.py`

Testy: `tests/unit/test_event_cluster.py`,
`tests/unit/test_event_whitelist_persistence.py`,
`tests/integration/test_release_cluster_selection.py`,
`tests/integration/test_live_readiness_server.py`.

Powiązane: [system-overview.md](system-overview.md) ·
[multi-instrument.md](multi-instrument.md) ·
[learning-loop.md](learning-loop.md)
