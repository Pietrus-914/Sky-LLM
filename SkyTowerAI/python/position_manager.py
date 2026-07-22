"""
SkyTower-AI Position Manager
Manages open positions with AI-driven exit decisions and USD-based safety guardrails.
"""
import copy
import json
import os
import threading
import time
from datetime import datetime
from timeutil import utcnow
from typing import Dict, Optional, List
from dataclasses import dataclass, field, asdict
from loguru import logger

from config import POSITION_MANAGEMENT_CONFIG


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

    def __init__(self, exit_engine=None, history_file: Optional[str] = None):
        self.position: Optional[OpenPosition] = None
        self.pending_command: Optional[PositionCommand] = None
        self.lock = threading.Lock()

        # Daily tracking
        self.daily_pnl_usd: float = 0.0
        self.daily_trades: int = 0
        self.daily_reset_date: str = ""

        # LLM timing
        self.last_llm_check: float = 0.0
        self.exit_engine = exit_engine  # ExitDecisionEngine instance

        # Config
        self.config = POSITION_MANAGEMENT_CONFIG

        # Trade history (for daily tracking)
        self.closed_trades: List[Dict] = []

        # Persistent trade log (survives restarts and UTC-midnight resets).
        # None (default, e.g. in tests) = in-memory only.
        self.history_file = history_file
        self.recent_trades: List[Dict] = []  # newest last, capped
        if self.history_file:
            try:
                os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            except OSError as e:
                logger.error(f"Could not create trade history dir: {e}")
        self._load_history()

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

    def _write_history_line(self, record: Dict) -> None:
        """Append one closed trade to the persistent JSONL log.
        Called OUTSIDE self.lock — a stalled disk write must not block
        can_open_trade()/update_position() (they gate the EA's signal
        polling during the entry window)."""
        if not self.history_file:
            return
        try:
            with open(self.history_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Could not write trade history {self.history_file}: {e}")

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
                           decision_id: str = "", forced: bool = False) -> None:
        """Called when EA reports a new position opened."""
        with self.lock:
            self._reset_daily_if_needed()

            now = utcnow()
            self.position = OpenPosition(
                ticket=data.get("ticket", 0),
                symbol=data.get("symbol", ""),
                direction=data.get("direction", ""),
                entry_price=data.get("entry_price", 0.0),
                current_price=data.get("entry_price", 0.0),
                lots=data.get("lots", 0.0),
                remaining_lots=data.get("lots", 0.0),
                sl=data.get("sl", 0.0),
                tp=data.get("tp", 0.0),
                profit_usd=0.0,
                max_profit_usd=0.0,
                max_drawdown_usd=0.0,
                tick_value=data.get("tick_value", 10.0),
                account_balance=data.get("account_balance", 0.0),
                open_time=now,
                last_update=now,
                event_name=data.get("event_name", ""),
                entry_reasoning=entry_reasoning,
                decision_id=decision_id,
                forced=forced,
            )
            self.daily_trades += 1
            self.pending_command = None
            self.last_llm_check = 0.0

            logger.info(f"Position opened: {self.position.direction} {self.position.symbol} "
                        f"@ {self.position.entry_price} | {self.position.lots} lots | "
                        f"ticket={self.position.ticket}")

    def on_position_closed(self, data: Dict) -> None:
        """Called when EA reports position closed.

        Also handles an ORPHANED close (self.position is None, e.g. the
        server restarted while the EA held the trade): the trade is still
        persisted and counted, otherwise its P/L would vanish from the
        daily-loss budget on the next restart and the daily trade count
        would under-count by one."""
        with self.lock:
            profit = data.get("profit", 0.0)
            self.daily_pnl_usd += profit

            # One schema for both branches: shared/EA-supplied fields with
            # neutral defaults; the tracked branch overlays the position's
            # real values. New fields get added exactly once — a forgotten
            # second literal would silently produce mixed-schema JSONL rows.
            record = {
                "ticket": data.get("ticket", 0),
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
                # EA echo (F2): keeps lineage even for an orphaned close,
                # where the opened-time binding died with the old process
                "decision_id": str(data.get("decision_id") or ""),
                "forced": False,
                "entry_price": 0.0,
                "close_price": data.get("close_price", 0.0),
                "sl": 0.0,
                "tp": 0.0,
                "max_profit_usd": 0.0,
                "max_drawdown_usd": 0.0,
                "realized_usd": 0.0,
                "spread_pips": 0.0,
                "entry_reasoning": "",
                "ai_decisions": [],
            }

            if self.position:
                # Everything here was already in memory and used to be
                # discarded at close — persisted so trade quality (MFE/MAE,
                # SL sizing, exit trail) is analyzable after the fact
                record.update({
                    "ticket": self.position.ticket,
                    "symbol": self.position.symbol,
                    "direction": self.position.direction,
                    "lots": self.position.lots,
                    "event_name": self.position.event_name,
                    "opened_at": self.position.open_time.isoformat(),
                    "decisions_count": len(self.position.ai_decisions),
                    # Tracked binding first (set at open), EA echo as backup
                    "decision_id": (self.position.decision_id
                                    or str(data.get("decision_id") or "")),
                    "forced": self.position.forced,
                    "entry_price": self.position.entry_price,
                    "sl": self.position.sl,
                    "tp": self.position.tp,
                    "max_profit_usd": self.position.max_profit_usd,
                    "max_drawdown_usd": self.position.max_drawdown_usd,
                    "realized_usd": self.position.realized_usd,
                    "spread_pips": self.position.spread_pips,
                    "entry_reasoning": self.position.entry_reasoning,
                    "ai_decisions": list(self.position.ai_decisions),
                })
                logger.info(f"Position closed: {self.position.symbol} | "
                            f"P/L: ${profit:.2f} | Reason: {data.get('reason', 'unknown')} | "
                            f"Daily P/L: ${self.daily_pnl_usd:.2f}")
            else:
                # Orphaned close — position state was lost (server restart).
                # Its open was never counted post-restart, so count it here.
                self.daily_trades += 1
                logger.warning(f"Orphaned position close (no tracked position) | "
                               f"ticket={record['ticket']} | P/L: ${profit:.2f} | "
                               f"Daily P/L: ${self.daily_pnl_usd:.2f}")

            self.closed_trades.append(record)
            self.recent_trades.append(record)
            self.recent_trades = self.recent_trades[-50:]

            self.position = None
            self.pending_command = None

        # Disk write outside the lock — see _write_history_line
        self._write_history_line(record)
        # Returned so the server can hand the closed trade to post-trade
        # consumers (F5 reflections) without re-reading the JSONL
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
        needs_llm = False

        # === PHASE 1: Update data + check guardrails (under lock, fast) ===
        with self.lock:
            if self.position is None:
                return self._hold_response()

            # BUG-5 FIX: Validate ticket matches current position
            reported_ticket = data.get("ticket")
            if reported_ticket is not None and reported_ticket != self.position.ticket:
                logger.warning(f"Ticket mismatch: expected {self.position.ticket}, "
                               f"got {reported_ticket}. Ignoring update.")
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
                closed_fraction = 1.0 - (reported_lots / prev_lots)
                realized_delta = self.position.profit_usd * closed_fraction
                self.position.realized_usd += realized_delta
                self.position.partial_closed = True
                logger.info(f"Partial close detected: {prev_lots:.2f} -> "
                            f"{reported_lots:.2f} lots | ~${realized_delta:.2f} "
                            f"credited as realized (total realized: "
                            f"${self.position.realized_usd:.2f})")

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

            # 1. Check safety guardrails (immediate, no LLM needed)
            guardrail_cmd = self._check_guardrails()
            if guardrail_cmd:
                self.pending_command = guardrail_cmd
                return self._format_command_response(guardrail_cmd)

            # 2. Check if we already have a pending command (from previous LLM call)
            if self.pending_command:
                cmd = self.pending_command
                self.pending_command = None
                self._update_flags_from_command(cmd)
                return self._format_command_response(cmd)

            # 3. Determine if LLM call is needed — prepare snapshot UNDER lock
            now = time.time()
            llm_interval = self.config.get("llm_check_interval_seconds", 30)
            if now - self.last_llm_check >= llm_interval and self.exit_engine is not None:
                needs_llm = True
                pos_snapshot = copy.copy(self.position)
                # Copy mutable list separately to avoid sharing reference
                pos_snapshot.ai_decisions = list(self.position.ai_decisions)
                self.last_llm_check = now

        # === PHASE 2: LLM call OUTSIDE lock (can take 10-60s) ===
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
            if self.position is None:
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
                    return self._format_command_response(llm_decision)

            return self._hold_response()

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
        max_loss = self.config.get("max_loss_usd", 100.0)
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

        # Emergency spread
        emergency_spread = self.config.get("emergency_spread_pips", 15)
        if pos.spread_pips >= emergency_spread:
            logger.warning(f"GUARDRAIL: Emergency spread {pos.spread_pips} pips >= {emergency_spread}")
            return PositionCommand(
                action="CLOSE",
                reason=f"Safety: emergency spread {pos.spread_pips:.1f} pips",
            )

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
            pip_size = 0.01 if "JPY" in self.position.symbol else 0.0001
            be_tolerance = pip_size * 20  # 2 pips tolerance
            if abs(cmd.sl_price - self.position.entry_price) <= be_tolerance:
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
