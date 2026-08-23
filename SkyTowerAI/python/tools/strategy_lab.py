"""
Strategy lab (offline): evaluate entry/exit strategy variants on the recorded
event paths (knowledge/historical_paths.jsonl.gz + logs/event_paths.jsonl)
for ONE instrument, over a grid of stop / target / hold parameters.

Why
---
The live system enters ~20 s BEFORE the print on a predicted direction and
exits 5-15 min after. Whether that is the right shape for a given instrument
(gold moves $8 in the first minute of CPI and then mostly holds; NZD jobs
drift for 30 min) is an empirical question the path dataset can answer
without touching the live code. This tool is the answer machine: it is
read-only, imports only the shared unit helpers, and the server never
imports it.

Strategies (all per release, direction d = +1 BUY / -1 SELL of the pair)
------------------------------------------------------------------------
* pre_oracle     — enter at T0 in the direction the SURPRISE implies
                   (actual vs forecast, currency-strength mapped onto the
                   pair, inverse-sense events handled). Upper bound of what
                   a perfect pre-release predictor could capture; the live
                   LLM cannot know the actual, so treat as a ceiling.
* pre_fade_drift — enter at T0 AGAINST the last-3-min pre-release drift
                   (needs |drift| >= --drift-min). No oracle: a real,
                   implementable pre-release rule.
* post_confirm   — enter at T+1 min in the direction of the first 1-min
                   candle when |move_1min| >= --confirm-min. No prediction
                   needed at all; pays the first minute to buy certainty.
* post_agree     — post_confirm AND the first candle agrees with the
                   surprise direction (oracle filter on top of confirmation;
                   in live use the "surprise" becomes the LLM's predicted
                   direction).
* post_fade      — enter at T+1 min AGAINST the first 1-min candle when
                   |move_1min| >= --confirm-min (mean-reversion of a
                   front-loaded print). No prediction needed.
* post_fade_5    — same, but enter at T+5 against the 5-min move (needs
                   |move_5min| >= --confirm-min); exits at 15/30 only.

Outcome model (conservative)
----------------------------
Only window extremes are stored (first5 high/low, 30-min high/low), not the
ordering of highs and lows. A stop is therefore assumed to be hit whenever
the adverse excursion within the horizon reaches it — even if the target
would have been reached first. A target counts only when the stop was NOT
reached. For post-release entries (T+1 / T+5) the window extremes also
cover the minutes BEFORE the entry, so the favorable excursion is taken
from the point samples after the entry only (move_5/15/30 relative to the
entry price) — a lower bound on the true MFE — while the adverse excursion
keeps the (pessimistic) window extremes. Every SL/TP expectancy is
therefore a LOWER bound; the "no SL / no TP" variant (timed exit) is exact
at the hold horizons the dataset measures (5, 15, 30 min).

Spread is charged once per trade (--spread-pips, default from the instrument
profile's typical_news_spread_pips; forex default 3).

Usage (from SkyTowerAI/python):
    python tools/strategy_lab.py --pair XAUUSD
    python tools/strategy_lab.py --pair XAUUSD --impact HIGH --min-n 20
    python tools/strategy_lab.py --pair XAUUSD --events "cpi m/m,non-farm employment change"
    python tools/strategy_lab.py --pair NZDUSD --json out.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instrument_profiles import normalize_root, profile_for  # noqa: E402

# Events whose "actual > forecast" is BAD for the currency (higher claims /
# unemployment = weaker currency). Only the oracle strategies use this.
INVERSE_SENSE = (
    "unemployment claims", "initial jobless claims", "continuing claims",
    "unemployment rate", "jobless claims",
)

HOLD_HORIZONS = (5, 15, 30)
DEFAULT_SL_GRID = (0, 50, 80, 100, 150)
DEFAULT_TP_GRID = (0, 60, 100, 150, 250)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def _iter_jsonl(path: str) -> Iterable[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    if not os.path.exists(path):
        return
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def load_paths(pair: str, historical: str, live: str) -> List[dict]:
    """Records for `pair` (root-normalized), live rows win on duplicate
    (event_key, pair), test rows and non-data rows dropped."""
    root = normalize_root(pair)
    by_key: Dict[Tuple[str, str], dict] = {}
    for source, path in (("historical", historical), ("live", live)):
        for rec in _iter_jsonl(path):
            if rec.get("test") or rec.get("non_data"):
                continue
            if normalize_root(rec.get("pair")) != root:
                continue
            if rec.get("move_1min_pips") is None:
                continue
            key = (str(rec.get("event_key")), root)
            if source == "historical" and key in by_key:
                continue
            by_key[key] = rec
    return list(by_key.values())


# ----------------------------------------------------------------------
# Direction helpers
# ----------------------------------------------------------------------

def _sign(v: Optional[float]) -> int:
    if v is None:
        return 0
    return (v > 0) - (v < 0)


def currency_is_base(pair: str, currency: str) -> Optional[bool]:
    """True when the event currency is the BASE leg of the pair, False when
    QUOTE, None when it is not a leg (profiled instruments use the profile)."""
    root = normalize_root(pair)
    cur = (currency or "").upper()
    prof = profile_for(root)
    if prof is not None:
        if cur == prof.quote_currency:
            return False
        if cur in (prof.base_tag, prof.learning_tag):
            return True
        return None
    if len(root) >= 6:
        if root[:3] == cur:
            return True
        if root[3:6] == cur:
            return False
    return None


def oracle_direction(rec: dict) -> int:
    """+1/-1 pair direction implied by the surprise, 0 when unknown/inline."""
    surprise = str(rec.get("surprise") or "").upper()
    if surprise not in ("BEAT", "MISS"):
        return 0
    name = str(rec.get("event_name_normalized") or "").lower()
    currency_up = surprise == "BEAT"
    if any(tok in name for tok in INVERSE_SENSE):
        currency_up = not currency_up
    base = currency_is_base(rec.get("pair") or "", rec.get("currency") or "")
    if base is None:
        return 0
    return (1 if currency_up else -1) if base else (-1 if currency_up else 1)


# ----------------------------------------------------------------------
# Outcome model
# ----------------------------------------------------------------------

def _excursions(rec: dict, d: int, entry_pips: float, entry_minute: int,
                horizon: int):
    """(favorable_max, adverse_max) in pips relative to the entry, over the
    horizon window. Adverse: stored window extremes (30-min extremes for
    every horizon > 5 — the dataset has no 15-min extremes — and, for
    post-release entries, including the pre-entry minutes: pessimistic).
    Favorable: window extremes for a T0 entry (all post-entry); for a
    post-release entry only the point samples strictly after the entry
    (lower bound on the MFE)."""
    if horizon <= 5:
        hi, lo = rec.get("first5_high_pips"), rec.get("first5_low_pips")
    else:
        hi, lo = rec.get("high_30min_pips"), rec.get("low_30min_pips")
    if hi is None or lo is None:
        return None, None
    adv = (entry_pips - lo) if d > 0 else (hi - entry_pips)
    if entry_minute <= 0:
        fav = (hi - entry_pips) if d > 0 else (entry_pips - lo)
        return fav, adv
    samples = [rec.get(f"move_{m}min_pips") for m in (5, 15, 30)
               if entry_minute < m <= horizon]
    samples = [v for v in samples if v is not None]
    if not samples:
        return None, adv
    fav = max(d * (v - entry_pips) for v in samples)
    return fav, adv


def simulate(rec: dict, d: int, entry_pips: float, entry_minute: int, horizon: int,
             sl: float, tp: float, spread: float) -> Optional[Tuple[float, str]]:
    """P/L in pips (after spread) and how the trade ended."""
    if d == 0 or horizon <= entry_minute:
        return None
    exit_move = rec.get(f"move_{horizon}min_pips")
    if exit_move is None:
        return None
    fav, adv = _excursions(rec, d, entry_pips, entry_minute, horizon)
    if sl > 0 and adv is not None and adv >= sl:
        return -sl - spread, "sl"
    if tp > 0 and fav is not None and fav >= tp:
        return tp - spread, "tp"
    return d * (exit_move - entry_pips) - spread, "time"


def strategy_entries(rec: dict, strategy: str, confirm_min: float,
                     drift_min: float) -> Tuple[int, float, int]:
    """(direction, entry offset in pips from the T0 price, entry minute)
    or (0, 0, 0) when the strategy does not fire on this release."""
    m1 = rec.get("move_1min_pips")
    none = (0, 0.0, 0)
    if strategy == "pre_oracle":
        return oracle_direction(rec), 0.0, 0
    if strategy == "pre_fade_drift":
        drift = rec.get("pre_release_3m_pips")
        if drift is None or abs(drift) < drift_min:
            return none
        return -_sign(drift), 0.0, 0
    if strategy == "post_confirm":
        if m1 is None or abs(m1) < confirm_min:
            return none
        return _sign(m1), float(m1), 1
    if strategy == "post_agree":
        if m1 is None or abs(m1) < confirm_min:
            return none
        o = oracle_direction(rec)
        if o == 0 or o != _sign(m1):
            return none
        return o, float(m1), 1
    if strategy == "post_fade":
        if m1 is None or abs(m1) < confirm_min:
            return none
        return -_sign(m1), float(m1), 1
    if strategy == "post_fade_5":
        m5 = rec.get("move_5min_pips")
        if m5 is None or abs(m5) < confirm_min:
            return none
        return -_sign(m5), float(m5), 5
    raise ValueError(strategy)


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def _pct(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def summarize(pnls: List[float], ends: List[str]) -> dict:
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "n": n,
        "win_rate": round(wins / n, 3),
        "avg_pips": round(sum(pnls) / n, 1),
        "median_pips": round(median(pnls), 1),
        "p10_pips": round(_pct(pnls, 0.10), 1),
        "p90_pips": round(_pct(pnls, 0.90), 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "sl_rate": round(ends.count("sl") / n, 3),
        "tp_rate": round(ends.count("tp") / n, 3),
    }


def run_grid(records: List[dict], strategies: List[str], sl_grid, tp_grid,
             horizons, spread: float, confirm_min: float, drift_min: float,
             min_n: int) -> List[dict]:
    rows = []
    by_event = defaultdict(list)
    for r in records:
        by_event[f"{r.get('currency')}|{r.get('event_name_normalized')}"].append(r)
    for event_key, recs in sorted(by_event.items(), key=lambda kv: -len(kv[1])):
        for strategy in strategies:
            for h in horizons:
                for sl in sl_grid:
                    for tp in tp_grid:
                        pnls, ends = [], []
                        for rec in recs:
                            d, entry, entry_min = strategy_entries(rec, strategy, confirm_min, drift_min)
                            res = simulate(rec, d, entry, entry_min, h, sl, tp, spread)
                            if res is None:
                                continue
                            pnls.append(res[0])
                            ends.append(res[1])
                        summ = summarize(pnls, ends)
                        if summ["n"] < min_n:
                            continue
                        rows.append({"event": event_key, "strategy": strategy,
                                     "hold_min": h, "sl": sl, "tp": tp, **summ})
    return rows


def best_per_event(rows: List[dict]) -> Dict[str, dict]:
    best: Dict[str, dict] = {}
    for row in rows:
        key = f"{row['event']}::{row['strategy']}"
        cur = best.get(key)
        if cur is None or row["avg_pips"] > cur["avg_pips"]:
            best[key] = row
    return best


def _fmt_row(r: dict) -> str:
    pf = r["profit_factor"]
    return (f"{r['strategy']:<15} hold={r['hold_min']:>2} sl={r['sl']:>4g} tp={r['tp']:>4g} "
            f"n={r['n']:>3} win={r['win_rate']*100:>5.1f}% avg={r['avg_pips']:>7.1f} "
            f"med={r['median_pips']:>7.1f} p10={r['p10_pips']:>7.1f} "
            f"PF={'-' if pf is None else pf:<5} sl%={r['sl_rate']*100:>4.0f} tp%={r['tp_rate']*100:>4.0f}")


def main(argv=None) -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pair", required=True)
    ap.add_argument("--historical", default=os.path.join(base, "knowledge", "historical_paths.jsonl.gz"))
    ap.add_argument("--live", default=os.path.join(base, "logs", "event_paths.jsonl"))
    ap.add_argument("--impact", default=None, help="HIGH / MEDIUM / LOW filter")
    ap.add_argument("--events", default=None, help="comma list of normalized event names")
    ap.add_argument("--strategies", default="pre_oracle,pre_fade_drift,post_confirm,post_agree,post_fade,post_fade_5")
    ap.add_argument("--sl", default=",".join(str(v) for v in DEFAULT_SL_GRID))
    ap.add_argument("--tp", default=",".join(str(v) for v in DEFAULT_TP_GRID))
    ap.add_argument("--hold", default=",".join(str(v) for v in HOLD_HORIZONS))
    ap.add_argument("--spread-pips", type=float, default=None)
    ap.add_argument("--confirm-min", type=float, default=None,
                    help="min |move_1min| pips for post_* entries (default: 10%% of the"
                         " event's median |move_5min| is NOT used; flat default 20 for"
                         " profiled instruments, 5 for forex)")
    ap.add_argument("--drift-min", type=float, default=None)
    ap.add_argument("--min-n", type=int, default=15)
    ap.add_argument("--top", type=int, default=12, help="events to print (by sample size)")
    ap.add_argument("--json", default=None, help="write every grid row here")
    args = ap.parse_args(argv)

    prof = profile_for(args.pair)
    spread = args.spread_pips
    if spread is None:
        spread = float(prof.typical_news_spread_pips or 3.0) if prof else 3.0
    confirm_min = args.confirm_min if args.confirm_min is not None else (20.0 if prof else 5.0)
    drift_min = args.drift_min if args.drift_min is not None else (15.0 if prof else 4.0)

    records = load_paths(args.pair, args.historical, args.live)
    if args.impact:
        records = [r for r in records if str(r.get("impact") or "").upper() == args.impact.upper()]
    if args.events:
        wanted = {e.strip().lower() for e in args.events.split(",") if e.strip()}
        records = [r for r in records if str(r.get("event_name_normalized") or "").lower() in wanted]
    if not records:
        print("no records for", args.pair)
        return 1

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    sl_grid = [float(v) for v in args.sl.split(",")]
    tp_grid = [float(v) for v in args.tp.split(",")]
    horizons = [int(v) for v in args.hold.split(",")]
    rows = run_grid(records, strategies, sl_grid, tp_grid, horizons, spread,
                    confirm_min, drift_min, args.min_n)

    unit = prof.units_label if prof else "pips"
    print(f"{normalize_root(args.pair)}: {len(records)} releases, unit={unit}, "
          f"spread charged={spread:g} pips, confirm_min={confirm_min:g}, drift_min={drift_min:g}")
    print("Outcome model is CONSERVATIVE for SL/TP variants (stop assumed hit before target).")

    counts = defaultdict(int)
    for r in records:
        counts[f"{r.get('currency')}|{r.get('event_name_normalized')}"] += 1
    best = best_per_event(rows)
    shown = 0
    for event_key, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        ev_rows = [v for k, v in best.items() if k.startswith(event_key + "::")]
        if not ev_rows:
            continue
        print(f"\n== {event_key} (n={counts[event_key]}) — best avg per strategy ==")
        for r in sorted(ev_rows, key=lambda x: -x["avg_pips"]):
            print("  " + _fmt_row(r))
        # Timed-exit reference (exact): no SL, no TP
        ref = [x for x in rows if x["event"] == event_key and x["sl"] == 0 and x["tp"] == 0]
        if ref:
            print("  -- timed exit only (exact) --")
            for r in sorted(ref, key=lambda x: (x["strategy"], x["hold_min"])):
                print("  " + _fmt_row(r))
        shown += 1
        if shown >= args.top:
            break

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"pair": normalize_root(args.pair), "releases": len(records),
                       "spread_pips": spread, "confirm_min": confirm_min,
                       "drift_min": drift_min, "rows": rows}, f, indent=1)
        print(f"\nwrote {len(rows)} grid rows to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
