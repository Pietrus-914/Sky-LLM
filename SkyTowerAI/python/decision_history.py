"""
SkyTower-AI Decision History
Persistent audit log for all trading decisions (BUY/SELL/SKIP).
Stores to JSONL file for easy debugging and review.
"""
import json
import os
from datetime import datetime
from timeutil import utcnow
from typing import List, Dict, Optional
from threading import Lock
from loguru import logger


class DecisionHistory:
    """
    Stores every trading decision with full context.
    Persists to a JSONL file (one JSON object per line).
    Thread-safe with Lock.
    """

    def __init__(self, log_dir: str = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        self._file_path = os.path.join(log_dir, 'decision_history.jsonl')
        self._lock = Lock()
        # In-memory cache of recent decisions (last 100)
        self._recent: List[Dict] = []
        self._load_recent()

    def _load_recent(self, max_entries: int = 300):
        """Load most recent entries from disk into memory cache."""
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in lines[-max_entries:]:
                        line = line.strip()
                        if line:
                            try:
                                self._recent.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                logger.info(f"Loaded {len(self._recent)} decision history entries from disk")
        except Exception as e:
            logger.error(f"Error loading decision history: {e}")

    def record(self, decision, data_sources_status: Dict = None):
        """
        Record a trading decision to history.

        Args:
            decision: TradingDecision object
            data_sources_status: Optional dict showing which data sources succeeded/failed
        """
        entry = {
            "timestamp": utcnow().isoformat() + "Z",
            "decision_id": getattr(decision, 'decision_id', ''),
            "event_name": getattr(decision, 'event', ''),
            "currency": getattr(decision, 'currency', ''),
            "pair": getattr(decision, 'pair', ''),
            "direction": getattr(decision, 'direction', ''),
            "confidence": getattr(decision, 'confidence', 0),
            "forced": getattr(decision, 'forced', False),
            "reasoning": getattr(decision, 'reasoning', ''),
            # The LLM's numeric choices — without them SL-sizing and
            # exit-window calibration can never be evaluated later
            "lot_percent": getattr(decision, 'lot_percent', None),
            "stop_loss_pips": getattr(decision, 'stop_loss_pips', None),
            "take_profit_pips": getattr(decision, 'take_profit_pips', None),
            "exit_minutes": getattr(decision, 'exit_minutes_after', None),
            "event_datetime": None,
            "data_sources": data_sources_status or {},
        }

        # Extract event datetime and data source info from data_summary
        data_summary = getattr(decision, 'data_summary', None)
        if data_summary and isinstance(data_summary, dict):
            evt = data_summary.get('event', {})
            entry["event_datetime"] = evt.get('datetime', '')

            # Extract data source status if not provided
            if not entry["data_sources"]:
                cot = data_summary.get('cot_analysis', {})
                sent = data_summary.get('sentiment_analysis', {})
                source_status = data_summary.get('_source_status', {})

                entry["data_sources"] = {
                    "cot_signal": cot.get('signal', 'UNKNOWN') if isinstance(cot, dict) else 'UNKNOWN',
                    "cot_has_data": 'error' not in cot if isinstance(cot, dict) else False,
                    "sentiment_signal": sent.get('signal', 'UNKNOWN') if isinstance(sent, dict) else 'UNKNOWN',
                    "sentiment_pairs": sent.get('pairs_analyzed', 0) if isinstance(sent, dict) else 0,
                    **source_status,
                }

        # Full context dump — everything the model saw (data_summary) plus its
        # raw reply, keyed by decision_id. The JSONL stays slim; this side file
        # is what makes "what did it see vs what happened" answerable later.
        self._dump_context(entry, data_summary, getattr(decision, 'raw_response', ''))

        with self._lock:
            # Append to file
            try:
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry) + '\n')
            except Exception as e:
                logger.error(f"Error writing decision history: {e}")

            # Update in-memory cache. Floor 300: the calibration ledger (F4)
            # reads get_recent(300) — trimming below that would silently
            # shrink the scored sample (and reset the n>=50 prompt gate
            # after restarts, which also load 300 now)
            self._recent.append(entry)
            if len(self._recent) > 600:
                self._recent = self._recent[-300:]

        logger.info(f"Decision recorded: {entry['event_name']} -> {entry['direction']} "
                     f"(confidence: {entry['confidence']:.0%})")

    # Keep at most this many context dumps; oldest are pruned. ~10-20 KB each,
    # a few per day live but one per event in TRADE_ALL+FORCE test phases —
    # without a cap the directory grows unbounded.
    CONTEXT_DUMP_KEEP = 2000

    def _dump_context(self, entry: Dict, data_summary, raw_response: str):
        """Write the decision's full prompt inputs + raw LLM reply to
        logs/decision_context/<decision_id>.json. The JSONL entry is spread
        in whole — single source of truth, so every future field recorded
        there lands in the archive automatically. Best-effort: a dump
        failure must never block recording the decision itself."""
        decision_id = entry.get("decision_id")
        if not decision_id:
            return
        try:
            ctx_dir = os.path.join(os.path.dirname(self._file_path), 'decision_context')
            os.makedirs(ctx_dir, exist_ok=True)
            dump = {
                **entry,
                "raw_response": raw_response or "",
                "data_summary": data_summary if isinstance(data_summary, dict) else None,
            }
            path = os.path.join(ctx_dir, f"{decision_id}.json")
            with open(path, 'w', encoding='utf-8') as f:
                # default=str: data_summary may hold datetimes/dataclasses
                json.dump(dump, f, ensure_ascii=False, default=str)
            self._prune_context_dir(ctx_dir)
        except Exception as e:
            logger.debug(f"Decision context dump failed: {e}")

    def _prune_context_dir(self, ctx_dir: str):
        """Drop the oldest dumps beyond CONTEXT_DUMP_KEEP (mtime order)."""
        try:
            names = [n for n in os.listdir(ctx_dir) if n.endswith('.json')]
            excess = len(names) - self.CONTEXT_DUMP_KEEP
            if excess <= 0:
                return
            paths = [os.path.join(ctx_dir, n) for n in names]
            paths.sort(key=lambda p: os.path.getmtime(p))
            for p in paths[:excess]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception as e:
            logger.debug(f"Decision context prune failed: {e}")

    def get_recent(self, limit: int = 20) -> List[Dict]:
        """Get most recent decisions (newest first)."""
        with self._lock:
            return list(reversed(self._recent[-limit:]))

    def get_today(self) -> List[Dict]:
        """Get all decisions from today (UTC, newest first)."""
        today = utcnow().strftime("%Y-%m-%d")
        with self._lock:
            return [d for d in reversed(self._recent) if d['timestamp'].startswith(today)]

    def get_file_path(self) -> str:
        """Return the path to the JSONL file."""
        return self._file_path
