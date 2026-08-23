# Laboratorium strategii na złocie (XAUUSD) — 23.08.2026

Narzędzie: `python/tools/strategy_lab.py` (offline, generyczne per para; serwer
go nie importuje). Dane: `knowledge/historical_paths.jsonl.gz` — 4 721 ścieżek
XAUUSD 2023-01 → 2026-07 (HistData M1, eventy USD z FF, `actual` znane) +
`logs/event_paths.jsonl` (live, gdy istnieje). Jednostka: 1 pip = $0.10.
Spread liczony raz na trade: 12 pipsów ($1.20, `typical_news_spread_pips`).

```
python tools/strategy_lab.py --pair XAUUSD --impact HIGH --min-n 20
python tools/strategy_lab.py --pair XAUUSD --strategies pre_oracle --sl 0,80 --tp 0,100 --min-n 10 --json out.json
```

## Model wyniku (świadomie konserwatywny)

Ścieżki przechowują tylko ekstrema okien (first-5 high/low, 30-min high/low),
nie kolejność. Dlatego: stop uznaje się za trafiony, gdy niekorzystny ruch w
oknie dosięgnął SL — nawet jeśli TP byłby pierwszy; TP liczy się tylko, gdy SL
NIE został dosięgnięty. Dla wejść po publikacji (T+1/T+5) korzystny ruch jest
brany wyłącznie z próbek punktowych po wejściu (move_5/15/30 względem ceny
wejścia), bo ekstrema okna obejmują też knot pierwszej minuty sprzed wejścia.
Każdy wynik z SL/TP jest więc **dolnym ograniczeniem**; wariant „tylko wyjście
czasowe" (bez SL/TP) jest dokładny w horyzontach 5/15/30 min.

## Strategie

| nazwa | wejście | kierunek | uwagi |
|---|---|---|---|
| `pre_oracle` | T0 | z niespodzianki (actual vs forecast, z listą eventów o odwrotnym sensie: claims/unemployment) | **sufit** dla predykcji przed publikacją — live model nie zna actual |
| `pre_fade_drift` | T0 | przeciw dryfowi ostatnich 3 min (≥15 pipsów) | implementowalna reguła bez wyroczni |
| `post_confirm` | T+1 | za 1. świecą (\|m1\| ≥ 20) | „momentum" |
| `post_agree` | T+1 | za 1. świecą, gdy zgodna z niespodzianką | filtr na momentum |
| `post_fade` | T+1 | przeciw 1. świecy | „cofka" |
| `post_fade_5` | T+5 | przeciw ruchowi 5-min | |

## Wyniki (pipsy/decyzję po spreadzie, exit 15 min, SL 80, bez TP)

### Ranking eventów USD wg `pre_oracle` (n ≥ 10)

| event | n | win | avg | med | PF |
|---|---|---|---|---|---|
| CPI m/m | 22 | 77% | **+99.8** | 96.2 | 6.95 |
| Core CPI m/m | 25 | 68% | +85.1 | 88.2 | 5.24 |
| Core CPI y/y | 22 | 68% | +82.7 | 90.6 | 4.29 |
| CPI y/y | 29 | 69% | +78.5 | 88.2 | 4.98 |
| Non-Farm Employment Change | 37 | 62% | **+64.2** | 40.2 | 3.21 |
| Prelim GDP Price Index q/q | 12 | 67% | +28.6 | 19.4 | 2.49 |
| Core PCE Price Index m/m | 11 | 73% | +27.5 | 31.7 | 2.63 |
| Unemployment Rate | 28 | 50% | +17.8 | 5.5 | 1.45 |
| Final GDP q/q | 12 | 58% | +17.0 | 20.8 | 1.75 |
| Core PPI m/m | 31 | 58% | +15.4 | 4.7 | 1.79 |
| ADP Non-Farm Employment Change | 43 | 72% | +14.4 | 7.4 | 1.97 |
| PPI m/m | 36 | 56% | +13.6 | 1.9 | 1.72 |
| ISM Services PMI | 41 | 56% | +9.5 | 10.3 | 1.34 |
| JOLTS | 40 | 60% | +5.2 | 13.9 | 1.22 |
| Retail Sales m/m | 39 | 54% | +1.9 | 3.1 | 1.07 (z TP 60 @30 min: **+27.5**, TP trafia 72%) |
| Unemployment Claims | 163 | 51% | +1.3 | 1.0 | 1.05 |
| ISM Manufacturing PMI | 41 | 49% | −0.4 | −1.4 | 0.99 |
| Existing Home Sales | 38 | 40% | **−14.2** | −11.4 | 0.57 |
| New Home Sales | 36 | 42% | **−14.3** | −2.2 | 0.49 |
| Durable Goods / Trade Balance / Chicago PMI / … | | | −20…−38 | | |

