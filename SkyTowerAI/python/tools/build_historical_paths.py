"""
Historical bootstrap builder: FF calendar events + HistData M1 bars ->
knowledge/historical_paths.jsonl.gz — the same schema and the SAME
measurement function (event_path_recorder.measure_path) as the live
recorder, so historical and live statistics are directly comparable.

Inputs:
- ff_events.jsonl from parse_ff_calendar.py (dateline = UTC epoch)
- HistData zips from fetch_histdata.py (M1 CSV). TIMEZONE, verified
  empirically against NFP release bars in BOTH DST seasons: the files are
  US Eastern WITH DST (winter bar 08:30 = 13:30 UTC, summer bar 08:30 =
  12:30 UTC) — despite HistData's own docs claiming fixed GMT-5. The
  conversion below is DST-aware; do NOT "simplify" it to a fixed shift.

Regime tags: replayed through RegimeTracker's deterministic rule — rate
decisions walked chronologically per currency, each event stamped with the
regime as of ITS time. Seed: all currencies "hold" at 2021-01-01 (the
pandemic zero-rate era), before the first observed decision.

Output ships in git (knowledge/) and rides the deploy ZIP unchanged —
history never refreshes; the live recorder extends the dataset from deploy
day onward, and aggregation dedupes any overlap by (event_key, pair) with
live records winning.

Usage (from SkyTowerAI/python):
    python tools/build_historical_paths.py \
        --events "..\\..\\dane historyczne\\ff_events.jsonl" \
        --bars "..\\..\\dane historyczne\\histdata" \
        --out knowledge/historical_paths.jsonl.gz
"""
import argparse
import bisect
import glob
import gzip
import io
import json
import os
import sys
import zipfile
from array import array
from calendar import timegm
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from event_path_recorder import measure_path
from event_reaction_history import (normalize_event_name, classify_surprise,
                                    surprise_magnitude)
from calendar_fetcher import is_non_data_event
from market_context import pip_size
from regime_tracker import RegimeTracker, is_rate_decision

import pytz

_NY = pytz.timezone("America/New_York")
_day_shift_cache = {}


def _ny_shift_seconds(y: int, m: int, d: int) -> int:
    """Seconds to ADD to a HistData file timestamp to get UTC (DST-aware,
    +5h winter / +4h summer). Cached per day; FX is closed across the
    2am-Sunday DST switches, so one offset per day is safe."""
    key = (y, m, d)
    shift = _day_shift_cache.get(key)
    if shift is None:
        local_noon = _NY.localize(datetime(y, m, d, 12, 0))
        shift = -int(local_noon.utcoffset().total_seconds())
        _day_shift_cache[key] = shift
    return shift


PRE_WINDOW = 240                    # bars needed before T0 (pre-release drift)
POST_WINDOW = 31 * 60               # ... and after (30-min horizons)
REGIME_SEED = {c: "hold" for c in ("USD", "NZD", "CAD", "AUD", "GBP")}


def load_events(path):
    events = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e['dateline'])
    return events


def load_pair_bars(bars_dir, pair):
    """All zips of one pair -> sorted parallel arrays (epoch, o, h, l, c)."""
    epochs = array('q')
    o, h, l, c = array('d'), array('d'), array('d'), array('d')
    for zpath in sorted(glob.glob(os.path.join(bars_dir, f"{pair}_M1_*.zip"))):
        with zipfile.ZipFile(zpath) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
            for name in csv_names:
                with io.TextIOWrapper(zf.open(name), encoding='ascii',
                                      errors='replace') as f:
                    for line in f:
                        parts = line.strip().split(';')
                        if len(parts) < 5:
                            continue
                        ts = parts[0]
                        try:
                            y, mo, d = int(ts[0:4]), int(ts[4:6]), int(ts[6:8])
                            epoch = timegm((y, mo, d, int(ts[9:11]),
                                            int(ts[11:13]), 0)) \
                                + _ny_shift_seconds(y, mo, d)
                            epochs.append(epoch)
                            o.append(float(parts[1]))
                            h.append(float(parts[2]))
                            l.append(float(parts[3]))
                            c.append(float(parts[4]))
                        except (ValueError, IndexError):
                            continue
    order = sorted(range(len(epochs)), key=epochs.__getitem__)
    return (array('q', (epochs[i] for i in order)),
            array('d', (o[i] for i in order)),
            array('d', (h[i] for i in order)),
            array('d', (l[i] for i in order)),
            array('d', (c[i] for i in order)))


def bar_map_around(bars, t0_min):
    epochs, o, h, l, c = bars
    lo = bisect.bisect_left(epochs, t0_min - PRE_WINDOW)
    hi = bisect.bisect_right(epochs, t0_min + POST_WINDOW)
    return {epochs[i]: {"open": o[i], "high": h[i], "low": l[i], "close": c[i]}
            for i in range(lo, hi)}


