# Schematy zagrań pod eventy — badanie danych historycznych 2017–2020

**Data badania:** 15.07.2026 · **Materiał:** 150 unikalnych screenów MT5 zespołu Roberta (286 plików po dedupie),
arkusz Google z analizami zespołu (zakładki CAD/AUD/SEK/NOK/GBP), wytyczne Roberta, PDF „BTMM — The Market Maker
Method" (84 str.), weryfikacja web u źródeł (RBNZ/RBA/BoC/Fed/ABS/StatCan/BLS/Stats NZ).
**Produkty:** `python/knowledge/event_playbooks.json` (30 wpisów — automatycznie wstrzykiwane do promptu LLM),
wzmocnione instrukcje promptu (punkty b i d checklisty), dataset `research/screens/extracted_dataset.json`
(121 rekordów), `research/BTMM_summary.md`, `research/calendar_2026_notes.md`.

---

## 1. KLUCZ GŁÓWNY — schemat, który spina wszystkie dane

Strategia zespołu to **metoda BTMM przeniesiona na moment publikacji**: przed newsem market maker
wykonuje ruch-pułapkę (wycinka/stop-hunt), a właściwy ruch idzie w przeciwną stronę.

**Statystyki (nasze zliczenia, nie deklaracje):**

| Reguła | Trafność | Źródło |
|---|---|---|
| Fade ostatnich 1–3 świec M1 przed publikacją | 36/46 = **78%** (arkusz) · FOMC **9/10** · RBNZ **8/10** · CAD 10/14 = 71% · NZ CPI 5/7 · AUD GDP 4/4 | arkusz + screeny |
| Bollinger (powrót do wstęgi: pod→BUY, nad→SELL) | 27/42 = **64%** (uwaga: SEK odwrotnie, 3/8) | arkusz |
| RSI (<50→BUY, >50→SELL) | 27/38 = **71%** | arkusz |
| **Konfluencja 3/3 (świeca+BB+RSI zgodne)** | **19/20 = 95%** | arkusz |
| Dryf 2h przed eventem jako predyktor | **brak przewagi** (z dryfem ≈ przeciw dryfowi ≈ 50/50 wszędzie) | screeny |

**Hierarchia ważności** (potwierdzona jedynym błędem konfluencji — RBA 05.02.2019):
przy decyzjach o stopach **fundament (zaskoczenie vs wycena rynkowa) > technika**. Technika działa
najlepiej na odczytach danych; na decyzjach banków centralnych liczy się komunikat.

**Anatomia minuty publikacji** (powtarzalna na ~połowie eventów):
1. W sekundach publikacji przeciwny flush/wycinka: 2–18 p (NZD), 8–12 p (AUD jobs), do 20–35 p (FOMC/NZ jobs).
2. Właściwy ruch — zwykle 1–2 świece M1 robią 80–110% wiarygodnego zasięgu.
3. Ekstremum 1. minuty to często NAJGORSZA cena wejścia (pościg kosztuje 15–35 p oddania).
4. Unieważnienie: spike wracający do zera w ≤6 min → dzień zwykle idzie w drugą stronę (sygnatura RBA III–IV 2019).

---

## 2. Schematy per event (najważniejsze liczby)

Zasięgi „1/5/10" = pipsy ruchu korzystnego po 1/5/10 minutach (adnotacje zespołu na screenach).

### Banki centralne

| Event | n | 1 min | 5 min | 10 min | Schemat |
|---|---|---|---|---|---|
| **RBNZ (OCR)** | 9 | 20–120 (med. ~85) | 31–126 | 29–132 | holdy: spike-and-hold + **2. noga po 30–90 min**; obniżki: profil gasnący (93→66→61) |
| **RBA (Cash Rate)** | 7 | 4,5–50 (med. ~16) | — | 13–57 | dzień RBA = AUD-up **6/7** (relief); z płaskiej konsolidacji 2h największe eksplozje |
| **FOMC** | 10 | 4–46 (med. ~38) | 15–61 (med. ~50) | 17–51 (**< 5 min w 4/5!**) | szczyt 4–6 min; konferencja +30 min = **osobny event** (1–2/6 pełny odwrót) |
| **BoC** | 1 | 45 | 46 | 38 | jedna świeca M1 z komunikatu; hold 30+ min; fade ~70% po godzinach |

