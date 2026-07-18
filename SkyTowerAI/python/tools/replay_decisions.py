"""
Replay harness (F5): offline A/B evaluation of prompt changes against
FROZEN past decisions — the guardrail that prompt "improvements" must pass
before they ship.

Every analyzed event left a full context dump in logs/decision_context/
(<decision_id>.json = decision row + data_summary, i.e. everything the
model saw). This tool joins those episodes with the MEASURED outcome
(event_paths 5-min move for the decision's pair), splits them
deterministically into dev (80%) and holdout (20%), and:

  --score-only      scores the RECORDED decisions (baseline table, no LLM)
  --variant NAME    rebuilds each episode's prompt with the CURRENT code
                    (so whatever prompt change you just made is what gets
                    tested), calls the LLM fresh, and scores the new
                    decisions. Costs money: N calls x entry model — the
                    run refuses to start without --yes.

HOLDOUT DISCIPLINE: iterate prompt variants on --split dev ONLY; run
--split holdout ONCE, at the end, for the honest number. Fitting the
holdout turns it into a second dev set — that is the entire reason the
split exists.

The server never imports this module (tools/ is offline).

Usage (from SkyTowerAI/python):
    python tools/replay_decisions.py --score-only
    python tools/replay_decisions.py --variant my-change --limit 20 --yes
"""
import argparse
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration import MIN_DIRECTIONAL_PIPS, index_paths
from event_reaction_history import normalize_event_name
from market_context import normalize_pair

HOLDOUT_BUCKETS = 5   # id-hash % 5 == 0 -> holdout (~20%)


def split_of(decision_id: str) -> str:
    """Deterministic dev/holdout split keyed by decision_id — stable across
    runs, machines and episode orderings."""
    digest = hashlib.sha1((decision_id or '').encode('utf-8')).hexdigest()
    return "holdout" if int(digest, 16) % HOLDOUT_BUCKETS == 0 else "dev"


