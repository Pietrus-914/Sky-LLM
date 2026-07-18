"""
Similar-episode retrieval (F5): the SIMILAR PAST EPISODES prompt section.

Where LEARNED EVENT STATISTICS gives the model the AGGREGATE (medians, hit
rates), this gives it EPISODIC memory: the few most similar individual prior
releases of the same event, each with its full story — setup (forecast vs
previous), outcome (surprise), the measured path, and what the model decided
back then (with the realized result when a trade followed). Research basis:
structural keys beat vector search at this scale (hundreds of episodes), and
individual examples must be clearly subordinated to the aggregates in the
prompt — the renderer's header says so explicitly.

Similarity is computed on PRE-decision features only (pair, regime,
forecast-direction), never on the outcome — retrieval must not leak what
"today" cannot know. Pure functions over plain dicts; the engine supplies
the rows (paths via paths_provider, decisions from DecisionHistory, trades
from trade_history.jsonl).
"""
from typing import Dict, List, Optional

from event_reaction_history import normalize_event_name, parse_numeric
from market_context import normalize_pair

# How many episodes the prompt gets at most (token budget)
MAX_EPISODES = 3
# Similarity points (pre-decision features only)
SCORE_SAME_PAIR = 4
SCORE_SAME_REGIME = 2
SCORE_SAME_FORECAST_DIRECTION = 1


def _forecast_direction(forecast, previous) -> Optional[str]:
    f, p = parse_numeric(forecast), parse_numeric(previous)
    if f is None or p is None:
        return None
    return "up" if f > p else "down" if f < p else "flat"


def find_similar_episodes(paths: List[Dict], event_name: str, currency: str,
                          pair: str, regime: Optional[str],
                          forecast=None, previous=None,
                          limit: int = MAX_EPISODES) -> List[Dict]:
    """The most similar prior releases of this event, one per release
    minute, preferring the same pair's measurement. Newest first among
    equally similar."""
    currency = (currency or '').upper()
    wanted_name = normalize_event_name(event_name)
    pair_norm = normalize_pair(pair or '')
    today_direction = _forecast_direction(forecast, previous)

    by_minute: Dict[str, Dict] = {}
    for p in paths or []:
        if not isinstance(p, dict) or p.get('test') or not p.get('pair'):
            continue
        if (p.get('currency') or '').upper() != currency:
            continue
        name = (p.get('event_name_normalized')
                or normalize_event_name(p.get('event_name') or ''))
        if name != wanted_name:
            continue
        # Strict numeric gate: a corrupted row (string move) admitted here
        # would blow up rendering and take the GOOD episodes down with it
        if not isinstance(p.get('move_5min_pips'), (int, float)):
            continue
        minute = (p.get('event_time') or '')[:16]
        prev_pick = by_minute.get(minute)
        # One episode per release; the same-pair measurement tells the story
        # in the pips the model is about to trade
        if prev_pick is None or (
                normalize_pair(p.get('pair')) == pair_norm
                and normalize_pair(prev_pick.get('pair')) != pair_norm):
            by_minute[minute] = p

    scored = []
    for minute, p in by_minute.items():
        score = 0
        if normalize_pair(p.get('pair')) == pair_norm:
            score += SCORE_SAME_PAIR
        if regime and p.get('regime') == regime:
            score += SCORE_SAME_REGIME
        if (today_direction
                and _forecast_direction(p.get('forecast'),
                                        p.get('previous')) == today_direction):
            score += SCORE_SAME_FORECAST_DIRECTION
        scored.append((score, minute, p))
    # Two stable passes: newest first, then score desc (ties keep recency)
    scored.sort(key=lambda t: t[1], reverse=True)
    scored.sort(key=lambda t: -t[0])
    return [p for _, _, p in scored[:limit]]


def _join_decision(episode: Dict, decisions: List[Dict]) -> Optional[Dict]:
    """The (non-forced) decision recorded for this episode's release."""
    minute = (episode.get('event_time') or '')[:16]
    name = (episode.get('event_name_normalized')
            or normalize_event_name(episode.get('event_name') or ''))
    for d in decisions or []:
        if d.get('forced'):
            continue
        if (d.get('currency') or '').upper() != (episode.get('currency') or '').upper():
            continue
        if normalize_event_name(d.get('event_name') or '') != name:
            continue
        if (d.get('event_datetime') or '')[:16] != minute:
            continue
        return d
    return None


def _realized_pnl(decision: Dict, trades: List[Dict]) -> Optional[float]:
    d_id = decision.get('decision_id')
    if not d_id:
        return None
    for t in trades or []:
        if t.get('decision_id') == d_id:
            try:
                return float(t.get('profit_usd'))
            except (TypeError, ValueError):
                return None
    return None


def _render_one(ep: Dict, decisions: List[Dict],
                trades: List[Dict]) -> str:
    """One episode line; raises on malformed data (caller drops the row)."""
    date = (ep.get('event_time') or '')[:10]
    regime = ep.get('regime') or 'regime?'
    setup = []
    if ep.get('forecast') is not None:
        setup.append(f"fcst {ep['forecast']}")
    if ep.get('previous') is not None:
        setup.append(f"prev {ep['previous']}")
    actual = ep.get('actual')
    surprise = ep.get('surprise')
    if actual is not None and surprise and surprise != 'UNKNOWN':
        setup.append(f"actual {actual} ({surprise})")
    setup_txt = ", ".join(setup) if setup else "no numbers recorded"

    move5 = ep.get('move_5min_pips')
    move30 = ep.get('move_30min_pips')
    path_bits = [f"{ep.get('pair')} 5min {move5:+.1f}p"]
    if isinstance(move30, (int, float)):
        path_bits.append(f"30min {move30:+.1f}p")
    lo, hi = ep.get('first5_low_pips'), ep.get('first5_high_pips')
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        path_bits.append(f"first-5min range {lo:+.1f}..{hi:+.1f}p")

    line = f"- {date} ({regime}): {setup_txt} -> {', '.join(path_bits)}"

    decision = _join_decision(ep, decisions)
    if decision is not None:
        direction = decision.get('direction')
        conf = decision.get('confidence')
        conf_txt = (f"@{round(conf * 100)}%"
                    if isinstance(conf, (int, float)) else "")
        then = f"; you then chose {direction}{conf_txt}"
        if direction in ('BUY', 'SELL') and abs(move5) >= 1.0:
            correct = (move5 > 0) == (direction == 'BUY')
            then += f" -> {'CORRECT' if correct else 'WRONG'}"
        pnl = _realized_pnl(decision, trades)
        if pnl is not None:
            then += f", realized ${pnl:+.2f}"
        line += then
    return line


def render_episodes(episodes: List[Dict], decisions: List[Dict],
                    trades: List[Dict]) -> Optional[str]:
    """The SIMILAR PAST EPISODES block body, or None with no episodes.
    Per-row containment: one malformed episode drops that ROW, never the
    section (a corrupted store line must not erase the good examples)."""
    lines = []
    for ep in episodes or []:
        try:
            lines.append(_render_one(ep, decisions, trades))
        except Exception:
            continue
    return "\n".join(lines) if lines else None