- **RBNZ — cecha definiująca:** „inline" ≠ spokój. 5/6 decyzji zgodnych z prognozą i tak dało 45–132 p
  (komunikat!). Pułapka: ostry kontr-ruch w ostatnich 3–15 min przed decyzją to zwykle zła strona.
  Najgorszy przypadek: XI 2018 — 50 p w dół i pełny powrót w 60 sekund (pin 11 p).
- **RBA:** flush w pierwszych sekundach przeciw właściwemu kierunkowi 3/4 przypadków. Od 2024 r.
  konferencja PO KAŻDEJ decyzji (+60 min) — ryzyko 2. nogi, którego nie ma w statystykach 2018–19.
- **FOMC:** stopa ZAWSZE wyceniona (10/10 inline) — kierunek robi język komunikatu/dots. Paradoks 2019:
  obniżki → USD w górę, gołębie holdy → USD w dół. Shakeout 2–7 min przed komunikatem to norma.

### Dane NZD (publikacje 22:45 UTC lata / 21:45 zimy — martwa płynność!)

| Event | n | 1 min | 5 min | 10–15 min | Schemat |
|---|---|---|---|---|---|
| **NZ jobs (czyste zaskoczenie)** | 4–5 | 46–74 (med. ~52) | 45–58 | 58–66 | 1 gigantyczna świeca → hold lub oddanie 25–45% |
| **NZ jobs (konflikt wewn.)** | 3 | 10–41 | — | — | whipsaw/pełny odwrót w 3–10 min → **skip** |
| **NZ CPI** | 7 | ~14–30 | do 28 | 20 | spike → oddanie 50–75% (małe zaskoczenia) / 20–30% (duże); mediana ~28 p |
| **NZ GDP** | 6 | 11–39 (med. ~25) | 17–33 | 23–35 | spike-and-hold, **2. noga w 7–10 min** (10 min ≥ 5 min w 3/4!) |
| **NZ retail (core+headline zgodne)** | 1 | 20–22 | 18 | — | szczyt ~2 min, szybko oddaje — TP wcześnie |
| **NZ retail (konflikt)** | 1 | ~0 | — | — | brak reakcji → **skip** |

- Pierwsza świeca z przeciwnym stabem 15–18 p to przy NZ GDP **norma**, nie sygnał odwrotu.
- Kierunek z linii **q/q** (nie y/y!) — III 2019 rynek kupił NZD przy inline q/q mimo czerwonego y/y.
- Beat przy hamującej gospodarce (wszystkie „previous" wyżej) → mały, gasnący ruch.
- AUDNZD wyraża eventy NZD mocniej niż NZDUSD: stopy +20–25%, **jobs 3–4×**.

### Dane AUD (01:30 UTC zimy / 00:30 lata? — patrz kalendarz; ABS 11:30 lokalnie)

| Event | n | 1 min | Dalej | Schemat |
|---|---|---|---|---|
| **AU jobs** | 7 | 23,5–49 (med. ~32) | 15 min: do 35–38 | **stopa bezrobocia > headline (3/3 konflikty!)**; czyste printy trzymają 50–100% do 10–15 min |
| **AU GDP** | 4 | 37–47 | 4–6 min: 34–68 | 4/4 wg zaskoczenia, 4/4 przeciw ostatnim świecom; płytkie odbicia |
| **AU retail** (†2025) | 5 | 10,5–30 (med. ~13) | — | 4/5 fade 50–100% w 5–15 min; trzyma tylko duży beat + zgodny bilans handlowy |
| **AU CPI kw.** | 1 | ~22 | — | pierwszy spike = pułapka (pełny V-odwrót w 1–4 min); od XI 2025 CPI **miesięczne** |

- Pełny odwrót rajdu 49 p po ~40 min (21.02.2019) → limit trzymania 30 min w systemie jest słuszny.
- Kanoniczna manipulacja: 15-minutowe wypchnięcie na szczyt zakresu tuż przed zawaleniem -68 p (GDP XII 2018).

### Dane CAD (12:30 UTC lata; BoC 13:45)

| Event | n | Zasięg | Schemat |
|---|---|---|---|
| **CAD jobs (USDCAD)** | 5 | 27–95 p/5 min (med. ~50); max 140 | **los ruchu dyktuje równoczesny NFP**: przeciwny → pełny odwrót **5/5**; zgodny/brak → hold **4/4** |
| **CAD jobs (crossy)** | 12 | 8–30 p/1 min | jw.; cross ≈ 1/3–1/2 wielkości USDCAD |
| **CAD CPI/retail/GDP** | 9 | 18–95 (med. ~52 USDCAD) | spike wg zaskoczenia 9/9, ale **5/9 pełny/prawie pełny powrót**; czysty hold tylko bez ropy/US-danych |

- **Mit obalony:** „CAD reaguje 10–30 s przed czasem" — NIE potwierdzone (2–3/15 za, 5–6/15 pre-lean
  wręcz mylący). Statystycznie mocniejsza reguła odwrotna: przeciw ostatnim świecom 71%.