def build_regime_timeline(events):
    """Per currency: sorted [(epoch, regime)] replayed from rate decisions."""
    decisions = {c: [] for c in REGIME_SEED}
    timeline = {c: [(0, REGIME_SEED[c])] for c in REGIME_SEED}
    last_rate = {c: None for c in REGIME_SEED}
    for e in events:
        cur = e['currency']
        if cur not in decisions or not is_rate_decision(e['name']):
            continue
        actual = _num(e.get('actual'))
        if actual is None:
            continue
        previous = _num(e.get('previous'))
        if previous is None:
            previous = last_rate[cur]
        if previous is None:
            action = "hold"
        elif actual > previous:
            action = "hike"
        elif actual < previous:
            action = "cut"
        else:
            action = "hold"
        last_rate[cur] = actual
        decisions[cur].append({"date": e['utc'], "action": action,
                               "rate": actual})
        as_of = datetime.fromtimestamp(e['dateline'], tz=timezone.utc) \
                        .replace(tzinfo=None)
        regime = RegimeTracker._deterministic(decisions[cur], as_of=as_of)
        timeline[cur].append((e['dateline'], regime))
    return timeline


def _num(value):
    from event_reaction_history import parse_numeric
    return parse_numeric(value)


def regime_at(timeline, currency, epoch):
    """Regime STRICTLY BEFORE the given instant (bisect_left, not _right):
    a rate decision is stamped with the PRE-decision regime — matching the
    live recorder, which stamps at T+31min, before the tracker can observe
    the decision. Historical and live tags must bucket identically."""
    points = timeline.get(currency)
    if not points:
        return None
    idx = bisect.bisect_left([p[0] for p in points], epoch) - 1
    return points[idx][1] if idx >= 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--bars", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pairs", nargs="+",
                    default=["NZDUSD", "USDCAD", "AUDUSD", "GBPUSD", "EURUSD"])
    args = ap.parse_args()

    events = load_events(args.events)
    print(f"events loaded: {len(events)}", flush=True)
    timeline = build_regime_timeline(events)
    for cur, points in timeline.items():
        print(f"  regime timeline {cur}: {len(points) - 1} decisions, "
              f"now={points[-1][1]}", flush=True)

    built_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    records = []
    stats = {"measured": 0, "no_bars": 0, "partial": 0}

    # Pair-by-pair to keep memory to one pair's arrays at a time
    for pair in args.pairs:
        bars = load_pair_bars(args.bars, pair)
        if not bars[0]:
            print(f"{pair}: NO BARS FOUND — skipping", flush=True)
            continue
        first = datetime.fromtimestamp(bars[0][0], tz=timezone.utc)
        last = datetime.fromtimestamp(bars[0][-1], tz=timezone.utc)
        print(f"{pair}: {len(bars[0])} bars "
              f"({first:%Y-%m-%d} .. {last:%Y-%m-%d})", flush=True)

        for e in events:
            cur = e['currency']
            if cur not in (pair[:3], pair[3:6]):
                continue
            t0_min = (e['dateline'] // 60) * 60
            metrics = measure_path(bar_map_around(bars, t0_min), t0_min,
                                   pip_size(pair), allow_partial=True)
            if metrics is None:
                stats["no_bars"] += 1
                continue
            complete = metrics.pop("complete")
            if not complete:
                stats["partial"] += 1
            stats["measured"] += 1

            revision = e.get('revision')
            rev_mag = (surprise_magnitude(revision, e.get('previous'))
                       if revision else None)
            record = {
                "recorded_at": built_at,
                "source": "historical",
                "test": False,
                "event_key": (f"{cur}|{normalize_event_name(e['name'])}"
                              f"|{e['utc'][:16]}"),
                "event_name": e['name'],
                "event_name_normalized": normalize_event_name(e['name']),
                "currency": cur,
                "event_time": e['utc'],
                "impact": e.get('impact', ''),
                "forecast": e.get('forecast'),
                "previous": e.get('previous'),
                "actual": e.get('actual'),
                "surprise": classify_surprise(e.get('actual'),
                                              e.get('forecast')),
                "surprise_magnitude": surprise_magnitude(e.get('actual'),
                                                         e.get('forecast')),
                "non_data": is_non_data_event(e['name']),
                "regime": regime_at(timeline, cur, e['dateline']),
                "pair": pair,
                "broker_utc_offset_min": 0,   # HistData converted to UTC
                "data_status": "ok" if complete else "partial",
                "spread_points_t0": None,
                **metrics,
            }
            if revision:
                record["previous_revised"] = revision
                if rev_mag is not None:
                    record["revision_magnitude"] = rev_mag
            records.append(record)

    records.sort(key=lambda r: (r['event_time'], r['pair']))
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    tmp = args.out + ".tmp"
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, args.out)

    per_year = {}
    for r in records:
        per_year[r['event_time'][:4]] = per_year.get(r['event_time'][:4], 0) + 1
    print(f"records written: {len(records)} -> {args.out}", flush=True)
    print(f"stats: {stats}  |  per year: {per_year}", flush=True)


if __name__ == "__main__":
    main()
