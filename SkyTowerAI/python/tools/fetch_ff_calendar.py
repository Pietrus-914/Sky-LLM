"""
Resumable fetcher of ForexFactory WEEKLY calendar pages (historical bootstrap).

Each week page embeds the full event data as strict JSON inside
window.calendarComponentStates — including a unix-epoch 'dateline' (UTC!),
actual, forecast, previous and revision. We save the RAW HTML per week and
parse separately (parse_ff_calendar.py), so a parsing bug never forces a
re-download.

Etiquette: one request per ~8-12s (randomized), immediate disk write,
skip-if-present resume, long backoff on any rate-limit/challenge response.
A full 2021->2026 run is ~289 pages / ~1-1.5h.

Usage:
    python tools/fetch_ff_calendar.py --out "..\\..\\dane historyczne\\ff_weeks" \
        --start 2021-01-04 --end 2026-07-13
"""
import argparse
import os
import random
import sys
import time
from datetime import date, timedelta

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")
# A real week page is ~350-480KB and contains the state blob; anything small
# or without the marker is a challenge/rate-limit page and must not be kept
MIN_VALID_BYTES = 100_000
VALIDITY_MARKER = "calendarComponentStates"
BACKOFF_SECONDS = 600
MAX_ATTEMPTS = 3


def mondays(start: date, end: date):
    d = start
    while d.weekday() != 0:
        d += timedelta(days=1)
    while d <= end:
        yield d
        d += timedelta(days=7)


def week_param(d: date) -> str:
    return f"{MONTHS[d.month - 1]}{d.day}.{d.year}"


def fetch_week(session: requests.Session, d: date) -> bytes:
    url = f"https://www.forexfactory.com/calendar?week={week_param(d)}"
    resp = session.get(url, timeout=40)
    body = resp.content
    if resp.status_code != 200 or len(body) < MIN_VALID_BYTES \
            or VALIDITY_MARKER.encode() not in body:
        raise RuntimeError(f"invalid response: HTTP {resp.status_code}, "
                           f"{len(body)} bytes")
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default=date.today().isoformat())
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    session = requests.Session()
    session.headers.update({"User-Agent": UA,
                            "Accept-Language": "en-US,en;q=0.9",
                            "Accept-Encoding": "gzip, deflate"})

    todo = list(mondays(start, end))
    done = failed = skipped = 0
    for i, d in enumerate(todo):
        out_path = os.path.join(args.out, f"{d.isoformat()}.html")
        if os.path.exists(out_path) and os.path.getsize(out_path) >= MIN_VALID_BYTES:
            skipped += 1
            continue

        ok = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                body = fetch_week(session, d)
                tmp = out_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, out_path)
                done += 1
                ok = True
                print(f"[{i + 1}/{len(todo)}] {d} OK ({len(body)} B)", flush=True)
                break
            except Exception as e:
                print(f"[{i + 1}/{len(todo)}] {d} attempt {attempt} FAILED: {e}",
                      flush=True)
                if attempt < MAX_ATTEMPTS:
                    print(f"  backoff {BACKOFF_SECONDS}s...", flush=True)
                    time.sleep(BACKOFF_SECONDS)
        if not ok:
            failed += 1

        time.sleep(random.uniform(8.0, 12.0))

    print(f"DONE: fetched {done}, skipped {skipped} (already on disk), "
          f"failed {failed} of {len(todo)} weeks", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