def load_paths(paths_file):
    rows = []
    try:
        with open(paths_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def load_episodes(context_dir, paths_file):
    """Episodes = context dumps joined with a measured outcome. Dumps whose
    event was never measured (EA down, too recent) are skipped and counted."""
    paths_idx = index_paths(load_paths(paths_file))
    episodes, skipped = [], 0
    for path in sorted(glob.glob(os.path.join(context_dir, '*.json'))):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                dump = json.load(f)
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(dump, dict) or not isinstance(
                dump.get('data_summary'), dict):
            skipped += 1
            continue
        key = ((dump.get('currency') or '').upper(),
               normalize_event_name(dump.get('event_name') or ''),
               (dump.get('event_datetime') or '')[:16],
               normalize_pair(dump.get('pair') or ''))
        outcome = paths_idx.get(key)
        if outcome is None or outcome.get('move_5min_pips') is None:
            skipped += 1
            continue
        episodes.append({
            "decision_id": dump.get('decision_id', ''),
            "split": split_of(dump.get('decision_id', '')),
            "event_name": dump.get('event_name', ''),
            "currency": dump.get('currency', ''),
            "pair": key[3],
            "direction": dump.get('direction', ''),
            "confidence": dump.get('confidence'),
            "forced": bool(dump.get('forced')),
            "data_summary": dump['data_summary'],
            "move_5min_pips": outcome['move_5min_pips'],
        })
    return episodes, skipped


def score_rows(rows):
    """Same conventions as the calibration ledger: BUY/SELL vs the 5-min
    move sign; flat moves excluded; SKIPs counted separately."""
    directional, flat, skips = [], 0, 0
    for r in rows:
        move = r.get("move_5min_pips")
        direction = r.get("direction")
        if direction == 'SKIP':
            skips += 1
            continue
        if direction not in ('BUY', 'SELL') or move is None:
            continue
        if abs(move) < MIN_DIRECTIONAL_PIPS:
            flat += 1
            continue
        try:
            conf = min(max(float(r.get("confidence") or 0.0), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = 0.0
        correct = (move > 0) == (direction == 'BUY')
        directional.append((conf, correct))
    out = {"n": len(directional), "skips": skips, "flat": flat,
           "hit_rate": None, "brier": None, "avg_confidence": None}
    if directional:
        n = len(directional)
        out["hit_rate"] = round(sum(1 for _, c in directional if c) / n, 3)
        out["brier"] = round(sum((conf - (1.0 if c else 0.0)) ** 2
                                 for conf, c in directional) / n, 3)
        out["avg_confidence"] = round(sum(conf for conf, _ in directional) / n, 3)
    return out


def print_table(title, stats):
    print(f"\n{title}")
    print(f"  directional n={stats['n']}  skips={stats['skips']}  "
          f"flat-excluded={stats['flat']}")
    if stats["n"]:
        print(f"  hit rate {stats['hit_rate']:.1%}   Brier {stats['brier']}   "
              f"avg stated conf {stats['avg_confidence']:.1%}")
    else:
        print("  (nothing scoreable)")


def run_variant(episodes, model, limit):
    """Fresh LLM decisions over frozen contexts using the CURRENT prompt
    code. Returns scored rows (direction/confidence from the new calls)."""
    from llm_decision_engine import LLMDecisionEngine
    from config import OPENROUTER_API_KEY, LLM_CONFIG
    from openai import OpenAI

    engine = LLMDecisionEngine(provider="rule-based")   # prompt/parse helpers only
    client = OpenAI(api_key=OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1")
    use_model = model or LLM_CONFIG.get("model")
    system_prompt = engine._system_prompt()

    rows = []
    for i, ep in enumerate(episodes[:limit]):
        prompt = engine._entry_prompt(ep["data_summary"])
        try:
            response = client.chat.completions.create(
                model=use_model,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": prompt}],
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                temperature=0.3, timeout=90.0)
            text = response.choices[0].message.content or ""
        except Exception as e:
            print(f"  [{i + 1}] {ep['event_name']}: CALL FAILED ({e})",
                  flush=True)
            continue
        parsed = engine._parse_llm_response(text)
        direction = engine._normalize_direction(parsed.get('direction'))
        confidence = engine._num(parsed.get('confidence'), 0.0, 0.0, 1.0)
        move = ep["move_5min_pips"]
        verdict = "SKIP" if direction == 'SKIP' else (
            "correct" if (move > 0) == (direction == 'BUY') else "wrong")
        print(f"  [{i + 1}] {ep['currency']} {ep['event_name']}: "
              f"{direction}@{confidence:.2f} vs move {move:+.1f}p -> {verdict}",
              flush=True)
        rows.append({"direction": direction, "confidence": confidence,
                     "move_5min_pips": move})
    return rows


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--context-dir",
                    default=os.path.join(base, "logs", "decision_context"))
    ap.add_argument("--paths",
                    default=os.path.join(base, "logs", "event_paths.jsonl"))
    ap.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    ap.add_argument("--score-only", action="store_true",
                    help="score the RECORDED decisions; no LLM calls")
    ap.add_argument("--variant", default=None,
                    help="label for a fresh-LLM run over frozen contexts")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--include-forced", action="store_true",
                    help="also include FORCE_DECISION episodes (both tables)")
    ap.add_argument("--yes", action="store_true",
                    help="confirm the LLM spend of a --variant run")
    args = ap.parse_args()

    if not args.score_only and not args.variant:
        ap.error("pick --score-only or --variant NAME")

    episodes, skipped = load_episodes(args.context_dir, args.paths)
    if args.split != "all":
        episodes = [e for e in episodes if e["split"] == args.split]
    # ONE forced-filter applied up front — variant and baseline must score
    # the SAME episode set, or the A/B comparison is meaningless (review
    # finding: demo-phase data is mostly forced=true)
    if not args.include_forced:
        episodes = [e for e in episodes if not e["forced"]]
    print(f"episodes with measured outcomes: {len(episodes)} "
          f"(split={args.split}"
          f"{', incl. forced' if args.include_forced else ''}; "
          f"unmeasured/unreadable skipped: {skipped})", flush=True)
    if not episodes:
        sys.exit(0)

    if args.score_only:
        print_table(f"BASELINE (recorded decisions, split={args.split})",
                    score_rows(episodes))
        return

    n_calls = min(args.limit, len(episodes))
    if not args.yes:
        print(f"\nVariant '{args.variant}' would make {n_calls} LLM calls "
              f"({args.model or 'entry model'}). Re-run with --yes to spend it.")
        sys.exit(1)
    print(f"\nVARIANT '{args.variant}': {n_calls} fresh calls over frozen "
          f"contexts (split={args.split})...", flush=True)
    rows = run_variant(episodes, args.model, args.limit)
    print_table(f"VARIANT '{args.variant}' (split={args.split})",
                score_rows(rows))
    print_table("BASELINE on the SAME episodes (recorded decisions)",
                score_rows(episodes[:args.limit]))


if __name__ == "__main__":
    main()
