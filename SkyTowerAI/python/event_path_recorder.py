"""
Event path recorder — measures the post-release M1 price path of EVERY
monitored calendar event (traded, skipped or filtered out by the name
whitelist), server-side, from the M1 candles the EA already pushes to
/api/market-data. No EA involvement, no extra feed fetches.

Why: trades happen 0-2x/day but the calendar produces 10-20 monitored
events/week — recording the reaction to ALL of them grows the learning
sample ~20x faster than trades alone, and finally makes SKIP decisions
evaluable ("what would have happened").

Storage: JSONL at logs/event_paths.jsonl, one record per (event, pair).
Separate from event_reactions.jsonl (EA tick-snapshot reactions): different
producer, richer schema; the two are merged only at aggregation time.

Timing model: measurement opens at T+31 min — the EA pushes the LAST 60 M1
bars every ~60s, so a fresh push at T+31 still covers T-29..T+31, i.e. the
pre-release candles, the release bar and the full 30-minute window. Each pair
is measured independently and only a COMPLETE measurement is written before
give-up (an incomplete one is retried on later ticks — pushes keep arriving);
at T+50 min the recorder accepts stale pushes and partial paths, or writes an
honest no_data record. Spread is snapshotted live around T0 (it cannot be
recovered from bars later).

Broker time: MT5 bar times are BROKER time epochs, not UTC. The broker's UTC
offset is inferred from the newest pushed bar vs the push timestamp (rounded
to 30 min) and stored in each record for auditability.
"""
import json
import os
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional

from loguru import logger

from timeutil import utcnow, to_naive_utc as _naive_utc, utc_epoch as _utc_epoch
from instrument_profiles import symbol_carries_currency as _symbol_carries_currency
from market_context import (pip_size, normalize_pair,
                            infer_broker_offset_seconds, entry_age_seconds)

from calendar_fetcher import is_non_data_event
from event_reaction_history import (normalize_event_name,
                                    is_test_event_name,
                                    apply_release_to_record,
                                    atomic_rewrite_jsonl)


# Single measurement pass: M1x60 still covers T0 at T+31 min
MEASURE_AFTER_SECONDS = 31 * 60
# Stop waiting for a fresh push and write partial/no_data
GIVE_UP_AFTER_SECONDS = 50 * 60
# How far ahead an upcoming event is put on the pending list
SCHEDULE_HORIZON_SECONDS = 2 * 3600
# Spread snapshot window around T0 (spread is not recoverable from bars)
SPREAD_SNAP_WINDOW_SECONDS = 90
# A push counts as "fresh" for measuring when younger than this
FRESH_PUSH_MAX_AGE_SECONDS = 180
# Actuals appear in the FF feed within minutes of release; past this window a
# record without one is unresolvable — stop asking the feed about it
BACKFILL_WINDOW_SECONDS = 48 * 3600
# ... and stop retrying a record the feed repeatedly failed to match at all
BACKFILL_MAX_ATTEMPTS = 12


