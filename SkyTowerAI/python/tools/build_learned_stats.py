"""
Learned-stats aggregator (F3): event path records -> knowledge/learned_stats.json

Merges the historical bootstrap (knowledge/historical_paths.jsonl.gz) with the
live recorder's output (logs/event_paths.jsonl), deduplicates by
(event_key, pair) with LIVE records winning, attributes shared same-minute
paths to the DOMINANT event of the release bundle, and computes per
(event, pair) frequency statistics: medians + IQR of absolute moves, adverse
first-5-min wick, 5->30 min continuation rate, fade-of-pre-drift rate,
surprise-direction hit rates (in CURRENCY-strength terms, base/quote aware)
and per-regime splits. Every number ships with its sample size — the prompt
renderer must always show n= and must not render thin samples.

Why attribution matters: 48.7% of path records share their measured path with
at least one other event released the same minute on the same pair (CPI m/m +
Core CPI + CPI y/y bundles; NFP + CAD Employment Change on USDCAD). Counting
the same path once per event would let Unemployment Claims "inherit" the CPI
tail. Rule: within each (pair, minute) group the dominant event (impact,
then event family, then unmodified-name preference) claims the path; the
other members are recorded as bundle aliases so the decision engine can still
find the bundle's stats when analyzing a non-dominant member. A dominance TIE
between DIFFERENT currencies (e.g. two unrelated LOW events) is ambiguous —
the whole group is excluded from per-event stats and counted in _meta.

The server never imports this module (tools/ is offline); it only reads the
JSON output, which is hot-reloaded by mtime in llm_decision_engine.

Usage (from SkyTowerAI/python):
    python tools/build_learned_stats.py            # default in/out paths
    python tools/build_learned_stats.py --out knowledge/learned_stats.json

LESSON (2026-07-17): never pipe this through `head` — a SIGPIPE can kill the
build mid-write. The tool therefore re-reads and content-checks its own
output after os.replace, and prints the verification inline.
"""
import argparse
import gzip
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from event_cluster import dominance_rank, family_rank  # noqa: F401 (family_rank re-exported)

SCHEMA_VERSION = 1

# A |move| below this is flat noise — excluded from directional rates
MIN_MOVE_PIPS = 2.0
# Currency-strength moves smaller than this don't count as "up"/"down"
MIN_DIRECTIONAL_PIPS = 1.0
# Emit a (event, pair) block only from this many measured releases
EMIT_MIN_N = 3
# Bundle alias: emitted when the member's own clean sample is thinner than
# this AND it co-released with the dominant at least ALIAS_MIN_BUNDLED times
ALIAS_MIN_OWN = 8
ALIAS_MIN_BUNDLED = 5

_PARAMS = {
    "min_move_pips": MIN_MOVE_PIPS,
    "min_directional_pips": MIN_DIRECTIONAL_PIPS,
    "emit_min_n": EMIT_MIN_N,
    "alias_min_own": ALIAS_MIN_OWN,
    "alias_min_bundled": ALIAS_MIN_BUNDLED,
}

# Dominance ordering lives in event_cluster.py — the SAME ranking decides
# which member of a same-minute release the live updater analyzes and labels
# the trade with. Two copies would let the decision be filed under one name
# while its base rates are bundled under another.
def dominance_key(record):
    """Sort key: lower = more dominant. The trailing name keeps same-currency
    bundle resolution deterministic (canonical member = first alphabetically
    among unmodified names)."""
    return dominance_rank(record.get("event_name_normalized", ""),
                          record.get("impact", ""))


def event_stat_key(record) -> str:
    return f"{record.get('currency', '')}|{record.get('event_name_normalized', '')}"


# ----------------------------------------------------------------------
# Loading + dedup
# ----------------------------------------------------------------------

