# Playbooki eventów ze screenów historycznych

## Po co

LLM podejmuje decyzję o kierunku, ale brakuje mu wiedzy o tym, JAK dany typ eventu
zwykle porusza rynkiem (spike-and-reverse? trend przez 5 minut? fake move w pierwszej
minucie?). Ta wiedza jest na Twoich historycznych screenach — destylujemy ją do pliku
`python/knowledge/event_playbooks.json`, który serwer automatycznie wstrzykuje do promptu
jako sekcję **EVENT PLAYBOOK** (bez restartu — plik jest przeładowywany po zmianie).

## Jak używać

1. Wrzuć screeny do tego folderu. Nazwa pliku najlepiej: `WALUTA_nazwa-eventu_data.png`,
   np. `USD_core-cpi_2026-06-11.png`, `NZD_official-cash-rate_2026-05-28.png`.
   Na screenie idealnie: M1/M5 wokół momentu publikacji + widoczna godzina.
2. W sesji Claude Code poproś: *"przeanalizuj screeny w research/screens i zaktualizuj
   event_playbooks.json"*. Claude obejrzy wykresy, poszuka wspólnych schematów
   (kierunek vs zaskoczenie, timing ruchu, zasięg w pipsach, zachowanie spreadu,
   pułapki pierwszej minuty) i zapisze/zaktualizuje playbooki.
3. Wpisy możesz też edytować ręcznie — format niżej.

## Format `python/knowledge/event_playbooks.json`

(Katalog `knowledge/` jest trackowany w git — w przeciwieństwie do `logs/`,
które jest ignorowane i nie trafia do ZIP-a wdrożeniowego.)

Klucz = nazwa eventu (dopasowanie po znormalizowanej nazwie: bez wielkości liter,
nawiasów i miesięcy) ALBO fallback dla całej waluty: `"CURRENCY:USD"`.

```json
{
  "Core CPI m/m": {
    "pattern": "Spike in the surprise direction within 5-15s, ~60% of the time a partial retrace after 2-3 min.",
    "typical_behavior": "Beat -> USD strength 15-40 pips/5min on USDCAD; inline -> chop, avoid.",
    "notes": "First 30s spread often 8-12 pips; entries against the spike have poor risk/reward."
  },
  "CURRENCY:NZD": {
    "pattern": "NZD events at 21:00-22:00 UTC hit thin liquidity - moves overshoot then mean-revert.",
    "typical_behavior": "OCR decisions: 30-80 pips/5min on NZDUSD.",
    "notes": "Spread on NZDUSD regularly >10 pips at release."
  }
}
```

Pola (wszystkie opcjonalne, ale co najmniej jedno musi być): `pattern`,
`typical_behavior`, `notes`. Piszemy PO ANGIELSKU — to trafia do promptu LLM.

## Priorytet dopasowania

1. Dokładna (znormalizowana) nazwa eventu — np. `"Core CPI m/m"`.
2. Fallback walutowy — np. `"CURRENCY:USD"`.
3. Brak wpisu → sekcja EVENT PLAYBOOK w ogóle nie pojawia się w prompcie (zero kosztu).
