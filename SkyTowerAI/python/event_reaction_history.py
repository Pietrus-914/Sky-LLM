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
from timeutil import utcnow, to_naive_utc
from threading import Lock
from typing import Dict, List, Optional

from loguru import logger

from market_context import pip_size


def is_test_event_name(name: str) -> bool:
    """True for the SKYTOWER_FAKE_EVENT_IN_SECONDS dry-run event.

    Single source of truth for the marker, because normalize_event_name strips
    the parenthesized qualifier: "CPI m/m (FAKE TEST EVENT)" normalizes to
    exactly "cpi m/m", so an unfiltered dry-run record becomes an EXACT-match
    anecdote in front of every future real CPI release. Reactions and price
    paths were already flagged; trades and reflections were not.
    """
    return "FAKE TEST" in (name or "").upper()


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

# Mirror of EventPathRecorder's guard (event_path_recorder.py BACKFILL_MAX_ATTEMPTS):
# without an attempt cap ONE permanently unmatchable row keeps the 15-minute
# calendar fetch loop running around the clock, and ForexFactory 429s when
# hammered. The 25 rows recorded so far are exactly that case — the weekly XML
# feed carries no <actual> element at all, so they can never resolve from it.
BACKFILL_MAX_ATTEMPTS = 12


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
    # Revision check BEFORE any overwrite of the stored value. NUMERIC first:
    # 'previous' is now populated at record time from the decision context while
    # event.previous comes from a feed, so a pure string compare would
    # manufacture phantom revisions on formatting alone ("0.30%" vs "0.3%").
    if record.get('previous') not in (None, '') and event.previous:
        old_n = parse_numeric(record['previous'])
        new_n = parse_numeric(event.previous)
        if old_n is not None and new_n is not None:
            differs = old_n != new_n
        else:
            differs = str(event.previous).strip() != str(record['previous']).strip()
        if differs:
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

    def __init__(self, log_dir: str = None, event_lookup=None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        self._file_path = os.path.join(log_dir, 'event_reactions.jsonl')
        self._lock = Lock()
        self._records: List[Dict] = []
        self.last_backfill_stats: Dict = {}
        self.last_enrich_persisted: bool = True
        # The EA has no calendar and never sends forecast/previous; the SERVER
        # showed both to the model at decision time. "surprise: UNKNOWN" on
        # every record was always a plumbing gap, not missing data.
        self._event_lookup = event_lookup
        self._load()

    def set_event_lookup(self, fn) -> None:
        """Wire the server-side forecast/previous resolver after construction —
        the decision engine builds this store before the server has a calendar
        or a path recorder to resolve against."""
        self._event_lookup = fn

    def _lookup_event(self, reaction: Dict) -> Dict:
        """Resolve forecast/previous from the server's own knowledge. NEVER
        raises and never blocks: a reaction must be stored even when the lookup
        is unwired or broken."""
        if self._event_lookup is None:
            return {}
        try:
            found = self._event_lookup(
                str(reaction.get('decision_id') or ''),
                (reaction.get('currency') or '').upper(),
                reaction.get('event_name') or '',
                (reaction.get('event_time') or '')[:16])
            return found if isinstance(found, dict) else {}
        except Exception as e:
            logger.warning(f"Event lookup for reaction failed: {e}")
            return {}

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

        # Resolved OUTSIDE self._lock: the lookup reaches into stores that hold
        # their own locks, and calling it under this one is a lock-ordering
        # hazard.
        known = self._lookup_event(reaction)

        def resolve(field):
            """EA value wins (it never sends one today), then the server's own
            knowledge of the event."""
            value = reaction.get(field)
            if value not in (None, ''):
                return value, 'ea'
            value = known.get(field)
            if value not in (None, ''):
                return value, known.get('source') or 'server'
            return None, None

        forecast, src_forecast = resolve('forecast')
        previous, src_previous = resolve('previous')
        actual, _ = resolve('actual')
        context_source = src_forecast or src_previous

        entry = {
            "recorded_at": utcnow().isoformat() + "Z",
            # Dry-run reactions must never surface as history for the real
            # event (the fake event's name normalizes identically to it)
            "test": is_test_event_name(reaction.get('event_name')),
            "event_name": reaction.get('event_name', ''),
            "event_name_normalized": normalize_event_name(reaction.get('event_name', '')),
            "currency": (reaction.get('currency') or '').upper(),
            "event_time": reaction.get('event_time', ''),
            "pair": pair,
            # EA echo (F2): joins this reaction to the exact decision that
            # armed it — empty from an old EA build (join falls back to
            # event name + minute matching)
            "decision_id": str(reaction.get('decision_id') or ''),
            "forecast": forecast,
            "previous": previous,
            "actual": actual,
            # 'decision' | 'schedule' | 'path_record' | 'calendar' | 'ea' | None.
            # Without it a null forecast is ambiguous between "the lookup was
            # unwired" and "the calendar genuinely had no forecast".
            "context_source": context_source,
            "surprise": classify_surprise(actual, forecast),
            "surprise_magnitude": surprise_magnitude(actual, forecast),
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

        Rows that no feed row resolves accumulate 'backfill_attempts' and drop
        out after BACKFILL_MAX_ATTEMPTS. The counters are PERSISTED even when
        nothing was filled — otherwise every restart resets them and the cap
        never bites. Diagnostics land on self.last_backfill_stats rather than
        the return value, which callers and tests rely on being an int.
        """
        updated = 0
        attempts_bumped = 0
        sample_unmatched = None
        with self._lock:
            pending = [r for r in self._records
                       if r.get('actual') in (None, '')
                       and not r.get('test')
                       and r.get('backfill_attempts', 0) < BACKFILL_MAX_ATTEMPTS]
            if not pending:
                self.last_backfill_stats = {"examined": 0, "updated": 0,
                                            "unmatched": 0, "sample": None}
                return 0

            for record in pending:
                rec_name = record.get('event_name_normalized', '')
                rec_time = (record.get('event_time') or '')[:16]
                matched = False
                for event in calendar_events:
                    if getattr(event, 'actual', None) in (None, ''):
                        continue
                    if (event.currency or '').upper() != record.get('currency'):
                        continue
                    if normalize_event_name(event.event_name) != rec_name:
                        continue
                    # to_naive_utc mirrors event_path_recorder's matcher; the
                    # [:16] slice already truncates before any UTC offset, so
                    # this only guards a future non-UTC-aware calendar source.
                    if to_naive_utc(event.datetime_utc).isoformat()[:16] != rec_time:
                        continue
                    apply_release_to_record(record, event)
                    updated += 1
                    matched = True
                    break
                if not matched:
                    record['backfill_attempts'] = record.get('backfill_attempts', 0) + 1
                    attempts_bumped += 1
                    if sample_unmatched is None:
                        sample_unmatched = (record.get('currency'), rec_name, rec_time,
                                            record['backfill_attempts'])

            self.last_backfill_stats = {"examined": len(pending), "updated": updated,
                                        "unmatched": attempts_bumped,
                                        "sample": sample_unmatched}
            if (updated or attempts_bumped) and atomic_rewrite_jsonl(
                    self._file_path, self._records):
                if updated:
                    logger.info(f"Backfilled 'actual' for {updated} event reaction(s)")

        return updated

    def enrich_missing_context(self, lookup=None) -> int:
        """
        Fill forecast/previous on records written before the server resolved
        them itself. Independent of backfill_actuals: it needs only the
        server's own decision knowledge, never the network. Returns the number
        of records changed.

        Lookups are collected BEFORE taking the lock, so the rule "never call
        out while holding the store lock" holds here as it does in record().
        """
        lookup = lookup or self._event_lookup
        if lookup is None:
            return 0

        with self._lock:
            targets = [r for r in self._records
                       if not r.get('test')
                       and (r.get('forecast') in (None, '')
                            or r.get('previous') in (None, ''))]

        resolved = []
        for record in targets:
            try:
                known = lookup(record.get('decision_id') or '',
                               (record.get('currency') or '').upper(),
                               record.get('event_name') or '',
                               (record.get('event_time') or '')[:16]) or {}
            except Exception as e:
                logger.debug(f"Reaction enrich lookup failed: {e}")
                continue
            if known:
                resolved.append((record, known))

        updated = 0
        with self._lock:
            for record, known in resolved:
                changed = False
                # forecast/previous ONLY. 'actual' has its own path
                # (backfill_actuals) and writing it here would flip 'surprise'
                # off UNKNOWN, which un-suppresses summarize()'s
                # "(actual X vs forecast Y)" clause — i.e. it would silently
                # turn this maintenance endpoint into a change of what the
                # entry model reads. Keep that door shut in code, not by
                # relying on the feed happening to carry no released values.
                for field in ('forecast', 'previous'):
                    if (record.get(field) in (None, '')
                            and known.get(field) not in (None, '')):
                        record[field] = known[field]
                        changed = True
                if changed:
                    record['context_source'] = known.get('source') or 'server'
                    record['surprise'] = classify_surprise(record.get('actual'),
                                                           record.get('forecast'))
                    record['surprise_magnitude'] = surprise_magnitude(
                        record.get('actual'), record.get('forecast'))
                    updated += 1

            # Persistence is reported separately: returning a count for a write
            # that never reached disk reads as success, and the enrichment
            # would then vanish on the next restart.
            self.last_enrich_persisted = True
            if updated:
                self.last_enrich_persisted = atomic_rewrite_jsonl(
                    self._file_path, self._records)
                if self.last_enrich_persisted:
                    logger.info(
                        f"Filled forecast/previous on {updated} reaction record(s)")
                else:
                    logger.error(
                        f"Enriched {updated} reaction record(s) IN MEMORY ONLY — "
                        f"the rewrite of {os.path.basename(self._file_path)} failed")
        return updated

    def get_file_path(self) -> str:
        return self._file_path
