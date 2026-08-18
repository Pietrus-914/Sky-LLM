"""
Same-minute release clusters — ONE decision per release.

Statistical agencies publish a whole family at one timestamp: CAD CPI m/m
(HIGH) lands together with Median / Trimmed CPI y/y (HIGH), Common CPI y/y
(MEDIUM) and Core CPI m/m (LOW); NFP arrives with Average Hourly Earnings and
the Unemployment Rate. The market prices the COMBINED surprise — there is one
price path, so there can be only one trade.

Before this module the updater treated them as independent candidates: it
analyzed the first one, and on SKIP marked only THAT name as analyzed, so the
next 15 s scan paid for a full panel on the next sibling. Consequences seen in
production on 17.08.2026 (CAD CPI at 12:30 UTC):
  * up to four paid panels for one release (~$0.85 instead of ~$0.21);
  * each successive analysis started later, and the panel deadline is
    max(T-20 s, 10 s) — the last sibling votes under a 10 s clock or falls to
    the rule-based engine;
  * the trade was labelled with the LAST, weakest sibling ("Common CPI y/y",
    MEDIUM) instead of the headline "CPI m/m" (HIGH), so the curated playbook
    and the reaction history were filed under a name nobody looks up;
  * the prompt showed the model that MEDIUM sibling's forecast/previous only,
    never mentioning that the HIGH headline printed in the same second.

The dominance ordering is shared with tools/build_learned_stats.py, which
attributes a bundled release to its dominant member — the decision label and
the statistics bundle must agree, otherwise the prompt quotes base rates for
an event name the trade was never filed under.
"""
from typing import List, Optional, Sequence, Tuple

from event_reaction_history import normalize_event_name
from regime_tracker import is_rate_decision

# Lower rank = more dominant.
IMPACT_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# Event families ordered by which one "owns" a shared release minute.
# Substring match on the normalized name; first hit wins. Rate decisions are
# rank 0 via is_rate_decision (handles all central-bank naming variants).
FAMILY_ORDER = (
    ("non-farm", "nonfarm", "payroll"),
    ("cpi",),
    ("gdp",),
    ("employment change",),
    ("unemployment rate",),
    ("retail sales",),
    ("ppi",),
    ("pmi",),
    ("trade balance",),
)

# "Core CPI m/m" must not outrank "CPI m/m" alphabetically — modified
# variants of a release yield the canonical bundle name to the plain one
MODIFIER_TOKENS = ("core", "final", "flash", "prelim", "revised", "trimmed")


def family_rank(name_norm: str) -> int:
    if is_rate_decision(name_norm):
        return 0
    for i, tokens in enumerate(FAMILY_ORDER):
        if any(t in name_norm for t in tokens):
            return i + 1
    return 50


def dominance_rank(name_normalized: str, impact: str) -> tuple:
    """Sort key: lower = more dominant. The trailing name keeps ties
    deterministic (canonical member = first alphabetically among unmodified
    names)."""
    name = name_normalized or ""
    return (
        IMPACT_RANK.get((impact or "").upper(), 3),
        family_rank(name),
        1 if any(t in name for t in MODIFIER_TOKENS) else 0,
        name,
    )


def _event_rank(event) -> tuple:
    return dominance_rank(normalize_event_name(getattr(event, "event_name", "")),
                          getattr(event, "impact", ""))


def release_key(event) -> Tuple[str, str]:
    """(currency, minute) — the identity of a release. Minute precision
    matches _analyzed_event_key in server.py, so a cluster member marked here
    is the same key the selector checks."""
    dt = getattr(event, "datetime_utc", None)
    minute = dt.strftime("%Y%m%d_%H%M") if dt is not None else ""
    return (str(getattr(event, "currency", "")).upper(), minute)


def same_release(events: Sequence, reference) -> List:
    """Every event of `events` published by the same currency in the same
    minute as `reference` (including it), in the order given."""
    key = release_key(reference)
    return [e for e in events if release_key(e) == key]


def pick_dominant(events: Sequence) -> Tuple[Optional[object], List]:
    """(dominant, shadowed) — the member that owns the release plus the ones
    it speaks for. Input order is irrelevant: the ranking is total (impact,
    family, modified-variant, name), so the same cluster always yields the
    same label no matter how the feed sorted it that day."""
    members = list(events)
    if not members:
        return None, []
    ranked = sorted(members, key=_event_rank)
    return ranked[0], ranked[1:]


def co_release_brief(dominant, candidates: Sequence, limit: int = 6) -> List[dict]:
    """The other prints of the same release, for the prompt: name, impact and
    the numbers the model needs to judge the COMBINED surprise.

    `candidates` may be any event list (monitored events are richer than the
    tradeable ones — a co-released MEDIUM print the whitelist ignores still
    moves the price). The dominant itself is excluded, duplicates by name are
    collapsed, and the list is capped so a busy minute cannot flood the prompt.
    """
    if dominant is None:
        return []
    key = release_key(dominant)
    dominant_name = normalize_event_name(getattr(dominant, "event_name", ""))
    seen = {dominant_name}
    out = []
    for event in sorted(candidates, key=_event_rank):
        if release_key(event) != key:
            continue
        name_norm = normalize_event_name(getattr(event, "event_name", ""))
        if name_norm in seen:
            continue
        seen.add(name_norm)
        out.append({
            "name": getattr(event, "event_name", ""),
            "impact": getattr(event, "impact", ""),
            "forecast": getattr(event, "forecast", None),
            "previous": getattr(event, "previous", None),
        })
        if len(out) >= limit:
            break
    return out