- Detale odwracają headline: -88K przy gorących płacach = pełny odwrót (II 2018). Zaskoczenie <±5K = whipsaw.
- Przy podwójnej publikacji (CPI+retail) rynek gra **CPI**. Ropa potrafi skasować 100-pipsowy spike w godziny.

### USD CPI, NFP, Scandi, GBP

- **US CPI** (12:30 UTC): na crossach małe — 6–17 p/1 min (med. ~9). Kierunek za **CORE** (100% rozstrzygalnych).
  Mieszany core/headline lub inline → spike w pełni wraca w 10–25 min. Czysty szeroki miss/beat → trend 10–30 min.
  Uwaga na zanieczyszczenia (claims/mówcy Fed w tej samej minucie).
- **NFP:** w naszych danych głównie jako dominator minuty CAD. BTMM wprost odradza granie NFP; płace
  potrafią odwrócić headline (+313K, miss płac → USD sold, III 2018). Ostrożność/mniejszy rozmiar.
- **NOK/SEK:** jedna gwałtowna świeca M1 (głównie knot), wypełnia się w 1–2 świece — reguła Roberta
  „zamykać po 1–2 świecach M1" **zweryfikowana 4/4** dla NOK; SEK żyje 6–10 min. Reakcje startują
  2–5 min PRZED oficjalną minutą (5/6 wykresów). Nawet Norges Bank (~500 pkt) zrobił pełny round-trip w 25 min.
- **GBP** (n=2): fade „książkowego" run-upu z ostatnich 10–15 min. Grać tylko topowe odczyty.

---

## 3. Uniwersalne zasady wykonania (z liczb, nie z opinii)

