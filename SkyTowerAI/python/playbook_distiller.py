"""
Playbook distillation (F5): machine-drafted updates to the curated
knowledge/event_playbooks.json, delivered as DIFF PROPOSALS the operator
approves or rejects on the dashboard. NEVER auto-applied — research
documents that self-rewriting prompt knowledge without a human gate
degrades; the approval step IS the design.

Flow: operator clicks "Generate" for an event -> the exit-tier model reads
the MEASURED learned-stats entry + the current playbook entry and drafts a
replacement written as frequencies-with-n (no imperatives) -> the proposal
lands in logs/playbook_proposals.jsonl as pending -> Approve writes the
entry into knowledge/event_playbooks.json (atomic; the engine hot-reloads
by mtime) and marks the proposal approved; Reject just marks it.
"""
import json
import os
from threading import Lock
from typing import Dict, List, Optional
from uuid import uuid4

from loguru import logger

from timeutil import utcnow
from event_reaction_history import (normalize_event_name,
                                    atomic_rewrite_jsonl)

# Per-field cap: a proposal is a compact playbook entry, not an essay
MAX_FIELD_CHARS = 450
_ENTRY_FIELDS = ("pattern", "typical_behavior", "notes")

# Batch selection gates (dashboard "Generate ALL" button)
BATCH_MIN_RELEASES = 10       # thinner samples make weak playbook entries
BATCH_MAX = 10                # per click — keeps the LLM spend visible
BATCH_COOLDOWN_DAYS = 14      # don't re-draft what was just decided


def select_batch_candidates(learned_events: Dict, playbooks: Dict,
                            proposals: List[Dict], tradeable_names: List[str],
                            now_iso: str, max_batch: int = BATCH_MAX,
                            min_releases: int = BATCH_MIN_RELEASES,
                            cooldown_days: int = BATCH_COOLDOWN_DAYS):
    """Which events deserve a machine-drafted playbook update right now.

    Gates: enough measured releases; the event name matches the tradeable
    whitelist (junk like weekly oil inventories has stats too — the model
    never trades it); no proposal already PENDING; no proposal created
    within the cooldown (a fresh reject must not re-spawn instantly).
    Returns (candidates, skipped_counter) — pure, unit-testable."""
    from datetime import datetime, timedelta
    cutoff = ((datetime.fromisoformat(now_iso.replace('Z', ''))
               - timedelta(days=cooldown_days)).isoformat())

    recent = {}
    for p in proposals or []:
        key = (p.get('currency', ''), normalize_event_name(p.get('event_name') or ''))
        if p.get('status') == 'pending':
            recent[key] = 'pending'
        elif (p.get('created_at') or '') >= cutoff:
            recent.setdefault(key, 'cooldown')

    wanted = [w.lower() for w in tradeable_names or []]
    candidates, skipped = [], {"thin": 0, "not_tradeable": 0,
                               "pending": 0, "cooldown": 0, "capped": 0}
    for key in sorted(learned_events or {},
                      key=lambda k: -(learned_events[k].get('n_releases') or 0)):
        entry = learned_events[key]
        display = entry.get('event_name') or key.split('|', 1)[-1]
        currency = entry.get('currency') or key.split('|', 1)[0]
        if (entry.get('n_releases') or 0) < min_releases:
            skipped["thin"] += 1
            continue
        if not any(w in display.lower() for w in wanted):
            skipped["not_tradeable"] += 1
            continue
        state = recent.get((currency, normalize_event_name(display)))
        if state:
            skipped[state] += 1
            continue
        if len(candidates) >= max_batch:
            skipped["capped"] += 1
            continue
        candidates.append({
            "event_name": display,
            "currency": currency,
            "learned": entry,
            "playbook_key": find_playbook_key(playbooks, display),
        })
    return candidates, skipped


DISTILL_SYSTEM = """You distill MEASURED trading statistics into a compact
playbook entry for one economic event. The entry will be shown to a trading
model before future releases of this event.

Rules:
- Write FREQUENCIES with sample sizes ("faded the pre-drift in 62% (n=41)"),
  never imperatives ("always fade") — the reader must weigh evidence, not
  obey instructions.
- Only claim what the provided statistics support; do not invent numbers.
- Keep the existing entry's still-valid observations; drop ones the
  statistics contradict.
- Reply with ONLY a JSON object:
{
  "pattern": "1-2 sentences: the dominant measured behavior",
  "typical_behavior": "1-2 sentences: magnitudes/wicks/continuation with n=",
  "notes": "1 sentence: caveats (regime dependence, thin samples)",
  "rationale": "1 sentence: what changed vs the current entry and why"
}"""


