"""
SkyTower-AI Position Manager
Manages open positions with AI-driven exit decisions and USD-based safety guardrails.
"""
import copy
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from timeutil import utcnow
from typing import Dict, Optional, List
from dataclasses import dataclass, field, asdict
from loguru import logger

from config import POSITION_MANAGEMENT_CONFIG
from position_store import PositionStore, PositionStoreError
from trading_units import forex_pip_size


# How many EA report samples the exit model gets to see. At the 5-15s report
# cadence 12 samples cover roughly the first 1-3 minutes of a fresh position
# and always the most recent stretch — enough to judge recovery, short enough
# to keep the prompt (and the persisted snapshot) small.
PNL_SAMPLE_CAP = 12

# Consecutive reports whose spread must exceed emergency_spread_pips before the
# server liquidates. Entry is capped just under the same threshold, so without
# confirmation one spiked tick at the release closes the trade at the worst
# price of the session. A spread at 2x the threshold still closes immediately.
SPREAD_BREACHES_TO_CLOSE = 2


class PositionConflictError(ValueError):
    """A lifecycle message refers to a different broker position."""


class PositionPersistenceError(RuntimeError):
    """A lifecycle transition could not be committed durably."""


@dataclass
class OpenPosition:
    """Tracks an open position with all data needed for AI exit decisions."""
    ticket: int
    symbol: str
    direction: str              # BUY/SELL
    entry_price: float
    current_price: float
    lots: float
    remaining_lots: float       # after partial close
    sl: float
    tp: float
    profit_usd: float           # P/L in account currency (USD)
    max_profit_usd: float       # highest profit reached
    max_drawdown_usd: float     # deepest drawdown reached
    tick_value: float           # value of 1 tick in USD
    account_balance: float      # for % calculations
    open_time: datetime
    last_update: datetime
    event_name: str
    entry_reasoning: str        # reasoning from entry decision
    # Joins this trade back to its entry decision (decision_history JSONL)
    decision_id: str = ""
    # FORCE_DECISION test-mode trade — must never enter the model's
    # "realized experience" prompt sections as a genuine outcome
    forced: bool = False
    position_id: str = ""
    magic: int = 0
    max_loss_usd: float = 0.0
    recovered: bool = False

    # Market context from EA
    spread_pips: float = 0.0
    zone_bias: float = 0.0
    nearest_resistance: float = 0.0
    nearest_support: float = 0.0

    # AI decision tracking
    ai_decisions: List[Dict] = field(default_factory=list)
    partial_closed: bool = False
    sl_moved_to_be: bool = False
    # P/L already realized by partial closes (estimated from the floating
    # P/L at the moment the EA report shows the volume drop). profit_usd
    # only covers the still-open lots, so guardrails, peak tracking and the
    # exit prompt must work on profit_usd + realized_usd.
    realized_usd: float = 0.0
    # Rolling (minutes, price, TOTAL P/L) samples from EA reports, newest last
    # and capped at PNL_SAMPLE_CAP. The exit prompt used to show a single
    # snapshot, which made its "no signs of recovery" rule unmeasurable.
    pnl_samples: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['open_time'] = self.open_time.isoformat()
        d['last_update'] = self.last_update.isoformat()
        return d