1. **Nie gonić ekstremum 1. minuty** — wejście tam kosztowało 15–35 p oddania (NZ jobs pin 35 p).
2. **Stop ≥ wycinka + spread**: budżet na przeciwny knot w sekundach publikacji: NZD 2–18 p,
   AUD jobs 8–12 p, FOMC/NZ jobs do 20–35 p. Ciaśniejszy stop ginie nawet przy dobrym kierunku.
   (BTMM: stop 7–23 p za ekstremum manipulacji; typowa „igła" 15 p.)
3. **Wyjścia per typ eventu**: FOMC/US CPI/CAD dane — szczyt 4–6 min, po 10 min mniej niż po 5;
   NZ GDP — nie ścinać przed 2. nogą (7–10 min); RBNZ holdy — 2. noga po 30–90 min (trailing zamiast TP);
   NOK — 1–2 świece; SEK — do ~10 min. Globalne `exit_minutes=10` to rozsądny kompromis,
   ale smart-exit może korzystać z playbooków (przyszły krok).
4. **Skipy systematyczne**: konflikt core-vs-headline (NZ retail, US CPI), konflikt headline-vs-stopa
   bezrobocia (AU/NZ jobs — chyba że gramy wg stopy), CAD jobs w dzień NFP bez zgodności obu printów,
   zaskoczenie <±5K na CAD jobs, mieszane wiersze przy AU retail.
5. **Konferencje = osobne eventy**: FOMC +30 min, RBA +60 min (od 2024 każda!), RBNZ MPS +60 min,
   BoC w dni MPR. Pozycja z komunikatu zamknięta/zabezpieczona przed konferencją.
6. **EUR/JPY ignorować** (wytyczne Roberta — brak reakcji nawet na topowe odczyty).

---

## 4. Weryfikacja kalendarzowa i transfer 2019→2026

Fact-check 8 kluczowych odczytów z adnotacji: **8/8 potwierdzone** u źródeł (RBNZ 08.05.2019 cięcie do 1,50%;
szok -50 pb 07.08.2019; FOMC 31.07.2019 pierwsze cięcie od 2008; NZ CPI Q4'18 +0,1%; AU jobs V'19 +42,3K
vs 16K; CAD jobs IV'19 +106,5K vs ~10–12K — rekord od 1976; NZ GDP Q3'18 0,3% vs 0,6%; NZ jobs Q3'18 +1,1%
vs ~0,5%). Adnotacje zespołu są wiarygodne (2 pliki z błędną nazwą roku, 1 screen z błędną etykietą —
odfiltrowane w datasecie).

**Terminy 2026 (UTC, lato):** RBNZ 02:00 (7×/rok; najbliższe: 2.09, 28.10, 9.12) · dane NZ 22:45 dnia
poprzedniego · RBA 04:30 + konferencja 05:30 (8×/rok; 11.08, 29.09, 3.11, 8.12) · AU dane 01:30 ·
BoC 13:45 (2.09, 28.10, 9.12; **15.07 dziś — decyzja z MPR!**) · dane CAD 12:30 · FOMC 18:00 + konferencja
18:30 (28–29.07, 15–16.09, 27–28.10, 8–9.12) · US CPI/NFP 12:30. Zimą +1h (NZ: 21:45).

**Zmiany strukturalne, które unieważniają część historii:**
- **AU Retail Sales nie istnieje** (koniec VI 2025) → następca „Household Spending m/m" (cienka historia).
- **AU CPI od XI 2025 miesięczne** (ostatnia środa miesiąca) — stary „kwartalny ładunek" rozcieńczony 4→12 publikacji.
- **RBA po reformie 2024**: 8 posiedzeń, decyzja 14:30 dnia 2., konferencja zawsze — playbooki
  „pierwszowtorkowe" bez konferencji są nieaktualne w części wykonawczej.
- **BoC 09:45 ET** (w 2019: 10:00 ET) — 15 min przesunięcia vs stare wykresy.
- **Reżim 2026 jest jastrzębi** (Fed 3,50–3,75 z dotsami ku podwyżce; **RBNZ podniósł 08.07.2026 do 2,50%**;
  RBA 4,35 po +75 pb podwyżek). Statystyki 2018–19 kodują gołębią asymetrię — kierunkową część
  (np. „hold RBA = relief w górę") należy LUSTRZANIE przeważyć, nie kopiować. Kształty ruchów
  (wycinki, fade ostatnich świec, timing szczytów) są bardziej uniwersalne niż kierunki.

---

## 5. Co zostało wdrożone w systemie (dziś)

1. **`python/knowledge/event_playbooks.json` — 30 wpisów** (eventowe + aliasy komunikatów
   + 7 fallbacków walutowych: NZD/AUD/CAD/USD/GBP/NOK/SEK). Plik jest **trackowany w git**
   (`logs/` jest ignorowane i nie trafiało do ZIP-a wdrożeniowego — poprawka po code review);
   nadal hot-reload po mtime, **bez restartu**; sekcja EVENT PLAYBOOK trafia do promptu tylko
   gdy jest dopasowanie. Znane ograniczenie: generyczny klucz „Interest Rate Decision" łapie
   tylko dokładnie tę nazwę — tytuły z prefiksem banku (np. „RBNZ Interest Rate Decision"
   z feedów nie-FF) spadają na fallback walutowy.
2. **Prompt LLM wzmocniony** (`llm_decision_engine.py`):
   - punkt (b): jakościowa reguła fade'u ostatnich świec + konfluencja (stretch z surowych
     świec M1) + hierarchia „fundament > technika przy REALNYM zaskoczeniu na stopach";
     zmierzone liczby per event celowo pozostają WYŁĄCZNIE w playbookach (hot-edytowalne,
     bez dublowania w kodzie);
   - punkt (d): budżet stopa na wycinkę w sekundach publikacji — rozmiary z sekcji EVENT
     PLAYBOOK, przy dwóch szacunkach brać większy;
   - nowy clamp/koercja pól decyzji LLM (`_num`): confidence 0–1, lot ≤85, exit 5–15,
     SL 25–80 i TP 30–120 (gdy >0) — śmieciowa/spoza kontraktu wartość nie dojedzie do EA.
3. **Dataset badawczy**: `research/screens/extracted_dataset.json` (121 rekordów z pełnymi polami:
   a/f/p, pipsy 1/5/10, kształt, dryf, ostatnie świece, pułapki) — gotowy do przyszłych backtestów.
4. **Testy: 233/233 pass** po zmianach.

## 6. Rekomendowane następne kroki (niewdrożone — do decyzji)

1. **Smart-exit z playbooków**: exit_decision_engine mógłby czytać te same wpisy (np. FOMC → nie
   trzymać do konferencji; NZ GDP → czekać na 2. nogę; NOK → 1–2 świece).
2. **Pre-release technicals w prompcie liczone serwerowo**: BB(20,2) i RSI(14) z M1 pushowanych przez EA
   + flaga kierunku ostatnich 3 świec — LLM dostałby gotową konfluencję zamiast liczyć ją z surowych świec.
3. **AUDNZD jako para wykonawcza dla eventów NZD/AUD** (Robert: główna para zespołu; jobs 3–4× większy
   ruch): wymaga wykresu+EA i sprawdzenia spreadu newsowego u brokera (zespół widział 3,1 pkt na NZ jobs).
4. **Zbieranie własnych reakcji 2026** (`/api/event-reaction` już działa): po ~20–30 eventach zweryfikować,
   czy proporcje z 2018–19 (fade %, timing szczytów) trzymają się w reżimie jastrzębim, i zaktualizować wpisy.
5. **NFP**: rozważyć obniżony mnożnik lota lub skip (BTMM blacklist + nasza próbka „najmniej przewidywalny USD").
6. **Household Spending m/m i AU CPI miesięczne**: zbierać reakcje od zera (wpisy playbooka już to sygnalizują).

## 7. Ograniczenia badania

- Próbka 2017–2020, głównie 2018–19 (reżim łagodzenia, niska zmienność) — kierunki wymagają lustrzanego
  przeważenia w 2026; kształty/timing przenoszą się lepiej.
- n per event: 1 (BoC, AU CPI) do 12 (CAD jobs) — częściowo anegdota, nie statystyka; konfluencja 19/20
  liczona na 20 obserwacjach z arkusza (możliwy bias selekcji screenów przez zespół).
- Adnotacje pipsów: zwykle 5-cyfrowe POINTS bez przecinka („1-336" = 33,6 p) — przeliczone; część
  wykresów bez osi cen = szacunki proporcjonalne (oznaczone w datasecie).
- Wykresy M5 (CAD 2018) nie rozstrzygają timingu 1-minutowego; 4 pliki „employment" z zip1/zip2 to
  w rzeczywistości piątki CPI/retail (odfiltrowane w statystykach jobs).
- Screen „nzd zatrudnienie 6.02.2019" to reakcja na wystąpienie Lowe (RBA), nie NZ jobs — wykluczony.
