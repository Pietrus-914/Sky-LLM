# Sprawdzenie reguły „fade ostatnich świec" na własnych ścieżkach (17.08.2026)

**Pytanie:** czy ruch po publikacji idzie *przeciwnie* do dryfu z ostatnich 3 minut przed
publikacją (playbook: „fade ostatnich świec, 78% na ekranach 2017-20")?

**Dane:** `knowledge/historical_paths.jsonl.gz` — 49 400 ścieżek (44 679 FX 2021-26
z HistData + 4 721 XAUUSD 2023-26), pola `pre_release_3m_pips` (dryf T-3→T0) i
`move_30min_pips` / `move_5min_pips`; tylko `data_status == ok`. Jednostki: pips FX
(0.0001; JPY brak w danych), złoto 1 pip = $0.10. „Fade" = znak ruchu po publikacji
przeciwny do znaku dryfu. EV = średnia z `-sign(pre) × move_30min` (pipsy, **przed
spreadem**).

## Wynik: reguła działa WARUNKOWO — tylko przy silnym dryfie, tylko na FX

| FX (5 par razem), próg \|dryf 3 min\| | n | fade 30 min | z | EV fade (pips, przed spreadem) |
|---|---|---|---|---|
| ≥ 2 pipsy (praktycznie każdy) | 15 986 | **51.1%** | 2.8 | +1.3 |
| ≥ 3 | 9 377 | 52.6% | 5.0 | +2.2 |
| ≥ 5 | 3 434 | **56.8%** | 8.0 | +4.3 |
| ≥ 8 | 870 | **61.6%** | 6.8 | +8.3 |
| ≥ 10 | 431 | **63.8%** | 5.7 | +10.0 |
| ≥ 15 | 98 | 65.3% | 3.0 | +16.6 |

Horyzont 5 min daje ten sam obraz (≥5: 55.8%, ≥8: 58.9%). Trafność rośnie
monotonicznie z wielkością dryfu → to nie szum: silny dryf tuż przed printem
(pozycjonowanie w ostatniej chwili) częściej się cofa niż kontynuuje.

**Per para, \|dryf\| ≥ 5:** AUDUSD 60.9% (n=312), NZDUSD 61.1% (311), EURUSD 58.9%
(516), USDCAD 56.4% (957), GBPUSD 54.3% (1 338). Tier-1: AUDUSD 80.8% (n=26!),
EURUSD 72% (25), NZDUSD 62.5% (32), USDCAD 51.1% (139), GBPUSD 51.2% (84) — dla
USDCAD/GBPUSD tier-1 reguła NIE działa.

**Po latach (\|dryf\| ≥ 5):** trafność 55-59% w każdym roku 2021-2025, ale **EV w
pipsach spadła**: 2022 +7.2, 2023 +5.9, 2024 +3.5, 2025 +0.9 (t=0.9), 2026 +0.6
(n=174, t=0.6) — cofnięcia stały się płytsze; przy spreadzie newsowym 8-15 pipsów
sama reguła NIE jest samodzielnie dochodowa (nigdy nie była: EV +4 pipsy przy
koszcie 10-15).

**Złoto (XAUUSD 2023-26):** brak jakiegokolwiek efektu przy każdym progu — ≥$1: 51.0%
(n=1 922), ≥$2: 48.1% (952), ≥$3: 49.5% (545), ≥$5: 48.3% (207); tier-1 ≥$3: 65.4%
ale n=26. Sekcja INSTRUMENT w prompcie słusznie ostrzega, że playbook FX nie
przenosi się na złoto.

Wypowiedzi (speaks/testifies) mają podobną trafność (58%), ale połowę EV
(+2.6 pipsa) — i tak nie są handlowane.

## Co z tego wynika dla systemu

1. Playbook „78%" był policzony na *ręcznie wybranych* dużych dryfach — jest zgodny
   z górnym ogonem tabeli (≥10-15 pipsów: 64-65%), a nie z ogółem (51%). W prompcie
   `learned_stats` już renderuje `fade_pre_drift` per event/para (próg 2 pipsy, 5 min)
   — czyli **wersję rozwodnioną**. Warto dodać wariant progowy (np. `fade_pre_drift_strong`
   dla \|dryf\| ≥ 5 pipsów) do `tools/build_learned_stats.py`, żeby model widział
   właściwą krzywą (decyzja operatora; wymaga progu per instrument — dla złota
   próg w pipsach 0.10 $ musi być inny).
2. Zalecana treść dla playbooka (do zatwierdzenia przez operatora, nie zmieniam
   `event_playbooks.json` automatycznie): „fade ostatnich świec tylko gdy dryf
   3-min ≥ 5 pipsów (≥ 8-10 = mocny sygnał, ~62-64%); przy słabym dryfie brak
   informacji; nie stosować na USDCAD/GBPUSD tier-1 ani na złocie; od 2025 cofnięcia
   są płytkie — nie liczyć na TP z samego fade'u".
3. Dla złota kierunek trzeba wywodzić ze znaku niespodzianki (mapowanie 76-86%
   wg badania M1), nie z mikrostruktury.

*Skrypt: analiza inline (sesja 17.08.2026), progi i definicje jak wyżej; powtarzalne
na `historical_paths.jsonl.gz` jedną pętlą po rekordach.*
