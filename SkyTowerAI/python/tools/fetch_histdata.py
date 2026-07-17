"""
Resumable downloader of HistData.com free M1 bar archives (historical
bootstrap). Yearly zips for full past years, monthly zips for the current
year. HistData M1 timestamps are US Eastern WITH DST (verified empirically
against NFP bars in both seasons — their docs wrongly say fixed GMT-5);
build_historical_paths.py does the DST-aware UTC conversion, this script
only fetches raw zips.

Flow per file: GET the download page -> read the hidden form fields (tk
token etc.) -> POST to get.php with the page as Referer -> save zip.

Usage:
    python tools/fetch_histdata.py --out "..\\..\\dane historyczne\\histdata" \
        --pairs nzdusd usdcad audusd gbpusd eurusd \
        --years 2021 2022 2023 2024 2025 --months-year 2026 --months 1 2 3 4 5 6
"""
import argparse
import os
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://www.histdata.com"
PAGE = BASE + "/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes"
GET_PHP = BASE + "/get.php"
FORM_FIELDS = ("tk", "date", "datemonth", "platform", "timeframe", "fxpair")


def form_data(html: str) -> dict:
    """Hidden inputs of the #file_down form. Attribute order varies
    (name= id= value=), so allow anything between name and value."""
    form = re.search(r'<form[^>]*id="file_down".*?</form>', html, re.DOTALL)
    scope = form.group(0) if form else html
    data = {}
    for field in FORM_FIELDS:
        m = re.search(rf'name="{field}"[^>]*value="([^"]*)"', scope)
        if m:
            data[field] = m.group(1)
    missing = [f for f in FORM_FIELDS if f not in data]
    if missing:
        raise RuntimeError(f"download form incomplete, missing {missing}")
    return data


def download_one(session: requests.Session, page_url: str, out_path: str):
    page = session.get(page_url, timeout=40)
    if page.status_code != 200:
        raise RuntimeError(f"page HTTP {page.status_code}")
    data = form_data(page.text)
    resp = session.post(GET_PHP, data=data,
                        headers={"Referer": page_url}, timeout=120)
    body = resp.content
    if resp.status_code != 200 or not body.startswith(b"PK"):
        raise RuntimeError(f"not a zip: HTTP {resp.status_code}, "
                           f"{len(body)} bytes, head={body[:40]!r}")
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, out_path)
    return len(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--pairs", nargs="+", required=True)
    ap.add_argument("--years", nargs="+", type=int, default=[])
    ap.add_argument("--months-year", type=int, default=None)
    ap.add_argument("--months", nargs="+", type=int, default=[])
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    jobs = []
    for pair in args.pairs:
        for year in args.years:
            jobs.append((f"{PAGE}/{pair}/{year}",
                         f"{pair.upper()}_M1_{year}.zip"))
        if args.months_year:
            for month in args.months:
                jobs.append((f"{PAGE}/{pair}/{args.months_year}/{month}",
                             f"{pair.upper()}_M1_{args.months_year}"
                             f"{month:02d}.zip"))

    done = failed = skipped = 0
    for i, (url, name) in enumerate(jobs):
        out_path = os.path.join(args.out, name)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 100_000:
            skipped += 1
            continue
        try:
            size = download_one(session, url, out_path)
            done += 1
            print(f"[{i + 1}/{len(jobs)}] {name} OK ({size // 1024} KB)",
                  flush=True)
        except Exception as e:
            failed += 1
            print(f"[{i + 1}/{len(jobs)}] {name} FAILED: {e}", flush=True)
        time.sleep(4.0)

    print(f"DONE: fetched {done}, skipped {skipped}, failed {failed} "
          f"of {len(jobs)} files", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
