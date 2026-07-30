"""
Post-trade reflections (F5): after each CLOSED, non-forced trade the system
writes itself a 2-3 sentence journal entry (exit-tier model) — what mattered
in THIS trade, what to verify next time.

QUARANTINE is the whole design: research documents that self-written
"lessons" degrade LLM behavior when they masquerade as knowledge. So
reflections live in their own JSONL, are injected into the entry prompt
ONLY under an explicit "n=1 anecdotes, NOT rules" header, capped at
MAX_PROMPT_REFLECTIONS, and the generation prompt itself forbids
generalization ("no always/never"). The measured statistics always outrank
them — the section header says so to the model.

Generation runs in a background thread fired from /api/position/closed —
it must never block the EA's HTTP call. Disabled via SKYTOWER_REFLECTIONS=0.
"""
import json
import os
from threading import Lock
from typing import Dict, List, Optional

from loguru import logger

from timeutil import utcnow
from event_reaction_history import normalize_event_name, is_test_event_name

# At most this many anecdotes reach the prompt
MAX_PROMPT_REFLECTIONS = 2
# A reflection longer than this is a runaway essay — hard-trimmed
MAX_REFLECTION_CHARS = 400

GENERATION_SYSTEM = """You are the private post-trade journal of an automated
news-trading system. Write a reflection on ONE closed trade.

Rules:
- 2-3 sentences, max 60 words total.
- Concrete: what mattered in THIS trade's outcome (timing, stop size,
  direction read, spread, exit), and ONE thing to verify next time.
- This is a SINGLE sample. NO general rules, no "always"/"never", no
  strategy rewrites.
- Reply with the reflection text only — no preamble, no JSON."""


class ReflectionStore:
    """Tolerant JSONL store, mtime-cached for the prompt-side reader."""

    def __init__(self, file_path: str):
        self._file_path = file_path
        self._lock = Lock()
        self._cache: Optional[List[Dict]] = None
        self._mtime = None

    def _load(self) -> List[Dict]:
        path = self._file_path
        try:
            if not path or not os.path.exists(path):
                return []
            mtime = os.path.getmtime(path)
            if self._cache is not None and mtime == self._mtime:
                return self._cache
            rows: List[Dict] = []
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
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
            self._cache = rows
            self._mtime = mtime
            return rows
        except Exception as e:
            logger.warning(f"Could not load reflections: {e}")
            return []

    def append(self, record: Dict) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
                # Torn-line guard: a worker killed mid-write leaves a line
                # without its newline — appending straight onto it would fuse
                # two records into one unparseable line
                lead = ""
                try:
                    if os.path.getsize(self._file_path) > 0:
                        with open(self._file_path, 'rb') as check:
                            check.seek(-1, os.SEEK_END)
                            if check.read(1) != b'\n':
                                lead = "\n"
                except OSError:
                    pass
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    f.write(lead + json.dumps(record, ensure_ascii=False) + '\n')
                self._cache = None   # invalidate; reader re-loads by mtime
            except Exception as e:
                logger.error(f"Error writing reflection: {e}")

    def get_matching(self, event_name: str, currency: str,
                     limit: int = MAX_PROMPT_REFLECTIONS) -> List[Dict]:
        """Newest first: exact normalized event name, padded with same-
        currency reflections when the exact sample is thin."""
        rows = self._load()
        wanted = normalize_event_name(event_name or '')
        cur = (currency or '').upper()
        exact, fallback = [], []
        for row in reversed(rows):
            if (row.get('currency') or '').upper() != cur:
                continue
            if normalize_event_name(row.get('event_name') or '') == wanted:
                exact.append(row)
            else:
                fallback.append(row)
        return (exact + fallback)[:limit]


def render_for_prompt(rows: List[Dict]) -> Optional[str]:
    if not rows:
        return None
    lines = []
    for row in rows:
        date = (row.get('timestamp') or '')[:10]
        try:
            pnl = f"${float(row.get('profit_usd')):+.2f}"
        except (TypeError, ValueError):
            pnl = "P/L ?"
        lines.append(f"- {date} {row.get('event_name') or '?'} "
                     f"{row.get('pair') or '?'} {row.get('direction') or '?'} "
                     f"-> {pnl} ({row.get('reason') or 'unknown'}): "
                     f"\"{row.get('text') or ''}\"")
    return "\n".join(lines)


def trade_eligible(trade: Dict) -> bool:
    """Only real, completed trades reflect: forced demo coin-flips, dry-run
    FAKE TEST trades and restart-orphaned stubs would journal noise.

    The dry-run case is the subtle one — a fake event traded WITHOUT force mode
    produces forced=False, and its name normalizes to the real event's, so the
    reflection would be served as first-hand experience of a release that never
    happened."""
    return (isinstance(trade, dict)
            and not trade.get('forced')
            and bool(trade.get('event_name'))
            and not is_test_event_name(trade.get('event_name'))
            and trade.get('event_name') != "(position lost in restart)"
            and trade.get('direction') in ('BUY', 'SELL'))


def build_generation_user_prompt(trade: Dict) -> str:
    fields = {k: trade.get(k) for k in (
        'event_name', 'symbol', 'direction', 'lots', 'entry_price',
        'close_price', 'sl', 'tp', 'profit_usd', 'reason',
        'max_profit_usd', 'max_drawdown_usd', 'spread_pips',
        'opened_at', 'closed_at')}
    text = "CLOSED TRADE:\n" + json.dumps(fields, ensure_ascii=False, indent=1)
    reasoning = (trade.get('entry_reasoning') or '')[:500]
    if reasoning:
        text += f"\n\nENTRY REASONING AT THE TIME:\n{reasoning}"
    return text


def generate_and_store(chat_fn, store: ReflectionStore, trade: Dict,
                       currency: str = None) -> Optional[Dict]:
    """One reflection for one closed trade. Returns the stored record or
    None (ineligible / no LLM / empty reply). currency: the EVENT currency
    resolved by the caller (e.g. from the decision row via decision_id) —
    the symbol's base currency is only a fallback (wrong for e.g. CAD
    events on USDCAD). Callers run this off the request thread — it makes
    a network call."""
    if chat_fn is None or store is None or not trade_eligible(trade):
        return None
    try:
        text = (chat_fn(GENERATION_SYSTEM,
                        build_generation_user_prompt(trade)) or '').strip()
    except Exception as e:
        logger.warning(f"Reflection generation failed: {e}")
        return None
    if not text:
        return None
    if len(text) > MAX_REFLECTION_CHARS:
        text = text[:MAX_REFLECTION_CHARS].rsplit(' ', 1)[0] + "..."
    record = {
        "timestamp": utcnow().isoformat() + "Z",
        "decision_id": trade.get('decision_id', ''),
        "event_name": trade.get('event_name', ''),
        "currency": (currency or '').upper() or _currency_of(trade),
        "pair": trade.get('symbol', ''),
        "direction": trade.get('direction', ''),
        "profit_usd": trade.get('profit_usd'),
        "reason": trade.get('reason', ''),
        "n_label": "n=1",
        "text": text,
    }
    store.append(record)
    logger.info(f"Reflection recorded for {record['event_name']} "
                f"({record['pair']}): {text[:80]}")
    return record


def _currency_of(trade: Dict) -> str:
    """Event currency if present; else base currency of the symbol."""
    cur = (trade.get('event_currency') or '').upper()
    if cur:
        return cur
    from market_context import normalize_pair
    pair = normalize_pair(str(trade.get('symbol') or ''))
    return pair[:3] if len(pair) >= 6 else ''
