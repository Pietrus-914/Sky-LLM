"""
Event reaction history — records how price actually moved after each
economic release, so the entry LLM can learn from past reactions.

Storage: JSONL at logs/event_reactions.jsonl (same pattern as
decision_history.jsonl). The EA measures the reaction (bid at T0, T+60s,
T+300s) and POSTs it to /api/event-reaction; the server computes pips and
backfills the released 'actual' value from the calendar when it appears.
"""
import json
import os
import re
from datetime import datetime
from timeutil import utcnow
from threading import Lock
from typing import Dict, List, Optional

from loguru import logger

from market_context import pip_size


def normalize_event_name(name: str) -> str:
    """
    Normalize an event name for matching across releases:
    lowercase, no parenthesized qualifiers ("(Jun)", "(QoQ)"), no month
    tokens, collapsed whitespace. Keeps m/m vs y/y distinction.
    """
    name = (name or "").lower()
    name = re.sub(r"\([^)]*\)", " ", name)
    months = ("jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
              "january|february|march|april|june|july|august|september|october|november|december")
    name = re.sub(rf"\b({months})\b", " ", name)
    name = re.sub(r"\bq[1-4]\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


_UNIT_MULTIPLIERS = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}


def parse_numeric(value) -> Optional[float]:
    """
    Parse calendar values like '210K', '1.2M', '4.25%', '-0.3%' into floats,
    honoring K/M/B/T magnitude suffixes so '1.2M' vs '900K' compares correctly.
    Shared by classify_surprise and LLMDecisionEngine._compare_values.
    """
    if value is None:
        return None
    s = str(value).strip().upper().replace(",", "")
    multiplier = 1.0
    for suffix, mult in _UNIT_MULTIPLIERS.items():
        if s.endswith(suffix):
            multiplier = mult
            s = s[:-1]
            break
    match = re.search(r"-?\d+(\.\d+)?", s)
    if not match:
        return None
    return float(match.group()) * multiplier


def classify_surprise(actual: Optional[str], forecast: Optional[str]) -> str:
    """BEAT / MISS / INLINE / UNKNOWN by comparing numeric actual vs forecast."""
    a, f = parse_numeric(actual), parse_numeric(forecast)
    if a is None or f is None:
        return "UNKNOWN"
    if a > f:
        return "BEAT"
    if a < f:
        return "MISS"
    return "INLINE"


def surprise_magnitude(actual, forecast) -> Optional[float]:
    """Numeric surprise = actual - forecast (calendar units). The SIGN alone
    (classify_surprise) loses the information that a 2pp beat and a 0.1pp
    beat behave very differently — magnitude is what the standardized-surprise
    statistics need once enough history accumulates."""
    a, f = parse_numeric(actual), parse_numeric(forecast)
    if a is None or f is None:
        return None
    return round(a - f, 6)


def apply_release_to_record(record: Dict, event) -> None:
    """
    Apply a released calendar row to a stored record: fill 'actual' (and a
    missing 'forecast'), detect a revision of the 'previous' reading (a
    second, simultaneous surprise — markets often move on it, not the
    headline), and refresh surprise sign + magnitude.

    SINGLE source of truth for release-matching semantics, shared by
    EventReactionHistory and EventPathRecorder so their datasets can never
    drift apart on surprise/revision labels. Caller has already matched the
    event to the record (name/currency/time) and verified event.actual is set.
    """
    record['actual'] = event.actual
    if record.get('forecast') in (None, '') and event.forecast:
        record['forecast'] = event.forecast
    # Revision check BEFORE any overwrite of the stored value
    if (record.get('previous') not in (None, '')
            and event.previous
            and str(event.previous) != str(record['previous'])):
        record['previous_revised'] = event.previous
        rev = surprise_magnitude(event.previous, record['previous'])
        if rev is not None:
            record['revision_magnitude'] = rev
    if record.get('previous') in (None, '') and event.previous:
        record['previous'] = event.previous
    record['surprise'] = classify_surprise(record['actual'], record.get('forecast'))
    record['surprise_magnitude'] = surprise_magnitude(record['actual'],
                                                      record.get('forecast'))