Wnioski:
- **Edge na złocie = trafny kierunek PRZED publikacją na inflacji i NFP.**
  Mediana ruchu CPI ≈ 80-96 pipsów = $8-10 na decyzję przy 68-77% trafień
  (wyrocznia). Nawet przy 55% trafień LLM oczekiwana wartość pozostaje dodatnia
  (0.55×96 − 0.45×80 − 12 ≈ +5 pipsów; przy 65%: +22).
- **Home Sales nie handlować na złocie** (ujemne nawet z wyrocznią) — były na
  whiteliście TIER2 dla FX. Retail Sales tylko z TP (ruch → cofka).
- **Core PCE i PPI** — dodatnie i nieobecne na whiteliście → dodane w profilu.
- **Claims**: +1 pips po spreadzie — statystycznie nieopłacalne na złocie.
- Wyjście: zysk CPI maleje po 15. minucie (hold 5: +112.8, 15: +106, 30: +85);
  NFP stabilny 5-15 (+89/+92). ADP/ISM Services lepsze przy 30 min.
- SL 80 ($8) kosztuje niewiele przy trafnym kierunku (sl% 8-9% na CPI), a
  ścina błędne kierunki — wariant „SL 80" ma avg 97.8 vs 97.5 bez SL.
  Dolny próg profilu podniesiony do 60 (p80 knotu niekorzystnego CPI = 65).

### Wejścia po publikacji (po poprawce modelu: ekstrema PRZED wejściem nie liczą się)

| event | post_confirm (15 min) | post_fade (15 / 30 min) | post_fade_5 (30 min) |
|---|---|---|---|
| CPI m/m | −10.0 (win 49%) | −12.1 / **+16.2** (win 67%, p10 −83) | +12.8 (win 45%) |
| NFP | −8.6 … | −14.0 / −0.4 | −12.4 |
| Retail Sales | | −12.8 / +14.6 | +12.2 |
| PPI | | +7.2 / −0.9 | +8.0 |
| Claims | −6.8 | −18.9 / −21.9 | −8.2 … −16.8 |
| FOMC (FFR) | | **−61.0** / −40.3 | −27.7 |
| Core PCE | +17.3 (z SL 100) | −21.5 / −36.8 | −27.7 |

Wniosek: **ani momentum, ani fade po 1./5. minucie nie mają wiarygodnej
przewagi na złocie** — znaki zmieniają się między eventami i horyzontami,
a wyniki dodatnie (CPI post_fade @30 +16) mają p10 na poziomie −83 pipsów.
Nie wdrażamy żadnej strategii „potwierdzeniowej" na złocie. To przeczy
intuicji „poczekaj na pierwszą świecę" — ruch złota jest front-loaded
(mediana |m1| 80.6 ≈ |m30| 78.2 na CPI), a po nim szum.

`pre_fade_drift` (przeciw dryfowi 3 min przed publikacją): CPI +11 (n=15,
win 47%), ISM Manufacturing +56.6 (n=15), claims +9.9 — małe próbki, brak
spójnego sygnału; zgodne z `fade_pre_drift` ≈ 0.5-0.6 w `learned_stats`.
Prompt informuje model, że „fade ostatnich świec" nie ma na złocie
ustalonej przewagi (sekcja INSTRUMENT).

## Co z tego trafiło do kodu (23.08.2026)

- Profil XAUUSD: `sl_range (60, 120)`, `extra_events` (Core PCE Price Index,
  PPI), `skip_events` (New/Existing Home Sales) — stosowane przez
  `CalendarAggregator._event_is_tradeable` tylko, gdy USD jest routowany na
  złoto (`config.routed_event_policy`).
- Bramki szumu statystyk i kalibracji w pipsach instrumentu
  (`stats_min_move_pips 10`, `stats_min_directional_pips 5`,
  `calibration_big_move_pips 50`) — 68 bloków XAUUSD w `learned_stats.json`
  przeliczone, 486 bloków FX bez zmian.
- Horyzont wyjścia z decyzji wejściowej (`exit_minutes`) trafia do promptu
  modelu wyjścia jako miękki horyzont (`planned_exit_minutes`).

## Co zostaje do zbadania

- To samo laboratorium dla par FX (`--pair NZDUSD` itd.) — whitelist TIER1/2
  można zweryfikować tymi samymi liczbami.
- Pełne ścieżki (wszystkie 30 świec M1) zamiast ekstremów — zdjęłoby
  konserwatyzm modelu; `build_historical_paths.py` musiałby zapisywać ogon.
- Próg pewności panelu per event: z kalibracji (`/api/calibration`) po ~50
  decyzjach na złocie — dopiero wtedy wiadomo, ile z sufitu `pre_oracle`
  model realnie bierze.