@dataclass
class PositionCommand:
    """Command to send to EA for position management."""
    action: str             # HOLD, MODIFY_SL, MODIFY_TP, PARTIAL_CLOSE, CLOSE
    sl_price: float = 0.0
    tp_price: float = 0.0
    close_percent: float = 0.0  # for PARTIAL_CLOSE (25-75)
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict:
        return {
            "action": self.action,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "close_percent": self.close_percent,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class PositionManager:
    """
    Manages open positions with:
    - USD-based safety guardrails (immediate, no LLM needed)
    - LLM exit decisions (every ~30s for strategic analysis)
    - Daily P/L tracking
    """

    def __init__(self, exit_engine=None, history_file: Optional[str] = None,
                 state_file: Optional[str] = None, forced_lookup=None):
        self.position: Optional[OpenPosition] = None
        self.pending_command: Optional[PositionCommand] = None
        self.lock = threading.Lock()
        self.recovery_state: str = "ready"
        self._closed_position_keys: set[str] = set()

        # Daily tracking
        self.daily_pnl_usd: float = 0.0
        self.daily_trades: int = 0
        self.daily_reset_date: str = ""

        # LLM timing
        self.last_llm_check: float = 0.0
        self.exit_engine = exit_engine  # ExitDecisionEngine instance

        # Background exit-LLM dispatch. A networked exit engine must NEVER run
        # inside the /api/position/report request: the EA's POST timeout is 10s
        # while the exit LLM is allowed 30s, so a slow-but-valid CLOSE used to
        # be written to a socket the EA had already abandoned — the command was
        # lost, no retry for another 30s, and MQL5's synchronous WebRequest
        # meant the EA processed no ticks (and ran no local guardrail) while it
        # waited. Now the call runs on a worker and the result is QUEUED in
        # pending_command for the next report, which is the same redelivery
        # path the guardrail commands already used.
        self._llm_thread: Optional[threading.Thread] = None
        self._llm_inflight: bool = False

        # Consecutive emergency-spread breaches on the current position
        self._spread_breaches: int = 0

        # Config
        self.config = POSITION_MANAGEMENT_CONFIG

        # decision_id -> was that entry decision forced? Used only when a
        # reconcile report omits the flag (older EA build); see resolve_forced.
        self.forced_lookup = forced_lookup

        # Trade history (for daily tracking)
        self.closed_trades: List[Dict] = []

        # Persistent trade log (survives restarts and UTC-midnight resets).
        # None (default, e.g. in tests) = in-memory only.
        self.history_file = history_file
        self.position_store = PositionStore(state_file)
        self.recent_trades: List[Dict] = []  # newest last, capped
        if self.history_file:
            try:
                os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            except OSError as e:
                logger.error(f"Could not create trade history dir: {e}")
        self._load_history()
        self._load_active_position()

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _position_key(data: Dict) -> str:
        position_id = str(data.get("position_id") or "").strip()
        if position_id and position_id != "0":
            return f"position:{position_id}"
        ticket = str(data.get("ticket") or "").strip()
        if ticket and ticket != "0":
            return f"ticket:{ticket}"
        return ""

    @staticmethod
    def _position_keys(data: Dict) -> set[str]:
        """Return every durable identity alias carried by a broker record."""
        keys: set[str] = set()
        position_id = str(data.get("position_id") or "").strip()
        ticket = str(data.get("ticket") or "").strip()
        if position_id and position_id != "0":
            keys.add(f"position:{position_id}")
        if ticket and ticket != "0":
            keys.add(f"ticket:{ticket}")
        return keys

    @staticmethod
    def _safe_datetime(value, fallback: Optional[datetime] = None) -> datetime:
        fallback = fallback or utcnow()
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        if isinstance(value, (int, float)) or (
                isinstance(value, str) and value.strip().isdigit()):
            try:
                parsed = datetime.fromtimestamp(
                    float(value), tz=timezone.utc
                ).replace(tzinfo=None)
            except (ValueError, OSError, OverflowError):
                return fallback
            # A broker-time epoch mislabeled as UTC lands HOURS in the
            # future and would make minutes_open negative for the whole
            # trade (time-based exits would never fire). An open_time
            # in the future is always wrong — clamp to the fallback.
            if parsed > utcnow() + timedelta(minutes=5):
                logger.warning(
                    f"open_time epoch {value} is in the future "
                    "(broker-time epoch?) — using fallback time"
                )
                return fallback
            return parsed
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed
            except ValueError:
                return fallback
        return fallback

    def _load_active_position(self) -> None:
        """Load metadata as unconfirmed until a live EA snapshot matches it."""
        try:
            payload = self.position_store.load()
        except PositionStoreError as exc:
            self.recovery_state = "error"
            logger.error(f"{exc}; new entries are blocked until reconciled")
            return
        if payload is None:
            return
        if payload.get("recovery_state") == "closing":
            record = payload.get("close_record")
            if not isinstance(record, dict):
                self.recovery_state = "error"
                logger.error(
                    "Closing tombstone has no close record; new entries blocked"
                )
                return
            keys = self._position_keys(record)
            key = self._position_key(record)
            if keys and not keys.intersection(self._closed_position_keys):
                if not self._write_history_line(record):
                    self.recovery_state = "error"
                    logger.error(
                        "Could not finalize closing tombstone; new entries blocked"
                    )
                    return
            try:
                self.position_store.clear()
            except PositionStoreError as exc:
                self.recovery_state = "error"
                logger.error(str(exc))
                return
            # Rebuild counters from the now-complete history record.
            self._load_history()
            self.recovery_state = "ready"
            logger.warning(
                f"Finalized interrupted close transaction for {key or 'unknown'}"
            )
            return
        try:
            stored = payload["position"]
            self.position = self._position_from_data(
                stored,
                entry_reasoning=str(stored.get("entry_reasoning") or ""),
                decision_id=str(stored.get("decision_id") or ""),
                forced=bool(stored.get("forced", False)),
                recovered=True,
            )
            self._reset_daily_if_needed()
            # Closed-history reconstruction does not include a still-open
            # trade. Count it once in this new process so a restart does not
            # weaken the daily limit.
            self.daily_trades += 1
            self.recovery_state = "pending"
            logger.warning(
                "Restored active-position metadata for "
                f"{self.position.symbol} position_id={self.position.position_id}; "
                "waiting for MT5 reconciliation"
            )
        except Exception as exc:
            self.position = None
            self.recovery_state = "error"
            logger.error(
                f"Active-position snapshot is invalid ({exc}); "
                "new entries are blocked until reconciled"
            )

    def _persist_active_position(self, position: Optional[OpenPosition] = None,
                                 recovery_state: str = "open") -> bool:
        position = position or self.position
        if position is None:
            return False
        if self.recovery_state == "closing":
            # A closing tombstone is a write barrier. No delayed report or
            # LLM result may downgrade it back to an open snapshot.
            logger.warning("Ignored active snapshot write while close is pending")
            return False
        try:
            self.position_store.save(position.to_dict(), recovery_state)
            return True
        except PositionStoreError as exc:
            # Keep managing the broker position. Persistence failure blocks
            # future entries after restart, not the current local guardrails.
            self.recovery_state = "error"
            logger.error(str(exc))
            return False

    def _clear_active_position(self) -> None:
        try:
            self.position_store.clear()
        except PositionStoreError as exc:
            self.recovery_state = "error"
            logger.error(str(exc))

    def _position_from_data(self, data: Dict, entry_reasoning: str = "",
                            decision_id: str = "", forced: bool = False,
                            recovered: bool = False) -> OpenPosition:
        ticket = self._safe_int(data.get("ticket"))
        position_id = str(data.get("position_id") or ticket)
        symbol = str(data.get("symbol") or "").strip()
        direction = str(data.get("direction") or "").upper()
        entry_price = self._safe_float(data.get("entry_price"))
        lots = self._safe_float(data.get("lots", data.get("remaining_lots")))
        remaining_lots = self._safe_float(data.get("remaining_lots", lots))
        if (ticket <= 0 or not position_id or not symbol
                or direction not in ("BUY", "SELL")
                or not math.isfinite(entry_price) or entry_price <= 0
                or not math.isfinite(lots) or lots <= 0
                or not math.isfinite(remaining_lots) or remaining_lots <= 0):
            raise ValueError("invalid broker position snapshot")

        now = utcnow()
        profit = self._safe_float(data.get("profit_usd"))
        realized = self._safe_float(data.get("realized_usd"))
        total_pnl = profit + realized
        max_loss = self._safe_float(data.get("max_loss_usd"))
        if max_loss <= 0:
            max_loss = self._safe_float(
                self.config.get("max_loss_usd", 100.0)
            )

        return OpenPosition(
            ticket=ticket,
            position_id=position_id,
            magic=self._safe_int(data.get("magic")),
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            current_price=self._safe_float(
                data.get("current_price", entry_price)
            ) or entry_price,
            lots=lots,
            remaining_lots=remaining_lots,
            sl=self._safe_float(data.get("sl")),
            tp=self._safe_float(data.get("tp")),
            profit_usd=profit,
            max_profit_usd=max(
                self._safe_float(data.get("max_profit_usd")), total_pnl, 0.0
            ),
            max_drawdown_usd=min(
                self._safe_float(data.get("max_drawdown_usd")), total_pnl, 0.0
            ),
            tick_value=self._safe_float(data.get("tick_value", 10.0)),
            account_balance=self._safe_float(data.get("account_balance")),
            open_time=self._safe_datetime(
                data.get("open_time", data.get("open_time_unix")), now
            ),
            last_update=now,
            event_name=str(
                data.get("event_name")
                or ("(recovered after restart)" if recovered else "")
            ),
            entry_reasoning=entry_reasoning,
            decision_id=decision_id or str(data.get("decision_id") or ""),
            forced=forced or bool(data.get("forced", False)),
            max_loss_usd=max_loss,
            recovered=recovered,
            spread_pips=self._safe_float(data.get("spread_pips")),
            zone_bias=self._safe_float(data.get("zone_bias")),
            nearest_resistance=self._safe_float(data.get("nearest_resistance")),
            nearest_support=self._safe_float(data.get("nearest_support")),
            ai_decisions=list(data.get("ai_decisions") or []),
            partial_closed=bool(data.get("partial_closed", False)),
            sl_moved_to_be=bool(data.get("sl_moved_to_be", False)),
            realized_usd=realized,
            # Restored with the rest of the trail: after a mid-trade restart
            # the exit model would otherwise see "no samples yet" and lose the
            # recovery evidence its cut-loss rule depends on.
            pnl_samples=list(data.get("pnl_samples") or [])[-PNL_SAMPLE_CAP:],
        )

    @staticmethod
    def _same_broker_position(position: OpenPosition, data: Dict) -> bool:
        reported_id = str(data.get("position_id") or "")
        if position.position_id and reported_id:
            return position.position_id == reported_id
        try:
            return position.ticket == int(data.get("ticket"))
        except (TypeError, ValueError, OverflowError):
            return False

    def _merge_live_snapshot(self, data: Dict, recovered: bool = False,
                             merge_volume: bool = True) -> None:
        """Refresh broker-owned fields without erasing server metadata."""
        if self.position is None:
            return
        position = self.position
        reported_symbol = str(data.get("symbol") or position.symbol)
        reported_direction = str(
            data.get("direction") or position.direction
        ).upper()
        if (reported_symbol != position.symbol
                or reported_direction != position.direction):
            raise ValueError("position identity conflicts with active snapshot")

        position.ticket = self._safe_int(data.get("ticket")) or position.ticket
        position.position_id = str(
            data.get("position_id") or position.position_id or position.ticket
        )
        position.magic = self._safe_int(data.get("magic")) or position.magic
        position.current_price = self._safe_float(
            data.get("current_price", position.current_price)
        ) or position.current_price
        if merge_volume:
            position.remaining_lots = self._safe_float(
                data.get(
                    "remaining_lots", data.get("lots", position.remaining_lots)
                )
            ) or position.remaining_lots
        position.lots = max(
            position.lots,
            self._safe_float(data.get("lots", position.lots)),
            position.remaining_lots,
        )
        position.sl = self._safe_float(data.get("sl", position.sl))
        position.tp = self._safe_float(data.get("tp", position.tp))
        position.tick_value = self._safe_float(
            data.get("tick_value", position.tick_value)
        )
        position.account_balance = self._safe_float(
            data.get("account_balance", position.account_balance)
        )
        if self._safe_float(data.get("max_loss_usd")) > 0:
            position.max_loss_usd = self._safe_float(data["max_loss_usd"])
        if not position.decision_id:
            position.decision_id = str(data.get("decision_id") or "")
        if not position.event_name or position.event_name.startswith("(recovered"):
            position.event_name = str(
                data.get("event_name") or position.event_name
            )
        position.recovered = position.recovered or recovered
        position.last_update = utcnow()

    def _load_history(self) -> None:
        """Rebuild daily counters and the recent-trades list from the
        persistent JSONL trade log (so a watchdog restart doesn't wipe
        the dashboard statistics). Any corruption degrades gracefully —
        a broken history file must never keep the server from starting.

        Note: reconstruction attributes a trade to the UTC day it CLOSED
        (live counting attributes it to the day it opened) — a trade held
        across UTC midnight can shift by one day after a restart. With the
        low daily trade counts involved this boundary skew is accepted."""
        if not self.history_file or not os.path.exists(self.history_file):
            return
        records: List[Dict] = []
        try:
            with open(self.history_file, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except Exception as e:
            logger.warning(f"Could not read trade history {self.history_file}: {e}")
            return

        try:
            self._closed_position_keys = set().union(
                *(self._position_keys(record) for record in records)
            ) if records else set()
            self.recent_trades = records[-50:]

            today = utcnow().strftime("%Y-%m-%d")
            todays = [r for r in records
                      if str(r.get("closed_at", "")).startswith(today)]
            self.closed_trades = todays
            # daily_trades counts opened positions; after a restart the best
            # reconstruction is the number of trades closed today (an open
            # position does not survive a restart anyway)
            self.daily_trades = len(todays)
            self.daily_pnl_usd = sum(self._safe_float(r.get("profit_usd"))
                                     for r in todays)
            self.daily_reset_date = today
            if todays:
                logger.info(f"Restored daily stats from trade history: "
                            f"{self.daily_trades} trades, ${self.daily_pnl_usd:.2f} P/L")
        except Exception as e:
            logger.warning(f"Trade history {self.history_file} unusable ({e}); "
                           f"starting with empty daily stats")
            self.recent_trades = []
            self.closed_trades = []
            self.daily_trades = 0
            self.daily_pnl_usd = 0.0

    @staticmethod
    def _safe_float(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _write_history_line(self, record: Dict) -> bool:
        """Append one closed trade to the persistent JSONL log.
        Called OUTSIDE self.lock — a stalled disk write must not block
        can_open_trade()/update_position() (they gate the EA's signal
        polling during the entry window)."""
        if not self.history_file:
            return True
        try:
            with open(self.history_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            logger.error(f"Could not write trade history {self.history_file}: {e}")
            return False

    def _reset_daily_if_needed(self):
        """Reset daily counters at midnight UTC."""
        today = utcnow().strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            self.daily_pnl_usd = 0.0
            self.daily_trades = 0
            self.closed_trades = []
            self.daily_reset_date = today
            logger.info(f"Daily counters reset for {today}")

    def can_open_trade(self) -> tuple[bool, str]:
        """Check if a new trade is allowed based on daily limits."""
        with self.lock:
            self._reset_daily_if_needed()

            if self.recovery_state in (
                    "pending", "pending_close", "closing", "conflict", "error"):
                return False, f"Position recovery not resolved: {self.recovery_state}"

            if self.position is not None:
                return False, "Position already open"

            max_daily_loss = self.config.get("max_daily_loss_usd", 300.0)
            if self.daily_pnl_usd < -max_daily_loss:
                return False, f"Daily loss limit reached: ${self.daily_pnl_usd:.2f} (max -${max_daily_loss})"

            max_trades = self.config.get("max_daily_trades", 5)
            if self.daily_trades >= max_trades:
                return False, f"Daily trade limit reached: {self.daily_trades}/{max_trades}"

            return True, "OK"

    def register_untracked_trade(self) -> None:
        """Count a trade against the daily limit when the EA could not
        deliver the full /api/position/opened report and fell back to the
        bare /api/trade-executed ping. Without this, a failed report would
        leave the panel's max_daily_trades unenforced for the rest of the
        day (the EA no longer has its own per-chart gate)."""
        with self.lock:
            self._reset_daily_if_needed()
            self.daily_trades += 1
            logger.warning(f"Untracked trade counted against daily limit "
                           f"({self.daily_trades} today) — EA used the "
                           f"fallback notification, no position management")

    def on_position_opened(self, data: Dict, entry_reasoning: str = "",
                           decision_id: str = "", forced: bool = False,
                           recovered: bool = False) -> str:
        """Idempotently register or reconcile a broker position.

        Returns ``opened``, ``reconciled`` or ``conflict``. A repeated open
        notification for the same broker position never increments daily
        counters or erases server-only metadata.
        """
        with self.lock:
            self._reset_daily_if_needed()
            if self.recovery_state == "closing":
                return "conflict"
            if self.position is not None:
                if not self._same_broker_position(self.position, data):
                    self.recovery_state = "conflict"
                    logger.error(
                        "Position conflict: server tracks "
                        f"{self.position.position_id or self.position.ticket}, "
                        "but MT5 reported "
                        f"{data.get('position_id') or data.get('ticket')}"
                    )
                    return "conflict"
                try:
                    was_pending = self.recovery_state == "pending"
                    self._merge_live_snapshot(
                        data,
                        recovered=recovered or was_pending,
                        merge_volume=not was_pending,
                    )
                except ValueError as exc:
                    self.recovery_state = "conflict"
                    logger.error(f"Position reconciliation conflict: {exc}")
                    return "conflict"
                if entry_reasoning and not self.position.entry_reasoning:
                    self.position.entry_reasoning = entry_reasoning
                if decision_id and not self.position.decision_id:
                    self.position.decision_id = decision_id
                self.position.forced = self.position.forced or forced
                self.recovery_state = "ready"
                result = "reconciled"
                logger.info(
                    f"Position reconciled: {self.position.direction} "
                    f"{self.position.symbol} | position_id="
                    f"{self.position.position_id}"
                )
            else:
                self.position = self._position_from_data(
                    data,
                    entry_reasoning=entry_reasoning,
                    decision_id=decision_id,
                    forced=forced,
                    recovered=recovered,
                )
                self.daily_trades += 1
                self.pending_command = None
                self.last_llm_check = 0.0
                self._spread_breaches = 0
                self.recovery_state = "ready"
                result = "opened"

                logger.info(
                    f"Position opened: {self.position.direction} "
                    f"{self.position.symbol} @ {self.position.entry_price} | "
                    f"{self.position.lots} lots | ticket={self.position.ticket} | "
                    f"position_id={self.position.position_id}"
                )

            if not self._persist_active_position(self.position):
                raise PositionPersistenceError(
                    "Position is tracked in memory but active snapshot failed"
                )
        return result

    def reconcile_empty(self) -> Dict:
        """Reconcile an EA that currently sees no owned broker position."""
        with self.lock:
            if self.recovery_state == "closing":
                return {
                    "reconciliation": "closing",
                    "position_id": (
                        self.position.position_id if self.position else ""
                    ),
                    "ticket": (
                        str(self.position.ticket) if self.position else ""
                    ),
                    "allow_new_trades": False,
                }
            if self.position is not None:
                self.recovery_state = "pending_close"
                return {
                    "reconciliation": "pending_close",
                    "position_id": self.position.position_id,
                    "ticket": str(self.position.ticket),
                    "allow_new_trades": False,
                }

            # A corrupt snapshot could not be parsed into self.position. An
            # explicit empty broker observation is the authority needed to
            # clear it without guessing.
            self._clear_active_position()
            if self.recovery_state != "error":
                self.recovery_state = "ready"
            else:
                # _clear_active_position leaves error only when deletion fails.
                try:
                    if self.position_store.path and os.path.exists(
                            self.position_store.path):
                        return {
                            "reconciliation": "error",
                            "allow_new_trades": False,
                        }
                except OSError:
                    return {
                        "reconciliation": "error",
                        "allow_new_trades": False,
                    }
                self.recovery_state = "ready"
            return {
                "reconciliation": "empty",
                "allow_new_trades": True,
            }

    def on_position_closed(self, data: Dict) -> Optional[Dict]:
        """Commit a close exactly once and never apply close A to position B.

        A durable ``closing`` tombstone is written before the history append.
        Startup finalizes an interrupted transaction, so no crash window can
        lose both the active snapshot and the realized P/L.
        """
        with self.lock:
            self._reset_daily_if_needed()
            if not self._position_key(data):
                raise ValueError(
                    "Close report requires position_id or ticket"
                )
            if self.position is not None:
                if not self._same_broker_position(self.position, data):
                    raise PositionConflictError(
                        "Close report does not match the active position"
                    )
                key = self._position_key(self.position.to_dict())
            else:
                key = self._position_key(data)

            incoming_keys = self._position_keys(data)
            if incoming_keys.intersection(self._closed_position_keys):
                logger.info(
                    "Duplicate close ignored for "
                    f"{sorted(incoming_keys)}"
                )
                return None

            profit = self._safe_float(data.get("profit"))
            record = {
                "ticket": data.get("ticket", 0),
                "position_id": str(data.get("position_id") or ""),
                "magic": self._safe_int(data.get("magic")),
                "symbol": data.get("symbol", "?"),
                "direction": data.get("direction", "?"),
                "lots": data.get("lots", 0.0),
                "event_name": "(position lost in restart)",
                "profit_usd": profit,
                "profit_source": data.get("profit_source", "unknown"),
                "reason": data.get("reason", "unknown"),
                "opened_at": "",
                "closed_at": utcnow().isoformat(),
                "decisions_count": 0,
                "decision_id": str(data.get("decision_id") or ""),
                "forced": False,
                "max_loss_usd": 0.0,
                "recovered": False,
                "entry_price": 0.0,
                "close_price": self._safe_float(data.get("close_price")),
                "sl": 0.0,
                "tp": 0.0,
                "max_profit_usd": 0.0,
                "max_drawdown_usd": 0.0,
                "realized_usd": 0.0,
                "spread_pips": 0.0,
                "entry_reasoning": "",
                "ai_decisions": [],
            }

            was_orphaned = self.position is None
            if self.position is not None:
                position = self.position
                record.update({
                    "ticket": position.ticket,
                    "position_id": position.position_id,
                    "magic": position.magic,
                    "symbol": position.symbol,
                    "direction": position.direction,
                    "lots": position.lots,
                    "event_name": position.event_name,
                    "opened_at": position.open_time.isoformat(),
                    "decisions_count": len(position.ai_decisions),
                    "decision_id": (
                        position.decision_id
                        or str(data.get("decision_id") or "")
                    ),
                    "forced": position.forced,
                    "max_loss_usd": position.max_loss_usd,
                    "recovered": position.recovered,
                    "entry_price": position.entry_price,
                    "sl": position.sl,
                    "tp": position.tp,
                    "max_profit_usd": position.max_profit_usd,
                    "max_drawdown_usd": position.max_drawdown_usd,
                    "realized_usd": position.realized_usd,
                    "spread_pips": position.spread_pips,
                    "entry_reasoning": position.entry_reasoning,
                    "ai_decisions": list(position.ai_decisions),
                })
                key = self._position_key(record)

            tombstone_position = (
                self.position.to_dict() if self.position is not None
                else {
                    "ticket": record["ticket"],
                    "position_id": record["position_id"],
                }
            )
            try:
                self.position_store.save(
                    tombstone_position,
                    recovery_state="closing",
                    close_record=record,
                )
                self.recovery_state = "closing"
            except PositionStoreError as exc:
                self.recovery_state = "error"
                raise PositionPersistenceError(str(exc)) from exc

            if not self._write_history_line(record):
                raise PositionPersistenceError(
                    "Close tombstone saved but history append failed"
                )

            self._closed_position_keys.update(self._position_keys(record))
            self.daily_pnl_usd += profit
            if was_orphaned:
                self.daily_trades += 1
            self.closed_trades.append(record)
            self.recent_trades.append(record)
            self.recent_trades = self.recent_trades[-50:]
            self.position = None
            self.pending_command = None

            try:
                self.position_store.clear()
                self.recovery_state = "ready"
            except PositionStoreError as exc:
                # History is already durable. Startup can remove the leftover
                # tombstone without duplicating P/L.
                self.recovery_state = "error"
                logger.error(str(exc))

            logger.info(
                f"Position close committed: {record['symbol']} | "
                f"P/L: ${profit:.2f} | key={key or 'legacy-unkeyed'} | "
                f"Daily P/L: ${self.daily_pnl_usd:.2f}"
            )
            return record

    def update_position(self, data: Dict) -> Dict:
        """
        Update position from EA report and return command.
        This is the main method called on every EA report cycle (5-15s).

        Returns dict with command for EA. Always includes 'command' object
        with at minimum {"action": "HOLD"} for consistent JSON format.

        BUG-1 FIX: LLM call happens OUTSIDE lock to avoid blocking server.
        BUG-4 FIX: Always returns {"has_command": bool, "command": {...}} format.
        BUG-5 FIX: Validates ticket before updating position data.
        """
        pos_snapshot = None
        snapshot_key = ""
        needs_llm = False

        with self.lock:
            if self.recovery_state == "closing":
                return self._hold_response()

        # A full broker snapshot can rebuild state after a Python restart.
        # Ordinary incomplete reports remain HOLD-only when nothing is
        # tracked, preserving the old endpoint behaviour for legacy clients.
        if data.get("reconcile"):
            with self.lock:
                needs_reconcile = (
                    self.position is None or self.recovery_state != "ready"
                )
            if needs_reconcile:
                try:
                    outcome = self.on_position_opened(
                        data,
                        decision_id=str(data.get("decision_id") or ""),
                        forced=self.resolve_forced(data),
                        recovered=True,
                    )
                except PositionPersistenceError as exc:
                    # The snapshot write failing must not disable live
                    # guardrails: the position IS tracked in memory, and a
                    # raise here would turn EVERY report into HOLD (each
                    # report carries reconcile:true and recovery_state stays
                    # 'error'), silently skipping max-loss/max-hold/exit
                    # management for the position's whole life. Persistence
                    # failure blocks NEW entries, not the current trade.
                    logger.error(
                        f"Snapshot persist failed during reconcile ({exc}); "
                        "continuing live guardrails on the in-memory position"
                    )
                    outcome = "reconciled" if self.position is not None else "conflict"
                if outcome == "conflict":
                    return self._hold_response()

        # === PHASE 1: Update data + check guardrails (under lock, fast) ===
        with self.lock:
            if self.recovery_state == "closing" or self.position is None:
                return self._hold_response()

            # BUG-5 FIX: Validate ticket matches current position
            has_reported_identity = (
                data.get("position_id") not in (None, "")
                or data.get("ticket") not in (None, "")
            )
            if (has_reported_identity
                    and not self._same_broker_position(self.position, data)):
                logger.warning(f"Ticket mismatch: expected {self.position.ticket}, "
                               f"got {data.get('ticket')}. Ignoring update.")
                return self._hold_response()

            # Partial-close realization: when the reported volume drops (AI
            # PARTIAL_CLOSE or a manual partial in MT5), the floating P/L of
            # the closed fraction leaves the report stream. Credit it as
            # realized BEFORE profit_usd is overwritten with the remaining
            # lots' floating value — otherwise the peak-drop guardrail sees a
            # phantom ~50% collapse right after the partial and force-closes
            # the rest (2026-07-22 GBPUSD: $201.60 peak -> $96 floating).
            reported_lots = data.get("remaining_lots", self.position.remaining_lots)
            prev_lots = self.position.remaining_lots
            if prev_lots > 0 and reported_lots < prev_lots - 1e-9:
                # PREFER the broker's own number. The EA sends realized_usd
                # from the deal history (GetRealizedPnL) in every report, and
                # it includes the actual fill, swap and commission. The
                # fraction-of-previous-floating estimate below is up to one
                # report cycle (5-15s) stale and pre-fill, so on a fast
                # post-news move it could be tens of dollars off — and every
                # intra-trade guardrail then runs on that error. After a
                # restart the stale value can even be the pre-restart
                # snapshot, making the estimate permanently wrong.
                # A ZERO is not proof: MT5's deal history can lag the fill by a
                # moment, so the first report after a partial may still carry
                # 0.0. Estimate then, and adopt the broker figure as soon as it
                # materializes (see the re-sync below).
                broker_realized = self._safe_float(data.get("realized_usd"))
                if (math.isfinite(broker_realized)
                        and abs(broker_realized) > 1e-9):
                    realized_delta = broker_realized - self.position.realized_usd
                    self.position.realized_usd = broker_realized
                    source = "broker deal history"
                else:
                    closed_fraction = 1.0 - (reported_lots / prev_lots)
                    realized_delta = self.position.profit_usd * closed_fraction
                    self.position.realized_usd += realized_delta
                    source = "estimated from last floating P/L"
                self.position.partial_closed = True
                logger.info(f"Partial close detected: {prev_lots:.2f} -> "
                            f"{reported_lots:.2f} lots | ${realized_delta:.2f} "
                            f"credited as realized ({source}; total realized: "
                            f"${self.position.realized_usd:.2f})")
            elif self.position.partial_closed:
                # Re-sync: the EA keeps refreshing realized_usd from the deal
                # history while a partial exists, so replace any earlier
                # estimate with the broker's number once it is available. Every
                # guardrail below reads profit_usd + realized_usd.
                broker_realized = self._safe_float(data.get("realized_usd"))
                if (math.isfinite(broker_realized)
                        and abs(broker_realized) > 1e-9
                        and abs(broker_realized - self.position.realized_usd) > 0.01):
                    logger.info(
                        f"Realized P/L re-synced from broker history: "
                        f"${self.position.realized_usd:.2f} -> "
                        f"${broker_realized:.2f}"
                    )
                    self.position.realized_usd = broker_realized

            # Update position data from EA report
            self.position.current_price = data.get("current_price", self.position.current_price)
            self.position.remaining_lots = data.get("remaining_lots", self.position.remaining_lots)
            self.position.sl = data.get("sl", self.position.sl)
            self.position.tp = data.get("tp", self.position.tp)
            self.position.profit_usd = data.get("profit_usd", self.position.profit_usd)
            self.position.tick_value = data.get("tick_value", self.position.tick_value)
            self.position.account_balance = data.get("account_balance", self.position.account_balance)
            self.position.spread_pips = data.get("spread_pips", 0.0)
            self.position.zone_bias = data.get("zone_bias", 0.0)
            self.position.nearest_resistance = data.get("nearest_resistance", 0.0)
            self.position.nearest_support = data.get("nearest_support", 0.0)
            self.position.last_update = utcnow()

            # Track max profit and drawdown on the WHOLE trade (floating on
            # remaining lots + realized partials) — continuous across a
            # partial close, unlike the raw floating value which halves
            total_pnl = self.position.profit_usd + self.position.realized_usd
            if total_pnl > self.position.max_profit_usd:
                self.position.max_profit_usd = total_pnl
            if total_pnl < self.position.max_drawdown_usd:
                self.position.max_drawdown_usd = total_pnl

            # Keep a short trajectory for the exit model. Peak/drawdown alone
            # cannot distinguish "sliding away" from "climbing back", which is
            # exactly the judgement its cut-loss rule depends on.
            self.position.pnl_samples.append({
                "minutes": round(
                    (utcnow() - self.position.open_time).total_seconds() / 60, 1
                ),
                "price": self.position.current_price,
                "pnl": round(total_pnl, 2),
            })
            if len(self.position.pnl_samples) > PNL_SAMPLE_CAP:
                del self.position.pnl_samples[:-PNL_SAMPLE_CAP]

            # 1. Check safety guardrails (immediate, no LLM needed)
            guardrail_cmd = self._check_guardrails()
            if guardrail_cmd:
                self.pending_command = guardrail_cmd
                self._persist_active_position(self.position)
                return self._format_command_response(guardrail_cmd)

            # 2. Check if we already have a pending command (from previous LLM call)
            if self.pending_command:
                cmd = self.pending_command
                self.pending_command = None
                self._update_flags_from_command(cmd)
                self._persist_active_position(self.position)
                return self._format_command_response(cmd)

            # 3. Determine if LLM call is needed — prepare snapshot UNDER lock
            now = time.time()
            llm_interval = self.config.get("llm_check_interval_seconds", 30)
            if (now - self.last_llm_check >= llm_interval
                    and self.exit_engine is not None
                    and not self._llm_inflight):
                needs_llm = True
                pos_snapshot = copy.copy(self.position)
                snapshot_key = self._position_key(
                    self.position.to_dict()
                )
                # Copy mutable list separately to avoid sharing reference
                pos_snapshot.ai_decisions = list(self.position.ai_decisions)
                self.last_llm_check = now
                if self._exit_engine_is_networked():
                    # Hand off and answer the EA immediately; the decision will
                    # be queued for one of the next reports (5-15s away).
                    self._llm_inflight = True
                    self._llm_thread = threading.Thread(
                        target=self._run_exit_llm,
                        args=(pos_snapshot, snapshot_key),
                        name="exit-llm",
                        daemon=True,
                    )
                    # Phases 2-3 belong to the inline path only
                    needs_llm = False
                    pos_snapshot = None
                    snapshot_key = ""
                    self._llm_thread.start()
            self._persist_active_position(self.position)

        # === PHASE 2: inline decision for a NON-networked engine ===
        # The rule-based engine is pure arithmetic: it cannot time out, and
        # running it inline keeps the fallback path deterministic (the same
        # report that triggers it carries its command).
        llm_decision = None
        if needs_llm and pos_snapshot:
            try:
                llm_decision = self.exit_engine.decide(pos_snapshot)
                if llm_decision:
                    logger.info(f"AI exit decision: {llm_decision.action} | {llm_decision.reason}")
            except Exception as e:
                logger.error(f"LLM exit decision error: {e}")

        # === PHASE 3: Apply LLM decision (under lock) ===
        with self.lock:
            if (
                self.recovery_state == "closing"
                or self.position is None
                or (
                    snapshot_key
                    and self._position_key(self.position.to_dict())
                    != snapshot_key
                )
            ):
                return self._hold_response()

            if llm_decision:
                # Log all decisions (HOLD and non-HOLD)
                self.position.ai_decisions.append({
                    "time": utcnow().isoformat(),
                    "action": llm_decision.action,
                    "reasoning": llm_decision.reason,
                })

                if llm_decision.action != "HOLD":
                    self._update_flags_from_command(llm_decision)
                    self._persist_active_position(self.position)
                    return self._format_command_response(llm_decision)

            self._persist_active_position(self.position)
            return self._hold_response()

    def resolve_forced(self, data: Dict) -> bool:
        """Forced marker for a report that REBUILDS server state. The EA sends
        it (recovery metadata v2), but an older build omits the field entirely
        — and defaulting to False re-registers a FORCE_DECISION coin flip as a
        genuine trade, defeating every downstream forced filter (track record,
        trade outcomes, calibration, reflections). When the flag is absent but
        the lineage id is known, ask the decision row instead of guessing."""
        if "forced" in data:
            return bool(data.get("forced"))
        decision_id = str(data.get("decision_id") or "")
        if decision_id and self.forced_lookup:
            try:
                resolved = bool(self.forced_lookup(decision_id))
                if resolved:
                    logger.info(
                        f"Reconcile report omitted 'forced'; decision "
                        f"{decision_id} was forced — restored from history"
                    )
                return resolved
            except Exception as e:
                logger.warning(f"Forced-flag lookup failed for {decision_id}: {e}")
        return False

    def _exit_engine_is_networked(self) -> bool:
        """True when an exit decision costs a network round trip (a real LLM
        client), i.e. when it may outlive the EA's HTTP timeout. The
        rule-based engine has no client and is dispatched inline."""
        return getattr(self.exit_engine, "client", None) is not None

    def _run_exit_llm(self, pos_snapshot: OpenPosition, snapshot_key: str) -> None:
        """Worker body: ask the exit model, then queue the answer. Runs off the
        request thread so the EA is never blocked waiting for a model."""
        decision = None
        try:
            decision = self.exit_engine.decide(pos_snapshot)
            if decision:
                logger.info(f"AI exit decision: {decision.action} | {decision.reason}")
        except Exception as e:
            logger.error(f"LLM exit decision error: {e}")
        finally:
            with self.lock:
                self._llm_inflight = False
                try:
                    self._queue_exit_decision(decision, snapshot_key)
                except Exception as e:  # never let a worker die silently
                    logger.error(f"Queueing exit decision failed: {e}")

    def _queue_exit_decision(self, decision: Optional[PositionCommand],
                             snapshot_key: str) -> None:
        """Record the decision on the position and queue a non-HOLD command for
        the next EA report. Must be called under self.lock.

        Queuing instead of answering the in-flight request is the whole point:
        the command now survives a timed-out or dropped HTTP response, exactly
        like the guardrail commands. Flags are deliberately NOT updated here —
        _update_flags_from_command runs when the command is actually DELIVERED
        (step 2 of update_position), so a command that never reaches the EA
        can no longer leave partial_closed/sl_moved_to_be lying about the
        position's real state.
        """
        if decision is None:
            return
        if (self.recovery_state == "closing" or self.position is None
                or (snapshot_key
                    and self._position_key(self.position.to_dict()) != snapshot_key)):
            logger.info(
                f"Exit decision {decision.action} dropped: position changed "
                f"while the model was thinking"
            )
            return

        self.position.ai_decisions.append({
            "time": utcnow().isoformat(),
            "action": decision.action,
            "reasoning": decision.reason,
        })

        if decision.action != "HOLD":
            if self.pending_command is not None:
                # A guardrail (or an earlier decision) is already waiting.
                # Safety commands outrank strategy — never displace them.
                logger.warning(
                    f"Exit decision {decision.action} discarded: "
                    f"{self.pending_command.action} already queued"
                )
            else:
                self.pending_command = decision
                logger.info(f"Exit command queued for next report: {decision.action}")
        self._persist_active_position(self.position)

    def wait_for_exit_worker(self, timeout: float = 10.0) -> bool:
        """Block until the background exit-LLM call finishes. Only tests and
        shutdown need this — in production the queued command is picked up by
        the next EA report. Returns False if the worker is still running."""
        thread = self._llm_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _check_guardrails(self) -> Optional[PositionCommand]:
        """
        Check USD-based safety guardrails. Returns command if action needed.
        These are immediate - no LLM consultation.
        """
        pos = self.position
        if pos is None:
            return None

        # Whole-trade P/L: floating on remaining lots + realized partials.
        # Before any partial close realized_usd is 0, so this is identical
        # to the old floating-only checks.
        total_pnl = pos.profit_usd + pos.realized_usd

        # Max loss per trade (USD)
        max_loss = pos.max_loss_usd or self.config.get("max_loss_usd", 100.0)
        if total_pnl < -max_loss:
            logger.warning(f"GUARDRAIL: Max loss ${max_loss} exceeded. "
                           f"Current P/L: ${total_pnl:.2f}")
            return PositionCommand(
                action="CLOSE",
                reason=f"Safety: max loss ${max_loss} exceeded (P/L: ${total_pnl:.2f})",
            )

        # Max hold time
        max_hold = self.config.get("max_hold_minutes", 30)
        minutes_open = (utcnow() - pos.open_time).total_seconds() / 60
        if minutes_open >= max_hold:
            logger.warning(f"GUARDRAIL: Max hold time {max_hold}min reached. "
                           f"Open for {minutes_open:.1f}min")
            return PositionCommand(
                action="CLOSE",
                reason=f"Safety: max hold time {max_hold}min reached ({minutes_open:.0f}min open)",
            )

        # Emergency spread. Entry is already capped just under this same
        # threshold, so there is almost no headroom between "too wide to enter"
        # and "so wide we liquidate" — and this system's whole premise is that
        # spread ALWAYS spikes at the release, i.e. exactly while the position
        # exists. A single sampled tick used to market-close the trade at the
        # spiked bid/ask, turning a transient quoting artifact into a realized
        # loss. Require CONFIRMATION on a second consecutive report unless the
        # spread is catastrophic (>= 2x), where waiting is the bigger risk.
        emergency_spread = self.config.get("emergency_spread_pips", 15)
        if pos.spread_pips >= emergency_spread:
            self._spread_breaches += 1
            catastrophic = pos.spread_pips >= emergency_spread * 2
            if catastrophic or self._spread_breaches >= SPREAD_BREACHES_TO_CLOSE:
                logger.warning(
                    f"GUARDRAIL: Emergency spread {pos.spread_pips} pips >= "
                    f"{emergency_spread} "
                    f"({'catastrophic' if catastrophic else f'{self._spread_breaches} consecutive reports'})"
                )
                return PositionCommand(
                    action="CLOSE",
                    reason=f"Safety: emergency spread {pos.spread_pips:.1f} pips",
                )
            logger.warning(
                f"Spread {pos.spread_pips:.1f} pips >= {emergency_spread} — "
                f"awaiting confirmation on the next report before closing "
                f"(breach {self._spread_breaches}/{SPREAD_BREACHES_TO_CLOSE})"
            )
        else:
            self._spread_breaches = 0

        # Profit protection: close if the WHOLE trade's profit dropped >X%
        # from its peak. Comparing floating-only against the peak made every
        # 50% partial close look like a ~50% collapse and instantly killed
        # the runner the AI had deliberately left open.
        protection_pct = self.config.get("profit_protection_percent", 50)
        if pos.max_profit_usd > 20.0:  # only activate after meaningful profit
            profit_drop_pct = ((pos.max_profit_usd - total_pnl) / pos.max_profit_usd) * 100
            if profit_drop_pct >= protection_pct:
                current_txt = f"${total_pnl:.2f}"
                if pos.realized_usd:
                    current_txt += f" incl. ${pos.realized_usd:.2f} realized"
                logger.warning(f"GUARDRAIL: Profit protection triggered. "
                               f"Peak: ${pos.max_profit_usd:.2f}, Current: {current_txt} "
                               f"(-{profit_drop_pct:.0f}%)")
                return PositionCommand(
                    action="CLOSE",
                    reason=f"Safety: profit dropped {profit_drop_pct:.0f}% from peak "
                           f"(${pos.max_profit_usd:.2f} → {current_txt})",
                )

        return None

    def _update_flags_from_command(self, cmd: PositionCommand) -> None:
        """
        BUG-2 FIX: Update position flags after processing a command.
        Must be called under self.lock.
        """
        if self.position is None:
            return

        if cmd.action == "MODIFY_SL" and cmd.sl_price > 0:
            # Check if SL was moved close to entry price (break-even)
            pip_size = forex_pip_size(self.position.symbol)
            be_tolerance = pip_size * 2  # 2 pips tolerance
            if (
                abs(cmd.sl_price - self.position.entry_price)
                <= be_tolerance + pip_size * 1e-9
            ):
                self.position.sl_moved_to_be = True
                logger.debug(f"Flag set: sl_moved_to_be = True "
                             f"(SL {cmd.sl_price} ≈ entry {self.position.entry_price})")

        elif cmd.action == "PARTIAL_CLOSE":
            self.position.partial_closed = True
            logger.debug(f"Flag set: partial_closed = True "
                         f"(close_percent={cmd.close_percent})")

    @staticmethod
    def _hold_response() -> Dict:
        """
        BUG-4 FIX: Return consistent JSON format for HOLD (no action needed).
        Always includes 'command' object for EA compatibility.
        """
        return {
            "has_command": False,
            "command": {
                "action": "HOLD",
                "sl_price": 0.0,
                "tp_price": 0.0,
                "close_percent": 0.0,
                "reason": "",
            },
        }

    def _format_command_response(self, cmd: PositionCommand) -> Dict:
        """Format command as response dict for EA."""
        return {
            "has_command": True,
            "command": cmd.to_dict(),
        }

    def get_trade_by_decision(self, decision_id: str) -> Optional[Dict]:
        """Full record — including the ai_decisions management trail that
        /api/position/status deliberately strips — of the most recent closed
        trade bound to this decision_id (F2 lineage). None when no trade
        matched: SKIP decisions, a still-open position, or trades older
        than the recent_trades cap (50; the JSONL keeps them all)."""
        if not decision_id:
            return None
        with self.lock:
            for rec in reversed(self.recent_trades):
                if rec.get("decision_id") == decision_id:
                    return dict(rec)
        return None

    def get_status(self) -> Dict:
        """Get current position manager status (for debugging/monitoring)."""
        with self.lock:
            self._reset_daily_if_needed()
            return {
                "has_position": self.position is not None,
                "position": self.position.to_dict() if self.position else None,
                "recovery_state": self.recovery_state,
                "daily_pnl_usd": round(self.daily_pnl_usd, 2),
                "daily_trades": self.daily_trades,
                "closed_trades_today": len(self.closed_trades),
                # Live limits so the dashboard shows what is actually enforced
                "max_daily_trades": self.config.get("max_daily_trades", 5),
                "max_daily_loss_usd": self.config.get("max_daily_loss_usd", 300.0),
                # Last closed trades (persistent, across UTC-day resets/restarts).
                # Slimmed: the exit-decision trail and reasoning stay in
                # trade_history.jsonl — shipping them here would add hundreds
                # of KB to every dashboard poll for data nothing renders
                "recent_trades": [
                    {k: v for k, v in t.items()
                     if k not in ("ai_decisions", "entry_reasoning")}
                    for t in reversed(self.recent_trades[-10:])
                ],
                "pending_command": self.pending_command.to_dict() if self.pending_command else None,
            }