class PlaybookProposals:
    """Tolerant JSONL store of distillation proposals."""

    def __init__(self, file_path: str):
        self._file_path = file_path
        self._lock = Lock()

    def _load(self) -> List[Dict]:
        rows: List[Dict] = []
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, 'r', encoding='utf-8',
                          errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict):
                            rows.append(row)
        except Exception as e:
            logger.warning(f"Could not load playbook proposals: {e}")
        return rows

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict]:
        with self._lock:
            rows = self._load()
        if status:
            rows = [r for r in rows if r.get('status') == status]
        return list(reversed(rows[-limit:] if not status else rows))[:limit]

    def add(self, proposal: Dict) -> bool:
        """True when persisted — the endpoint must not report success for a
        proposal that never reached disk (review finding)."""
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(proposal, ensure_ascii=False) + '\n')
                return True
            except Exception as e:
                logger.error(f"Error writing playbook proposal: {e}")
                return False

    def decide(self, proposal_id: str, action: str,
               playbooks_file: str) -> Dict:
        """approve|reject a pending proposal. Approval APPLIES the entry to
        the playbooks file first — if that write fails, the proposal stays
        pending (no lost updates)."""
        if action not in ('approve', 'reject'):
            return {"ok": False, "error": f"unknown action '{action}'"}
        with self._lock:
            rows = self._load()
            target = next((r for r in rows
                           if r.get('id') == proposal_id), None)
            if target is None:
                return {"ok": False, "error": "proposal not found"}
            if target.get('status') != 'pending':
                return {"ok": False,
                        "error": f"already {target.get('status')}"}
            if action == 'approve':
                try:
                    _apply_entry(playbooks_file, target)
                except Exception as e:
                    logger.error(f"Playbook apply failed: {e}")
                    return {"ok": False, "error": f"apply failed: {e}"}
            target['status'] = ('approved' if action == 'approve'
                                else 'rejected')
            target['decided_at'] = utcnow().isoformat() + "Z"
            if not atomic_rewrite_jsonl(self._file_path, rows):
                return {"ok": False, "error": "proposal store rewrite failed"}
            return {"ok": True, "proposal": target}


def _apply_entry(playbooks_file: str, proposal: Dict) -> None:
    """Write the approved entry into the curated playbooks JSON (atomic).
    Uses the proposal's playbook_key — the EXISTING key when the event
    already had an entry (normalized-name lookup found it at proposal
    time), else the event's display name."""
    data = {}
    if os.path.exists(playbooks_file):
        with open(playbooks_file, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("playbooks file is not a JSON object")
    entry = {k: proposal['proposed'][k] for k in _ENTRY_FIELDS
             if proposal.get('proposed', {}).get(k)}
    if not entry:
        raise ValueError("proposal has no entry fields")
    data[proposal['playbook_key']] = entry
    os.makedirs(os.path.dirname(playbooks_file), exist_ok=True)
    tmp = playbooks_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, playbooks_file)


def find_playbook_key(playbooks: Dict, event_name: str) -> str:
    """The existing key matching this event (normalized), else the event's
    own display name — approval must UPDATE the entry the engine already
    finds, not create a near-duplicate under a variant spelling."""
    wanted = normalize_event_name(event_name)
    for key in (playbooks or {}):
        if key.upper().startswith("CURRENCY:"):
            continue
        if normalize_event_name(key) == wanted:
            return key
    return event_name


def build_distill_user_prompt(event_name: str, currency: str,
                              learned_entry: Optional[Dict],
                              current_entry: Optional[Dict]) -> str:
    parts = [f"EVENT: {currency} \"{event_name}\""]
    if learned_entry:
        parts.append("MEASURED STATISTICS (2021->today, machine-computed):\n"
                     + json.dumps(learned_entry, ensure_ascii=False, indent=1))
    else:
        parts.append("MEASURED STATISTICS: none recorded for this event yet.")
    if current_entry:
        parts.append("CURRENT PLAYBOOK ENTRY:\n"
                     + json.dumps(current_entry, ensure_ascii=False, indent=1))
    else:
        parts.append("CURRENT PLAYBOOK ENTRY: none (this would be a new entry).")
    return "\n\n".join(parts)


def parse_proposal_reply(text: str) -> Optional[Dict]:
    """Validate the model's JSON: string fields, hard length caps, at least
    one entry field. Salvages the {...} slice from a chatty reply."""
    if not text:
        return None
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for field in _ENTRY_FIELDS + ("rationale",):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            out[field] = value.strip()[:MAX_FIELD_CHARS]
    if not any(f in out for f in _ENTRY_FIELDS):
        return None
    return out


def generate_proposal(chat_fn, event_name: str, currency: str,
                      learned_entry: Optional[Dict],
                      current_entry: Optional[Dict],
                      playbook_key: str) -> Optional[Dict]:
    """One pending proposal, or None (no LLM / unparseable reply)."""
    if chat_fn is None:
        return None
    try:
        reply = chat_fn(DISTILL_SYSTEM,
                        build_distill_user_prompt(event_name, currency,
                                                  learned_entry, current_entry))
    except Exception as e:
        logger.warning(f"Distillation call failed: {e}")
        return None
    parsed = parse_proposal_reply(reply)
    if parsed is None:
        logger.warning("Distillation reply unparseable — no proposal")
        return None
    rationale = parsed.pop('rationale', '')
    return {
        "id": uuid4().hex,
        "created_at": utcnow().isoformat() + "Z",
        "event_name": event_name,
        "currency": (currency or '').upper(),
        "playbook_key": playbook_key,
        "current": current_entry,
        "proposed": parsed,
        "rationale": rationale,
        "status": "pending",
    }