def atomic_rewrite_jsonl(file_path: str, records: List[Dict]) -> bool:
    """Rewrite a JSONL store atomically (tmp + os.replace) so a crash/kill
    mid-write cannot truncate an accumulated dataset. Returns True on
    success. Shared by all JSONL stores that rewrite in place."""
    tmp_path = file_path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
        os.replace(tmp_path, file_path)
        return True
    except Exception as e:
        logger.error(f"Error rewriting {os.path.basename(file_path)}: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


class EventReactionHistory:
    """Thread-safe JSONL store of post-release price reactions."""

    def __init__(self, log_dir: str = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        self._file_path = os.path.join(log_dir, 'event_reactions.jsonl')
        self._lock = Lock()
        self._records: List[Dict] = []
        self._load()

    def _load(self):
        # Load ALL records: backfill_actuals rewrites the file from memory,
        # so a partial load would silently truncate the accumulated dataset
        # on the first rewrite after a restart. Records are ~300 bytes each;
        # even years of data stay in the low megabytes.
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                self._records.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                logger.info(f"Loaded {len(self._records)} event reaction records")
        except Exception as e:
            logger.error(f"Error loading event reactions: {e}")

    def record(self, reaction: Dict) -> Dict:
        """
        Store one reaction. Expected keys (from /api/event-reaction):
        event_name, currency, event_time, pair,
        price_at_event, price_after_1min, price_after_5min
        Optional: forecast, previous, actual.
        Pip moves are computed here.
        """
        pair = (reaction.get('pair') or '').upper().replace('/', '')
        pip = pip_size(pair)

        def move_pips(later_price):
            base = reaction.get('price_at_event')
            if base and later_price:
                return round((float(later_price) - float(base)) / pip, 1)
            return None

        entry = {
            "recorded_at": utcnow().isoformat() + "Z",
            # Dry-run reactions must never surface as history for the real
            # event (the fake event's name normalizes identically to it)
            "test": "FAKE TEST" in (reaction.get('event_name') or '').upper(),
            "event_name": reaction.get('event_name', ''),
            "event_name_normalized": normalize_event_name(reaction.get('event_name', '')),
            "currency": (reaction.get('currency') or '').upper(),
            "event_time": reaction.get('event_time', ''),
            "pair": pair,
            # EA echo (F2): joins this reaction to the exact decision that
            # armed it — empty from an old EA build (join falls back to
            # event name + minute matching)
            "decision_id": str(reaction.get('decision_id') or ''),
            "forecast": reaction.get('forecast'),
            "previous": reaction.get('previous'),
            "actual": reaction.get('actual'),
            "surprise": classify_surprise(reaction.get('actual'), reaction.get('forecast')),
            "surprise_magnitude": surprise_magnitude(reaction.get('actual'),
                                                     reaction.get('forecast')),
            "price_at_event": reaction.get('price_at_event'),
            "move_1min_pips": move_pips(reaction.get('price_after_1min')),
            "move_5min_pips": move_pips(reaction.get('price_after_5min')),
        }

        with self._lock:
            self._records.append(entry)
            try:
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry) + '\n')
            except Exception as e:
                logger.error(f"Error writing event reaction: {e}")

        logger.info(f"Event reaction recorded: {entry['event_name']} ({entry['currency']}) "
                    f"{entry['surprise']} -> {entry['pair']} {entry['move_5min_pips']} pips/5min")
        return entry

    def get_matching(self, event_name: str, currency: str, limit: int = 10) -> List[Dict]:
        """Past reactions for the same (normalized) event + currency, newest first."""
        wanted = normalize_event_name(event_name)
        currency = (currency or '').upper()
        with self._lock:
            matches = [r for r in reversed(self._records)
                       if not r.get('test')
                       and r.get('currency') == currency
                       and r.get('event_name_normalized') == wanted]
        return matches[:limit]

    def summarize(self, event_name: str, currency: str, limit: int = 4) -> Optional[str]:
        """
        One-line-per-release summary for the LLM prompt, e.g.:
        "2026-06-05 BEAT (actual 210K vs forecast 180K) -> EURUSD -41.5 pips/5min"
        Returns None when there is no history yet.
        """
        matches = self.get_matching(event_name, currency, limit)
        if not matches:
            return None

        lines = []
        for r in matches:
            date = (r.get('event_time') or '')[:10]
            surprise = r.get('surprise', 'UNKNOWN')
            detail = ""
            if r.get('actual') is not None and r.get('forecast') is not None:
                detail = f" (actual {r['actual']} vs forecast {r['forecast']})"
            move5 = r.get('move_5min_pips')
            move1 = r.get('move_1min_pips')
            move_txt = f"{move5} pips/5min" if move5 is not None else (
                f"{move1} pips/1min" if move1 is not None else "move unknown")
            lines.append(f"{date} {surprise}{detail} -> {r.get('pair', '?')} {move_txt}")
        return f"Last {len(lines)} '{event_name}' ({currency}) releases:\n" + "\n".join(lines)

    def summarize_currency_fallback(self, currency: str, limit: int = 10) -> Optional[str]:
        """
        Currency-level behavior prior for events with no direct history yet
        (a first-time event name matches nothing in get_matching). Aggregates
        the most recent reactions of the SAME currency across ALL events:
        average absolute 1-min / 5-min move and how often the 5-min move kept
        the 1-min direction. Explicitly labeled as a volatility prior, not an
        event-specific signal. Returns None with fewer than 3 usable records.
        """
        currency = (currency or '').upper()
        with self._lock:
            usable = [r for r in reversed(self._records)
                      if not r.get('test')
                      and r.get('currency') == currency
                      and r.get('move_5min_pips') is not None][:limit]

        if len(usable) < 3:
            return None

        avg_1min = None
        moves_1 = [abs(r['move_1min_pips']) for r in usable
                   if r.get('move_1min_pips') is not None]
        if moves_1:
            avg_1min = sum(moves_1) / len(moves_1)
        avg_5min = sum(abs(r['move_5min_pips']) for r in usable) / len(usable)

        both = [r for r in usable
                if r.get('move_1min_pips') not in (None, 0)
                and r.get('move_5min_pips') not in (None, 0)]
        continuation_txt = "n/a"
        if both:
            cont = sum(1 for r in both
                       if (r['move_1min_pips'] > 0) == (r['move_5min_pips'] > 0))
            continuation_txt = f"{round(100 * cont / len(both))}%"

        avg_1min_txt = f"{avg_1min:.1f}" if avg_1min is not None else "n/a"
        return (f"No direct history for this event. Currency-level stats for "
                f"{currency} (last {len(usable)} releases, any event, mixed pairs):\n"
                f"avg |1-min move| {avg_1min_txt} pips, avg |5-min move| {avg_5min:.1f} pips, "
                f"1->5min direction continuation {continuation_txt}.\n"
                f"Use only as a volatility/behavior prior, not an event-specific signal.")

    def get_recent(self, limit: int = 50) -> List[Dict]:
        """Newest first. Returns COPIES — backfill mutates the live dicts in
        the updater thread, and a shared reference handed to Flask's jsonify
        can tear mid-serialization."""
        if limit <= 0:
            return []
        with self._lock:
            return [dict(r) for r in reversed(self._records[-limit:])]

    def backfill_actuals(self, calendar_events: List) -> int:
        """
        Fill in 'actual' (and surprise/revision) for records stored before
        the calendar published the released value. calendar_events is a list
        of EconomicEvent objects (may include past events with .actual set).
        Rewrites the JSONL when anything changed. Returns count updated.
        """
        updated = 0
        with self._lock:
            pending = [r for r in self._records
                       if r.get('actual') in (None, '') and not r.get('test')]
            if not pending:
                return 0

            for record in pending:
                rec_name = record.get('event_name_normalized', '')
                rec_time = (record.get('event_time') or '')[:16]
                for event in calendar_events:
                    if getattr(event, 'actual', None) in (None, ''):
                        continue
                    if (event.currency or '').upper() != record.get('currency'):
                        continue
                    if normalize_event_name(event.event_name) != rec_name:
                        continue
                    event_time_str = event.datetime_utc.isoformat()[:16]
                    if event_time_str != rec_time:
                        continue
                    apply_release_to_record(record, event)
                    updated += 1
                    break

            if updated and atomic_rewrite_jsonl(self._file_path, self._records):
                logger.info(f"Backfilled 'actual' for {updated} event reaction(s)")

        return updated

    def get_file_path(self) -> str:
        return self._file_path