def _iter_jsonl(path, open_fn):
    with open_fn(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def load_records(historical_path, live_path):
    """All measurable records, deduped by (event_key, pair), live wins.
    Returns (records, counters)."""
    dropped = Counter()
    by_key = {}
    sources = Counter()

    def admit(rec, source_rank):
        if rec.get("test"):
            dropped["test"] += 1
            return
        if (rec.get("impact") or "").upper() == "HOLIDAY":
            dropped["holiday"] += 1
            return
        if not rec.get("pair") or rec.get("data_status") == "no_data":
            dropped["no_data"] += 1
            return
        if rec.get("move_1min_pips") is None:
            dropped["no_path"] += 1
            return
        key = (rec.get("event_key"), rec.get("pair"))
        prev = by_key.get(key)
        # source_rank: live=1 outranks historical=0; same rank keeps first
        if prev is None or source_rank > prev[0]:
            by_key[key] = (source_rank, rec)

    if historical_path and os.path.exists(historical_path):
        for rec in _iter_jsonl(historical_path,
                               lambda p: gzip.open(p, "rt", encoding="utf-8")):
            sources["historical"] += 1
            admit(rec, 0)
    if live_path and os.path.exists(live_path):
        for rec in _iter_jsonl(live_path,
                               lambda p: open(p, "r", encoding="utf-8")):
            sources["live"] += 1
            admit(rec, 1)

    records = [rec for _, rec in by_key.values()]
    return records, {"sources": dict(sources), "dropped": dropped}


# ----------------------------------------------------------------------
# Attribution (dominant event claims the shared path)
# ----------------------------------------------------------------------

def attribute_records(records):
    """Group by (pair, release minute); the dominant record keeps the path,
    non-dominant bundle members are counted for aliases, ambiguous groups
    (cross-currency dominance tie) are excluded entirely.

    Returns (attributed_records, bundled_under, info) where bundled_under is
    {member_event_key: Counter({dominant_event_key: joint_RELEASES})} —
    release-level (distinct minutes), not record-level, so the count reads
    as "co-released N times" regardless of how many pairs measured it."""
    groups = defaultdict(list)
    for rec in records:
        groups[(rec.get("pair"), (rec.get("event_time") or "")[:16])].append(rec)

    attributed = []
    joint_releases = set()   # (member_key, dominant_key, minute)
    info = Counter()
    for (_, minute), members in groups.items():
        if len(members) == 1:
            attributed.append(members[0])
            continue
        ranked = sorted(members, key=dominance_key)
        top = ranked[0]
        top_prefix = dominance_key(top)[:3]
        # ANY equally-dominant member of a DIFFERENT currency makes the group
        # ambiguous — checking only the runner-up is not enough, because
        # alphabetical name ordering can hide the foreign tie behind
        # same-currency bundle members
        if any(dominance_key(m)[:3] == top_prefix
               and m.get("currency") != top.get("currency")
               for m in ranked[1:]):
            info["ambiguous_groups"] += 1
            info["ambiguous_records"] += len(members)
            continue
        attributed.append(top)
        info["bundle_groups"] += 1
        for member in ranked[1:]:
            info["bundled_records"] += 1
            joint_releases.add((event_stat_key(member), event_stat_key(top),
                               minute))
    # Sorted iteration keeps Counter insertion order (and therefore
    # most_common tie-breaks) deterministic across runs — the output is a
    # git-tracked file and must rebuild reproducibly
    bundled_under = defaultdict(Counter)
    for member_key, dominant_key, _ in sorted(joint_releases):
        bundled_under[member_key][dominant_key] += 1
    return attributed, bundled_under, info


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------

def _dist(values, tail=False):
    """{"median", "p25", "p75", "n"} of a numeric list (None -> omitted).
    tail=True adds p80/p90 (n>=10) — stop budgets must survive the tail wick,
    not the median one, and take-profits anchor on the p80 favorable run."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    out = {"median": round(statistics.median(vals), 1), "n": len(vals)}
    if len(vals) >= 2:
        q1, _, q3 = statistics.quantiles(vals, n=4, method="inclusive")
        out["p25"] = round(q1, 1)
        out["p75"] = round(q3, 1)
    if tail and len(vals) >= 10:
        deciles = statistics.quantiles(vals, n=10, method="inclusive")
        out["p80"] = round(deciles[7], 1)
        out["p90"] = round(deciles[8], 1)
    return out


def _rate(hits, n):
    return {"rate": round(hits / n, 2), "n": n} if n else None

def _sign(v):
    return (v > 0) - (v < 0)


def _currency_strength_move(rec, horizon="move_5min_pips"):
    """Move in CURRENCY-strength terms: positive = event currency stronger.
    Pair-price moves flip sign when the currency is the QUOTE side (e.g. USD
    events on EURUSD or AUDUSD)."""
    move = rec.get(horizon)
    if move is None:
        return None
    pair = rec.get("pair") or ""
    cur = rec.get("currency") or ""
    if len(pair) < 6 or cur not in (pair[:3], pair[3:6]):
        return None
    return move if pair[:3] == cur else -move


def _pair_stats(recs):
    """Frequency stats for one (event, pair) sample of attributed releases."""
    out = {"n": len(recs)}

    for label, field in (("abs_move_1min", "move_1min_pips"),
                         ("abs_move_5min", "move_5min_pips"),
                         ("abs_move_15min", "move_15min_pips"),
                         ("abs_move_30min", "move_30min_pips")):
        dist = _dist([abs(r[field]) for r in recs if r.get(field) is not None])
        if dist:
            out[label] = dist

    # Adverse wick: the counter-excursion within the first 5 min against the
    # eventual 5-min direction — what a stop must survive at the release
    adverse = []
    ranges = []
    for r in recs:
        hi, lo, mv = r.get("first5_high_pips"), r.get("first5_low_pips"), r.get("move_5min_pips")
        if hi is None or lo is None:
            continue
        ranges.append(hi - lo)
        if mv is None or abs(mv) < MIN_MOVE_PIPS:
            continue
        adverse.append(max(0.0, -lo) if mv > 0 else max(0.0, hi))
    if adverse:
        out["adverse_wick_5min"] = _dist(adverse, tail=True)
    if ranges:
        out["range_5min"] = _dist(ranges)

    # Favorable run: the excursion WITH the eventual direction — what a
    # take-profit can realistically capture when the direction call is right.
    # Mirrors the adverse wick above: the window extreme on the move's own
    # side, conditional on that window's |move| clearing the noise gate.
    # 5-min matches the shortest hold; 30-min is the ceiling for 5-15 min
    # holds (an extreme printed at T+29 may be unreachable earlier).
    fav5 = []
    for r in recs:
        hi, lo, mv = (r.get("first5_high_pips"), r.get("first5_low_pips"),
                      r.get("move_5min_pips"))
        if hi is None or lo is None or mv is None or abs(mv) < MIN_MOVE_PIPS:
            continue
        fav5.append(hi if mv > 0 else -lo)
    if fav5:
        out["favorable_run_5min"] = _dist(fav5, tail=True)
    fav30 = []
    for r in recs:
        hi, lo, mv = (r.get("high_30min_pips"), r.get("low_30min_pips"),
                      r.get("move_30min_pips"))
        if hi is None or lo is None or mv is None or abs(mv) < MIN_MOVE_PIPS:
            continue
        fav30.append(hi if mv > 0 else -lo)
    if fav30:
        out["favorable_run_30min"] = _dist(fav30, tail=True)

    # Continuation: does the 5-min direction still hold at 30 min?
    cont = [(r["move_5min_pips"], r["move_30min_pips"]) for r in recs
            if r.get("move_5min_pips") is not None
            and r.get("move_30min_pips") is not None
            and abs(r["move_5min_pips"]) >= MIN_MOVE_PIPS]
    if cont:
        out["continuation_5to30"] = _rate(
            sum(1 for m5, m30 in cont if _sign(m30) == _sign(m5)), len(cont))

    # Fade: release move against the last-3-min pre-release drift
    fade = [(r["pre_release_3m_pips"], r["move_5min_pips"]) for r in recs
            if r.get("pre_release_3m_pips") is not None
            and r.get("move_5min_pips") is not None
            and abs(r["pre_release_3m_pips"]) >= MIN_MOVE_PIPS
            and abs(r["move_5min_pips"]) >= MIN_MOVE_PIPS]
    if fade:
        out["fade_pre_drift"] = _rate(
            sum(1 for pre, m5 in fade if _sign(m5) != _sign(pre)), len(fade))

    # Surprise-direction hit rates, currency-strength terms
    beat, miss = [], []
    for r in recs:
        cs = _currency_strength_move(r)
        if cs is None or abs(cs) < MIN_DIRECTIONAL_PIPS:
            continue
        if r.get("surprise") == "BEAT":
            beat.append(cs)
        elif r.get("surprise") == "MISS":
            miss.append(cs)
    if beat:
        out["beat_currency_up_5min"] = _rate(sum(1 for c in beat if c > 0), len(beat))
    if miss:
        out["miss_currency_down_5min"] = _rate(sum(1 for c in miss if c < 0), len(miss))

    # Regime split: the same event often reacts OPPOSITE ways per cycle
    by_regime = {}
    for regime in ("hiking", "cutting", "hold"):
        sub = [r for r in recs if r.get("regime") == regime]
        if len(sub) < EMIT_MIN_N:
            continue
        entry = {"n": len(sub)}
        dist = _dist([abs(r["move_5min_pips"]) for r in sub
                      if r.get("move_5min_pips") is not None])
        if dist:
            entry["abs_move_5min"] = dist
        r_beat = [c for c in (_currency_strength_move(r) for r in sub
                              if r.get("surprise") == "BEAT")
                  if c is not None and abs(c) >= MIN_DIRECTIONAL_PIPS]
        if r_beat:
            entry["beat_currency_up_5min"] = _rate(
                sum(1 for c in r_beat if c > 0), len(r_beat))
        r_miss = [c for c in (_currency_strength_move(r) for r in sub
                              if r.get("surprise") == "MISS")
                  if c is not None and abs(c) >= MIN_DIRECTIONAL_PIPS]
        if r_miss:
            entry["miss_currency_down_5min"] = _rate(
                sum(1 for c in r_miss if c < 0), len(r_miss))
        by_regime[regime] = entry
    if by_regime:
        out["by_regime"] = by_regime
    return out


def build_stats(records):
    """Full pipeline over deduped records -> learned_stats dict (sans _meta
    sources). Pure — unit-testable without files."""
    attributed, bundled_under, attr_info = attribute_records(records)

    by_event = defaultdict(list)
    for rec in attributed:
        by_event[event_stat_key(rec)].append(rec)

    events = {}
    for key, recs in by_event.items():
        if all(r.get("non_data") for r in recs):
            continue  # speeches/pressers are never tradeable -> never looked up
        release_minutes = {(r.get("event_time") or "")[:16] for r in recs}
        by_pair = defaultdict(list)
        for r in recs:
            by_pair[r["pair"]].append(r)
        pairs = {pair: _pair_stats(sub) for pair, sub in by_pair.items()
                 if len(sub) >= EMIT_MIN_N}
        if not pairs:
            continue

        entry = {
            "event_name": Counter(r.get("event_name") for r in recs).most_common(1)[0][0],
            "currency": recs[0].get("currency"),
            "n_releases": len(release_minutes),
            "span": [min(release_minutes)[:7], max(release_minutes)[:7]],
            "pairs": pairs,
        }

        # Surprise sigma over UNIQUE releases (records duplicate one release
        # across pairs) — the standardized-surprise denominator per event type
        mags = {}
        for r in recs:
            if r.get("surprise_magnitude") is not None:
                mags[(r.get("event_time") or "")[:16]] = r["surprise_magnitude"]
        if len(mags) >= 10:
            entry["surprise_sigma"] = round(statistics.pstdev(list(mags.values())), 4)
            entry["surprise_sigma_n"] = len(mags)

        bundled_with = Counter()
        for member_key, doms in bundled_under.items():
            if key in doms:
                bundled_with[member_key] += doms[key]
        if bundled_with:
            entry["bundled_with"] = [k.split("|", 1)[1]
                                     for k, _ in bundled_with.most_common(3)]
        events[key] = entry

    # Aliases: thin members point at the bundle dominant they usually
    # co-release with, so the engine can fall back to the bundle's stats
    aliases = {}
    for member_key, doms in bundled_under.items():
        own_n = events.get(member_key, {}).get("n_releases", 0)
        dominant, joint = doms.most_common(1)[0]
        if own_n < ALIAS_MIN_OWN and joint >= ALIAS_MIN_BUNDLED and dominant in events:
            aliases[member_key] = {"to": dominant, "n": joint}

    return {
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "params": dict(_PARAMS),
            "attribution": dict(attr_info),
            "n_events": len(events),
        },
        "events": events,
        "bundle_alias": aliases,
    }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--historical",
                    default=os.path.join(base, "knowledge", "historical_paths.jsonl.gz"))
    ap.add_argument("--live",
                    default=os.path.join(base, "logs", "event_paths.jsonl"))
    ap.add_argument("--out",
                    default=os.path.join(base, "knowledge", "learned_stats.json"))
    args = ap.parse_args()

    records, load_info = load_records(args.historical, args.live)
    print(f"input: {load_info['sources']}  dropped: {dict(load_info['dropped'])}",
          flush=True)
    if not records:
        print("FATAL: no usable records — refusing to write an empty stats file",
              flush=True)
        sys.exit(1)

    stats = build_stats(records)
    stats["_meta"]["built_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats["_meta"]["sources"] = load_info["sources"]
    stats["_meta"]["input_records_deduped"] = len(records)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, sort_keys=True, indent=1)

    # Verify the TMP content BEFORE it can clobber a previous good file — a
    # wrong/truncated input can load records yet emit ZERO events, and that
    # must never replace knowledge the live server is reading. Explicit check,
    # not assert (asserts vanish under python -O).
    with open(tmp, "r", encoding="utf-8") as f:
        written = json.load(f)
    ev = written.get("events", {})
    if written.get("_meta", {}).get("schema_version") != SCHEMA_VERSION or not ev:
        os.remove(tmp)
        print("FATAL: output verification failed (no events) — previous stats "
              "file left untouched", flush=True)
        sys.exit(1)
    os.replace(tmp, args.out)
    biggest = sorted(ev.items(), key=lambda kv: -kv[1]["n_releases"])[:5]
    print(f"written: {args.out}  events={len(ev)} "
          f"aliases={len(written.get('bundle_alias', {}))} "
          f"size={os.path.getsize(args.out)}B", flush=True)
    for key, entry in biggest:
        pair, pstats = max(entry["pairs"].items(), key=lambda kv: kv[1]["n"])
        med = (pstats.get("abs_move_5min") or {}).get("median")
        print(f"  {key}: releases={entry['n_releases']} span={entry['span']} "
              f"{pair} n={pstats['n']} abs_move5_median={med}", flush=True)


if __name__ == "__main__":
    main()
