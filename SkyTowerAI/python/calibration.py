"""
Calibration ledger (F4): scores past decisions — including SKIPs — against
the server-measured post-event price paths (event_path_recorder), producing
Brier score + reliability buckets. The point: LLM verbal confidence is
systematically overconfident; showing the model its own measured calibration
(prompt line, gated at n>=50) and the operator a dashboard card makes the
drift visible instead of anecdotal.

Join: decision (currency, normalized event name, event minute, pair) ->
measured path record. decision_id cannot be the join key here — path records
are per EVENT, not per decision.

Scoring rules:
- forced=true decisions (FORCE_DECISION demo mode) are excluded — coin flips
  must not pollute calibration.
- BUY/SELL with a measured 5-min move: correct = sign(move) matches; moves
  under MIN_DIRECTIONAL_PIPS are flat — neither right nor wrong, excluded
  from Brier/hit-rate (counted separately).
- SKIP: no direction to score; reported as "what happened anyway" (median
  |move|, share of big movers) — informational, not right/wrong.
- Duplicate decisions for one event (re-analysis appends) — only the NEWEST
  row per (currency, event, minute) is scored; it's the one that drove (or
  would have driven) the signal.

Pure functions over plain dicts — no file or server dependencies; callers
(engine prompt section, /api/calibration) supply the rows.
"""
from typing import Dict, List, Optional

from config import TYPICAL_NEWS_SPREADS
from event_reaction_history import normalize_event_name
from market_context import normalize_pair

# |move| under this is flat noise — direction neither right nor wrong
MIN_DIRECTIONAL_PIPS = 1.0
# A skipped event that then moved at least this much was a "big mover"
BIG_MOVE_PIPS = 15.0
# Fallback when a pair has no configured news spread
DEFAULT_NEWS_SPREAD_PIPS = 8.0


def news_spread_pips(pair: str) -> float:
    """Typical NEWS spread for the pair — the cost every entry crosses.
    Direction-hit alone flatters the model: the 2026-07 path study showed
    only ~56-83% of even HIGH-impact 5-min moves clear this spread."""
    value = TYPICAL_NEWS_SPREADS.get(normalize_pair(pair or ""))
    if isinstance(value, dict):
        try:
            return float(value.get("news", DEFAULT_NEWS_SPREAD_PIPS))
        except (TypeError, ValueError):
            return DEFAULT_NEWS_SPREAD_PIPS
    return DEFAULT_NEWS_SPREAD_PIPS
# Reliability buckets over stated confidence (right-open; last takes 1.0)
BUCKETS = ((0.0, 0.55), (0.55, 0.70), (0.70, 1.01))
BUCKET_LABELS = ("<55%", "55-70%", ">=70%")
# The model only sees its calibration once the sample is meaningful
PROMPT_MIN_N = 50


def index_paths(paths: List[Dict]) -> Dict:
    """(currency, event_norm, minute, pair) -> newest measured path record.
    Records without a 5-min move (no_data / too partial) are unusable."""
    out = {}
    for p in paths or []:
        if not isinstance(p, dict) or p.get('test') or not p.get('pair'):
            continue
        if p.get('move_5min_pips') is None:
            continue
        key = ((p.get('currency') or '').upper(),
               p.get('event_name_normalized')
               or normalize_event_name(p.get('event_name') or ''),
               (p.get('event_time') or '')[:16],
               normalize_pair(p.get('pair') or ''))
        # Explicit newest-by-recorded_at — callers pass get_recent() output
        # (newest FIRST), so "last write wins" would keep the OLDEST record
        prev = out.get(key)
        if prev is None or (p.get('recorded_at') or '') >= (prev.get('recorded_at') or ''):
            out[key] = p
    return out


def _dedup_decisions(decisions: List[Dict]) -> List[Dict]:
    """Newest decision per (currency, event, minute). Input may be in any
    order — rows carry ISO timestamps, so 'newest' is comparable directly."""
    best = {}
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        key = ((d.get('currency') or '').upper(),
               normalize_event_name(d.get('event_name') or ''),
               (d.get('event_datetime') or '')[:16])
        prev = best.get(key)
        if prev is None or (d.get('timestamp') or '') > (prev.get('timestamp') or ''):
            best[key] = d
    return list(best.values())


