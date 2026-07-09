"""
SkyTower-AI Decision History
Persistent audit log for all trading decisions (BUY/SELL/SKIP).
Stores to JSONL file for easy debugging and review.
"""
import json
import os
from datetime import datetime
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

    def _load_recent(self, max_entries: int = 100):
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
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_name": getattr(decision, 'event', ''),
            "currency": getattr(decision, 'currency', ''),
            "pair": getattr(decision, 'pair', ''),
            "direction": getattr(decision, 'direction', ''),
            "confidence": getattr(decision, 'confidence', 0),
            "forced": getattr(decision, 'forced', False),
            "reasoning": getattr(decision, 'reasoning', ''),
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

        with self._lock:
            # Append to file
            try:
                with open(self._file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry) + '\n')
            except Exception as e:
                logger.error(f"Error writing decision history: {e}")

            # Update in-memory cache
            self._recent.append(entry)
            if len(self._recent) > 200:
                self._recent = self._recent[-100:]

        logger.info(f"Decision recorded: {entry['event_name']} -> {entry['direction']} "
                     f"(confidence: {entry['confidence']:.0%})")

    def get_recent(self, limit: int = 20) -> List[Dict]:
        """Get most recent decisions (newest first)."""
        with self._lock:
            return list(reversed(self._recent[-limit:]))

    def get_today(self) -> List[Dict]:
        """Get all decisions from today (UTC, newest first)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._lock:
            return [d for d in reversed(self._recent) if d['timestamp'].startswith(today)]

    def get_file_path(self) -> str:
        """Return the path to the JSONL file."""
        return self._file_path
