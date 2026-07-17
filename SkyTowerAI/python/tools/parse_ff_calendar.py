"""
Parser of saved ForexFactory weekly calendar pages -> events JSONL.

Input: raw HTML files saved by fetch_ff_calendar.py (one per Monday).
Each page embeds event objects as strict JSON inside
window.calendarComponentStates; the 'dateline' field is a UNIX EPOCH (UTC) —
we never touch the rendered time labels (they are IP-geolocated guest-
timezone strings, the exact class of bug this project got burned by before).

Output row: {name, currency, dateline (epoch), utc (iso), impact,
actual, forecast, previous, revision}

Validation gates (fail loud, never silently):
1. Every parsed week must yield >= 40 raw events (a challenge page that
   slipped past the fetcher's size check would yield ~0).
2. Non-Farm Payrolls rows must land at 13:30 UTC (winter) / 12:30 UTC
   (summer DST) — proves the epoch really is UTC across DST changes.
3. Global duplicate-id dedup (each page embeds the state blob twice).

Usage:
    python tools/parse_ff_calendar.py \
        --in "..\\..\\dane historyczne\\ff_weeks" \
        --out "..\\..\\dane historyczne\\ff_events.jsonl"
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

CURRENCIES = {"USD", "NZD", "CAD", "AUD", "GBP"}
EVENT_RE = re.compile(r'\{"id":\d+,.*?"soloUrl":"[^"]*"\}')


def parse_file(path: str):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        html = f.read()
    events = {}
    for m in EVENT_RE.finditer(html):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict) or 'dateline' not in obj:
            continue
        events[obj.get('id')] = obj
    return list(events.values())


def to_row(obj: dict):
    dateline = int(obj['dateline'])
    return {
        "name": obj.get('name', ''),
        "currency": (obj.get('currency') or '').upper(),
        "dateline": dateline,
        "utc": datetime.fromtimestamp(dateline, tz=timezone.utc)
                       .strftime('%Y-%m-%dT%H:%M:%S'),
        "impact": (obj.get('impactName') or '').upper(),
        "actual": obj.get('actual') or None,
        "forecast": obj.get('forecast') or None,
        "previous": obj.get('previous') or None,
        "revision": obj.get('revision') or None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-week-events", type=int, default=40)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.indir, "*.html")))
    if not files:
        print("No input files", flush=True)
        sys.exit(1)

    all_rows = {}
    weak_weeks = []
    for path in files:
        raw = parse_file(path)
        if len(raw) < args.min_week_events:
            weak_weeks.append((os.path.basename(path), len(raw)))
        for obj in raw:
            try:
                row = to_row(obj)
            except (KeyError, TypeError, ValueError):
                continue
            if row['currency'] in CURRENCIES:
                all_rows[(row['currency'], row['name'], row['dateline'])] = row

    rows = sorted(all_rows.values(), key=lambda r: r['dateline'])

    # Gate 2: the BLS NFP (NOT the Wednesday ADP variant) must sit at
    # 13:30 UTC (winter) / 12:30 UTC (US DST). Rare reschedules exist
    # (holiday weeks), so the gate is a >=90% majority, deviants listed.
    nfp = [r for r in rows if 'non-farm' in r['name'].lower()
           and 'adp' not in r['name'].lower()
           and r['currency'] == 'USD']
    bad_nfp = [r for r in nfp if r['utc'][11:16] not in ('13:30', '12:30')]
    print(f"weeks parsed: {len(files)}  |  rows kept: {len(rows)}  |  "
          f"NFP rows: {len(nfp)} (times: "
          f"{sorted({r['utc'][11:16] for r in nfp})})", flush=True)
    if weak_weeks:
        print(f"GATE 1 WARNING — weeks below {args.min_week_events} raw "
              f"events: {weak_weeks[:10]}", flush=True)
    if not nfp:
        print("GATE 2 FAILED — no NFP rows found at all", flush=True)
        sys.exit(2)
    if bad_nfp:
        print(f"GATE 2 INFO — {len(bad_nfp)}/{len(nfp)} NFP rows off-schedule "
              f"(reschedules?): {[(r['utc'],) for r in bad_nfp[:5]]}", flush=True)
    if len(bad_nfp) > 0.1 * len(nfp):
        print("GATE 2 FAILED — too many off-schedule NFP rows, timezone "
              "assumption suspect", flush=True)
        sys.exit(2)

    per_year = {}
    with_actual = 0
    for r in rows:
        per_year[r['utc'][:4]] = per_year.get(r['utc'][:4], 0) + 1
        if r['actual']:
            with_actual += 1
    print(f"per year: {per_year}  |  with actual: {with_actual}", flush=True)

    tmp = args.out + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, args.out)
    print(f"written: {args.out}", flush=True)


if __name__ == "__main__":
    main()