def score_decision(decision: Dict, paths_index: Dict) -> Optional[Dict]:
    """One ledger row, or None when unscorable (forced / malformed / the
    event's path was never measured)."""
    if decision.get('forced'):
        return None
    direction = decision.get('direction')
    if direction not in ('BUY', 'SELL', 'SKIP'):
        return None
    key = ((decision.get('currency') or '').upper(),
           normalize_event_name(decision.get('event_name') or ''),
           (decision.get('event_datetime') or '')[:16],
           normalize_pair(decision.get('pair') or ''))
    path = paths_index.get(key)
    if path is None:
        return None
    move = path.get('move_5min_pips')
    try:
        confidence = min(max(float(decision.get('confidence') or 0.0), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    spread = news_spread_pips(key[3])
    row = {
        "decision_id": decision.get('decision_id', ''),
        "event_name": decision.get('event_name', ''),
        "direction": direction,
        "confidence": confidence,
        "model": decision.get('model', ''),
        "pair": key[3],
        "move_5min_pips": move,
        "spread_pips": spread,
        "correct": None,   # None = flat or SKIP (not scoreable directionally)
        "net_pips": None,  # signed 5-min move minus the news spread
        "playable": None,  # |move| cleared the news spread at all
    }
    if direction in ('BUY', 'SELL') and abs(move) >= MIN_DIRECTIONAL_PIPS:
        row["correct"] = (move > 0) == (direction == 'BUY')
        signed = move if direction == 'BUY' else -move
        row["net_pips"] = round(signed - spread, 1)
        row["playable"] = abs(move) >= spread
    return row


def build_summary(decisions: List[Dict], paths: List[Dict]) -> Dict:
    """Full calibration summary for the dashboard + prompt line source."""
    index = index_paths(paths)
    rows = []
    for d in _dedup_decisions(decisions):
        row = score_decision(d, index)
        if row is not None:
            rows.append(row)

    directional = [r for r in rows if r["correct"] is not None]
    flat = [r for r in rows
            if r["direction"] in ('BUY', 'SELL') and r["correct"] is None]
    skips = [r for r in rows if r["direction"] == 'SKIP']

    summary = {
        "n_scored": len(directional),
        "n_flat_excluded": len(flat),
        "brier": None,
        "hit_rate": None,
        "avg_confidence": None,
        "buckets": [],
        "skips": {"n": len(skips)},
    }

    if directional:
        n = len(directional)
        summary["brier"] = round(sum(
            (r["confidence"] - (1.0 if r["correct"] else 0.0)) ** 2
            for r in directional) / n, 3)
        summary["hit_rate"] = round(
            sum(1 for r in directional if r["correct"]) / n, 2)
        summary["avg_confidence"] = round(
            sum(r["confidence"] for r in directional) / n, 2)
        # Spread-aware view: mean signed pips net of the news spread, and
        # the hit rate among moves that actually cleared the spread. The
        # 2026-07 path study break-even for the TIER1-like basket is ~57%
        # directional hit — direction-only hit rate above 50% can still
        # lose money, and this makes that visible.
        summary["net_ev_pips"] = round(
            sum(r["net_pips"] for r in directional) / n, 2)
        playable = [r for r in directional if r["playable"]]
        summary["playable"] = {
            "n": len(playable),
            "hit_rate": (round(sum(1 for r in playable if r["correct"])
                               / len(playable), 2) if playable else None),
        }
        # Per-model breakdown (decision rows carry `model` since 2026-07-26)
        by_model = {}
        for r in directional:
            by_model.setdefault(r["model"] or "(unknown)", []).append(r)
        summary["by_model"] = [
            {
                "model": model,
                "n": len(sub),
                "hit_rate": round(
                    sum(1 for r in sub if r["correct"]) / len(sub), 2),
                "avg_confidence": round(
                    sum(r["confidence"] for r in sub) / len(sub), 2),
                "net_ev_pips": round(
                    sum(r["net_pips"] for r in sub) / len(sub), 2),
            }
            for model, sub in sorted(
                by_model.items(), key=lambda kv: -len(kv[1]))
            if len(sub) >= 10
        ]
        for (lo, hi), label in zip(BUCKETS, BUCKET_LABELS):
            sub = [r for r in directional if lo <= r["confidence"] < hi]
            if not sub:
                summary["buckets"].append({"label": label, "n": 0})
                continue
            summary["buckets"].append({
                "label": label,
                "n": len(sub),
                "avg_confidence": round(
                    sum(r["confidence"] for r in sub) / len(sub), 2),
                "hit_rate": round(
                    sum(1 for r in sub if r["correct"]) / len(sub), 2),
            })

    if skips:
        moves = sorted(abs(r["move_5min_pips"]) for r in skips)
        mid = len(moves) // 2
        median = (moves[mid] if len(moves) % 2
                  else (moves[mid - 1] + moves[mid]) / 2)
        summary["skips"] = {
            "n": len(skips),
            "median_abs_move_5min": round(median, 1),
            "big_move_share": round(
                sum(1 for m in moves if m >= BIG_MOVE_PIPS) / len(moves), 2),
        }
    return summary


def prompt_line(summary: Dict) -> Optional[str]:
    """One compact prompt line, only once the sample is meaningful (n>=50).
    Names the direction of miscalibration explicitly — 'Brier 0.29' alone
    would not move a language model's stated confidence."""
    n = summary.get("n_scored") or 0
    if n < PROMPT_MIN_N:
        return None
    avg_conf = summary["avg_confidence"]
    hit = summary["hit_rate"]
    gap = avg_conf - hit
    if gap >= 0.05:
        verdict = (f"you have been OVERCONFIDENT by "
                   f"{round(gap * 100)} points — state lower confidence")
    elif gap <= -0.05:
        verdict = (f"you have been UNDERCONFIDENT by "
                   f"{round(-gap * 100)} points")
    else:
        verdict = "well calibrated so far — keep it up"
    bucket_bits = []
    for b in summary.get("buckets", []):
        if b.get("n", 0) >= 10:
            bucket_bits.append(f"{b['label']}: {round(b['hit_rate'] * 100)}% "
                               f"hit (n={b['n']})")
    text = (f"CALIBRATION (your last {n} measured directional calls): stated "
            f"confidence averaged {round(avg_conf * 100)}%, actual hit rate "
            f"{round(hit * 100)}% (Brier {summary['brier']}) — {verdict}.")
    if bucket_bits:
        text += " By stated confidence: " + "; ".join(bucket_bits) + "."
    return text