def measure_path(bar_map: Dict[int, Dict[str, float]], t0_min: int, pip: float,
                 allow_partial: bool) -> Optional[Dict]:
    """
    Pure path math over minute bars — SHARED by the live recorder and the
    historical bootstrap so both datasets are computed identically.

    Args:
        bar_map: minute-epoch -> {open, high, low, close} (floats; epochs in
            the same clock as t0_min — broker time live, UTC historically)
        t0_min: release minute epoch (floored)
        pip: pip size for the pair
        allow_partial: False = only a COMPLETE 30-min window yields a result
            (caller retries later); True = accept a partial path (give-up /
            historical gaps), still requiring at least the 1-min move

    Returns metrics dict (price_at_event, move_{1,5,15,30}min_pips,
    first5_high/low_pips, high/low_30min_pips, pre_release_3m_pips,
    complete) or None when nothing meaningful is measurable.
    """
    if not bar_map:
        return None
    newest = max(bar_map)
    # The data must reach back to the release bar at all
    if min(bar_map) > t0_min:
        return None

    def price_at(t: int) -> Optional[float]:
        """Price at instant t: open of the bar starting at t (set on its
        first tick — valid even while forming), else close of the previous
        minute's bar, but ONLY when a bar at/after t exists proving that
        previous bar closed — the newest bar in a live push is still
        forming, and its close is the push-instant price, not the boundary."""
        b = bar_map.get(t)
        if b is not None:
            return b["open"]
        if newest >= t:
            b = bar_map.get(t - 60)
            if b is not None:
                return b["close"]
        return None

    p0 = price_at(t0_min)
    if p0 is None:
        return None

    def move(minutes: int) -> Optional[float]:
        p = price_at(t0_min + minutes * 60)
        return round((p - p0) / pip, 1) if p is not None else None

    def window_extremes(minutes: int):
        end = t0_min + (minutes - 1) * 60
        # Full confidence needs every window bar CLOSED (a bar at end+60
        # exists); in partial mode accept a still-forming/last bar — its
        # high/low are true observed extremes of a partial minute
        if newest < (end if allow_partial else end + 60):
            return None, None
        window = [b for t, b in bar_map.items() if t0_min <= t <= end]
        # Demand most of the window (quiet single-bar gaps are fine,
        # a half-empty window is not a measurement)
        if len(window) < max(2, minutes - minutes // 5):
            return None, None
        hi = max(b["high"] for b in window)
        lo = min(b["low"] for b in window)
        return round((hi - p0) / pip, 1), round((lo - p0) / pip, 1)

    moves = {m: move(m) for m in (1, 5, 15, 30)}
    first5_high, first5_low = window_extremes(5)
    high30, low30 = window_extremes(30)

    complete = (all(v is not None for v in moves.values())
                and high30 is not None and first5_high is not None)
    if not complete and not allow_partial:
        return None   # caller retries when fuller data exists
    if moves[1] is None:
        # No real path data — a record with just a release price is noise
        return None

    # Pre-release drift over the last 3 full pre-event minutes
    pre_pips = None
    b_start, b_end = bar_map.get(t0_min - 180), bar_map.get(t0_min - 60)
    if b_start is not None and b_end is not None:
        pre_pips = round((b_end["close"] - b_start["open"]) / pip, 1)

    return {
        "price_at_event": p0,
        "move_1min_pips": moves[1],
        "move_5min_pips": moves[5],
        "move_15min_pips": moves[15],
        "move_30min_pips": moves[30],
        "first5_high_pips": first5_high,
        "first5_low_pips": first5_low,
        "high_30min_pips": high30,
        "low_30min_pips": low30,
        "pre_release_3m_pips": pre_pips,
        "complete": complete,
    }


class EventPathRecorder:
    """Thread-safe scheduler + JSONL store for post-event price paths."""

    def __init__(self, log_dir: str = None, regime_provider=None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        # Callable currency -> regime|None (live RegimeTracker); falls back
        # to the static config.CURRENCY_REGIMES seed when not wired (tests)
        self._regime_provider = regime_provider
        self._file_path = os.path.join(log_dir, 'event_paths.jsonl')
        self._lock = Lock()
        self._records: List[Dict] = []      # full history (backfill rewrites)
        self._pending: Dict[str, Dict] = {}  # key -> scheduled measurement
        # Retired/recorded event keys. A plain set: eviction could never
        # re-enable a measurement anyway (scheduling requires a FUTURE event)
        self._done: set = set()
        self._load()

    # ------------------------------------------------------------------
    # Persistence (same tolerant JSONL pattern as EventReactionHistory)
    # ------------------------------------------------------------------

    def _load(self):
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
                # Seed dedup so a restart can't re-measure recorded events
                for r in self._records:
                    key = r.get('event_key')
                    if key:
                        self._done.add(key)
                logger.info(f"Loaded {len(self._records)} event path records")
        except Exception as e:
            logger.error(f"Error loading event paths: {e}")

    def _append(self, records: List[Dict]):
        with self._lock:
            self._records.extend(records)
            try:
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    for r in records:
                        f.write(json.dumps(r) + '\n')
            except Exception as e:
                logger.error(f"Error writing event paths: {e}")

    def get_recent(self, limit: int = 50) -> List[Dict]:
        """Newest first. Returns COPIES — backfill mutates the live dicts in
        the updater thread while Flask may be serializing a response."""
        if limit <= 0:
            return []
        with self._lock:
            return [dict(r) for r in reversed(self._records[-limit:])]

    def get_file_path(self) -> str:
        return self._file_path

    # ------------------------------------------------------------------
    # Scheduling + measurement (driven by the updater loop, every ~15s)
    # ------------------------------------------------------------------

    @staticmethod
    def _event_key(currency: str, event_name: str, event_time: datetime) -> str:
        return (f"{(currency or '').upper()}|{normalize_event_name(event_name)}"
                f"|{_naive_utc(event_time).isoformat()[:16]}")

    def tick(self, events: List, market_snapshot: Dict[str, Dict]):
        """
        One recorder cycle:
        1. put upcoming monitored events on the pending list,
        2. snapshot spread for pending events around T0,
        3. measure pending events past T+31 min from pushed M1 bars.

        Args:
            events: EconomicEvent list (impact-filtered, ANY name — measuring
                non-whitelisted events is the point). Static-source events are
                skipped here (guessed times would produce garbage paths).
            market_snapshot: copy of server market_data_reports
                {PAIR: {"ohlc_multi": {...}, "spread_points": ..., "updated_at": ...}}
        """
        now = utcnow()
        try:
            self._schedule(events, now)
            self._snapshot_spreads(market_snapshot, now)
            self._measure_due(market_snapshot, now)
            self._cleanup(now)
        except Exception as e:
            logger.debug(f"Event path recorder tick failed: {e}")

    def _schedule(self, events: List, now: datetime):
        for event in events or []:
            try:
                if getattr(event, 'source', '') == 'static':
                    continue
                t0 = _naive_utc(event.datetime_utc)
                until = (t0 - now).total_seconds()
                if not 0 < until <= SCHEDULE_HORIZON_SECONDS:
                    continue
                key = self._event_key(event.currency, event.event_name, t0)
                if key in self._pending or key in self._done:
                    continue
                self._pending[key] = {
                    "event_key": key,
                    "event_name": event.event_name,
                    "currency": (event.currency or '').upper(),
                    "event_time": t0,
                    "impact": getattr(event, 'impact', ''),
                    "forecast": getattr(event, 'forecast', None),
                    "previous": getattr(event, 'previous', None),
                    # Speeches/pressers publish no number — they never get an
                    # 'actual', so backfill must not keep asking the feed
                    "non_data": is_non_data_event(event.event_name),
                    "spreads": {},   # pair -> (abs_seconds_from_t0, spread_points)
                    "measured": set(),  # pairs already recorded (per-pair retire)
                }
                logger.info(f"Path recorder scheduled: {event.event_name} "
                            f"({event.currency}) @ {t0.isoformat()}Z")
            except Exception as e:
                logger.debug(f"Path recorder schedule failed for event: {e}")

    @staticmethod
    def _fresh_pairs(market_snapshot: Dict, currency: str, now: datetime,
                     max_age: float) -> List[str]:
        """Pushed pairs containing the currency, no fresher-than-max_age."""
        out = []
        for pair, entry in market_snapshot.items():
            norm = normalize_pair(pair)
            if not _symbol_carries_currency(norm, currency):
                continue
            if entry_age_seconds(entry, now) <= max_age:
                out.append(norm)
        return out

    def _snapshot_spreads(self, market_snapshot: Dict, now: datetime):
        for entry in self._pending.values():
            dist = abs((now - entry["event_time"]).total_seconds())
            if dist > SPREAD_SNAP_WINDOW_SECONDS:
                continue
            for pair in self._fresh_pairs(market_snapshot, entry["currency"],
                                          now, FRESH_PUSH_MAX_AGE_SECONDS):
                spread = market_snapshot[pair].get('spread_points')
                if spread is None:
                    continue
                best = entry["spreads"].get(pair)
                if best is None or dist < best[0]:
                    entry["spreads"][pair] = (dist, spread)

    def _measure_due(self, market_snapshot: Dict, now: datetime):
        for key, entry in list(self._pending.items()):
            elapsed = (now - entry["event_time"]).total_seconds()
            if elapsed < MEASURE_AFTER_SECONDS:
                continue
            give_up = elapsed >= GIVE_UP_AFTER_SECONDS
            measured = entry["measured"]

            # Per-pair, incremental: before give-up only COMPLETE paths are
            # written (incomplete ones retry on later ticks as fresher pushes
            # arrive); the event stays pending so a sibling pair whose push
            # hiccuped at T+31 still gets recorded minutes later. At give-up
            # stale pushes and partial paths are accepted.
            records = []
            pairs = self._fresh_pairs(market_snapshot, entry["currency"], now,
                                      float('inf') if give_up
                                      else FRESH_PUSH_MAX_AGE_SECONDS)
            for pair in pairs:
                if pair in measured:
                    continue
                try:
                    rec = self._measure_pair(entry, pair, market_snapshot[pair],
                                             give_up)
                except Exception as e:
                    # One malformed pair must not poison the others or the tick
                    logger.warning(f"Path measurement failed for {pair} / "
                                   f"{entry['event_name']}: {e}")
                    rec = None
                if rec is not None:
                    records.append(rec)
                    measured.add(pair)

            if records:
                self._append(records)
                for r in records:
                    logger.info(f"Event path recorded: {r['event_name']} "
                                f"({r['currency']}) {r['pair']} "
                                f"1min {r.get('move_1min_pips')} / 5min "
                                f"{r.get('move_5min_pips')} / 30min "
                                f"{r.get('move_30min_pips')} pips "
                                f"[{r['data_status']}]")

            if give_up:
                if not measured:
                    # Nothing measurable through the whole window (EA/MT5
                    # down) — record the gap honestly instead of staying silent
                    self._append([self._base_record(entry, pair=None,
                                                    offset=None,
                                                    status="no_data")])
                    logger.warning(f"Event path NOT measurable (no usable M1): "
                                   f"{entry['event_name']} ({entry['currency']})")
                self._retire(key)

    def _retire(self, key: str):
        self._pending.pop(key, None)
        self._done.add(key)

    def _cleanup(self, now: datetime):
        # Pending entries whose give-up window passed long ago (e.g. clock
        # jump) — drop defensively; _measure_due normally retires them
        dead = [k for k, e in self._pending.items()
                if (now - e["event_time"]).total_seconds() > 2 * GIVE_UP_AFTER_SECONDS]
        for k in dead:
            del self._pending[k]

    # ------------------------------------------------------------------
    # Per-pair measurement from M1 bars
    # ------------------------------------------------------------------

    def _regime_for(self, currency: str) -> Optional[str]:
        if self._regime_provider is not None:
            try:
                return self._regime_provider(currency)
            except Exception as e:
                logger.debug(f"Regime provider failed for {currency}: {e}")
        import config as cfg
        return (getattr(cfg, 'CURRENCY_REGIMES', {}) or {}).get(currency)

    def _base_record(self, entry: Dict, pair: Optional[str],
                     offset: Optional[int], status: str) -> Dict:
        return {
            "recorded_at": utcnow().isoformat() + "Z",
            "source": "server_m1",
            "test": is_test_event_name(entry["event_name"]),
            "event_key": entry["event_key"],
            "event_name": entry["event_name"],
            "event_name_normalized": normalize_event_name(entry["event_name"]),
            "currency": entry["currency"],
            "event_time": entry["event_time"].isoformat(),
            "impact": entry.get("impact", ''),
            "forecast": entry.get("forecast"),
            "previous": entry.get("previous"),
            "actual": None,  # backfilled from the FF feed like reactions are
            "surprise": "UNKNOWN",
            "surprise_magnitude": None,
            "non_data": entry.get("non_data", False),
            "regime": self._regime_for(entry["currency"]),
            "pair": pair,
            "broker_utc_offset_min": (offset // 60) if offset is not None else None,
            "data_status": status,
        }

    def _measure_pair(self, entry: Dict, pair: str, market_entry: Dict,
                      give_up: bool) -> Optional[Dict]:
        """Measure one pair's path. Returns None to mean 'not now' — before
        give-up that defers to a later tick (a fresher push will arrive);
        at give-up it means the pair is unmeasurable."""
        bars = (market_entry.get('ohlc_multi') or {}).get('M1') or []
        if len(bars) < 5:
            return None
        try:
            pushed_at = datetime.fromisoformat(market_entry.get('updated_at', ''))
        except (ValueError, TypeError):
            return None
        offset = infer_broker_offset_seconds(bars, pushed_at)
        if offset is None:
            return None

        pip = pip_size(pair)
        t0_min = (_utc_epoch(entry["event_time"]) // 60) * 60 + offset  # broker epoch
        # Admit only fully-formed bars — a glitched bar missing an OHLC key
        # must not blow up the measurement (or the whole tick)
        bar_map = {}
        for b in bars:
            try:
                bar_map[int(b["time"])] = {k: float(b[k]) for k in
                                           ("open", "high", "low", "close")}
            except (KeyError, TypeError, ValueError):
                continue
        metrics = measure_path(bar_map, t0_min, pip, allow_partial=give_up)
        if metrics is None:
            return None

        record = self._base_record(entry, pair, offset,
                                   "ok" if metrics.pop("complete") else "partial")
        spread_snap = entry["spreads"].get(pair)
        record.update(metrics)
        record["spread_points_t0"] = spread_snap[1] if spread_snap else None
        return record

    # ------------------------------------------------------------------
    # Actual/surprise backfill (same FF-feed flow as EventReactionHistory)
    # ------------------------------------------------------------------

    def _backfillable(self, record: Dict, cutoff_iso: str) -> bool:
        """True when this record still deserves a feed lookup. Speeches
        (non_data) never publish an actual; records older than the backfill
        window or that failed too many passes are unresolvable — without
        these exclusions ONE such record would keep the 15-min FF fetch
        loop running around the clock (and FF already 429s when hammered)."""
        return (record.get('actual') in (None, '')
                and not record.get('test')
                and not record.get('non_data')
                and record.get('data_status') != 'no_data'
                and record.get('backfill_attempts', 0) < BACKFILL_MAX_ATTEMPTS
                and (record.get('event_time') or '') >= cutoff_iso)

    def _backfill_cutoff_iso(self) -> str:
        return (utcnow() - timedelta(seconds=BACKFILL_WINDOW_SECONDS)).isoformat()

    def has_pending_actuals(self) -> bool:
        cutoff = self._backfill_cutoff_iso()
        with self._lock:
            return any(self._backfillable(r, cutoff)
                       for r in self._records[-400:])

    def scheduled_event(self, currency: str, event_name: str,
                        event_minute: str) -> Optional[Dict]:
        """forecast/previous for a monitored event, taken from the SCHEDULE,
        falling back to an already-measured record. Used to label reaction
        records at T+5 with the numbers the decision was made on.

        The schedule entry is created up to SCHEDULE_HORIZON_SECONDS before the
        release and survives well past T+5: per-pair completion deliberately
        does NOT retire it, and retirement happens at give-up (T+50).

        NOTE: _pending is mutated by the updater thread WITHOUT the lock, so
        list() here can still raise mid-iteration — the CALLER must swallow it.
        Taking self._lock here would NOT be enough on its own; closing the race
        properly means bringing _schedule/_retire/_cleanup under the lock too."""
        wanted = normalize_event_name(event_name)
        currency = (currency or '').upper()
        for entry in list(self._pending.values()):
            if entry.get('currency') != currency:
                continue
            if normalize_event_name(entry.get('event_name', '')) != wanted:
                continue
            if _naive_utc(entry['event_time']).isoformat()[:16] != event_minute:
                continue
            return {"forecast": entry.get('forecast'),
                    "previous": entry.get('previous'),
                    "source": "schedule"}
        with self._lock:
            for record in reversed(self._records[-400:]):
                if record.get('currency') != currency:
                    continue
                if record.get('event_name_normalized') != wanted:
                    continue
                if (record.get('event_time') or '')[:16] != event_minute:
                    continue
                return {"forecast": record.get('forecast'),
                        "previous": record.get('previous'),
                        "actual": record.get('actual'),
                        "source": "path_record"}
        return None

    def backfill_actuals(self, calendar_events: List) -> int:
        """Fill 'actual' (+ surprise, revision) from released calendar rows.
        Match: currency + normalized name + minute-precision UTC time.
        Shared apply/rewrite helpers with EventReactionHistory — one source
        of truth for release semantics."""
        updated = 0
        cutoff = self._backfill_cutoff_iso()
        with self._lock:
            pending = [r for r in self._records if self._backfillable(r, cutoff)]
            if not pending:
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
                    if _naive_utc(event.datetime_utc).isoformat()[:16] != rec_time:
                        continue
                    apply_release_to_record(record, event)
                    matched = True
                    updated += 1
                    break
                if not matched:
                    # Count the failed pass; persisted on the next rewrite
                    record['backfill_attempts'] = record.get('backfill_attempts', 0) + 1

            if updated and atomic_rewrite_jsonl(self._file_path, self._records):
                logger.info(f"Backfilled 'actual' for {updated} event path(s)")
        return updated
