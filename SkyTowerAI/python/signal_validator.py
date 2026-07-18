"""
Signal Validator - Validate trading signals against MT5 data
Based on python-workspace skill patterns

Provides:
- Real-time signal validation with current MT5 prices
- Spread monitoring and alerts
- Post-trade analysis
- Signal history tracking
"""

import MetaTrader5 as mt5
from datetime import datetime, timedelta
from typing import Optional, Dict
from dataclasses import dataclass
from loguru import logger
import json
import sqlite3
import os

from config import TRADING_CONFIG, TYPICAL_NEWS_SPREADS, SPREAD_LOT_REDUCTION


@dataclass
class SignalValidation:
    """Signal validation result"""
    timestamp: datetime
    symbol: str
    direction: str
    confidence: float
    current_bid: float
    current_ask: float
    current_spread: float
    spread_status: str  # "OK", "HIGH", "EXTREME"
    recommended_lot_multiplier: float
    validation_passed: bool
    reason: str


@dataclass
class TradeResult:
    """Post-trade analysis result"""
    ticket: int
    symbol: str
    direction: str
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float]
    exit_time: Optional[datetime]
    profit: float
    pips: float
    spread_at_entry: float
    event_name: str
    confidence: float


class SignalValidator:
    """
    Validate trading signals against live MT5 data

    Usage:
        validator = SignalValidator()
        result = validator.validate_signal("NZDUSD", "BUY", 0.65)
        if result.validation_passed:
            # Execute trade
        else:
            print(f"Signal rejected: {result.reason}")
    """

    def __init__(self, db_path: str = "logs/signal_history.db"):
        """
        Initialize Signal Validator

        Args:
            db_path: Path to SQLite database for history
        """
        self.db_path = db_path
        self.connected = False
        self._ensure_db()

    def _ensure_db(self):
        """Create database tables if not exist"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                direction TEXT,
                confidence REAL,
                current_bid REAL,
                current_ask REAL,
                current_spread REAL,
                spread_status TEXT,
                recommended_lot_multiplier REAL,
                validation_passed INTEGER,
                reason TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket INTEGER,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                entry_time TEXT,
                exit_price REAL,
                exit_time TEXT,
                profit REAL,
                pips REAL,
                spread_at_entry REAL,
                event_name TEXT,
                confidence REAL
            )
        """)

        conn.commit()
        conn.close()

    def connect(self) -> bool:
        """Connect to MT5"""
        if not mt5.initialize():
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return False

        self.connected = True
        return True

    def disconnect(self):
        """Disconnect from MT5"""
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def validate_signal(self, symbol: str, direction: str,
                        confidence: float) -> SignalValidation:
        """
        Validate a trading signal against current market conditions

        Args:
            symbol: Trading symbol (e.g., "NZDUSD")
            direction: "BUY" or "SELL"
            confidence: Signal confidence 0.0-1.0

        Returns:
            SignalValidation result
        """
        timestamp = datetime.now()

        # Ensure connected
        if not self.connected:
            if not self.connect():
                return SignalValidation(
                    timestamp=timestamp,
                    symbol=symbol,
                    direction=direction,
                    confidence=confidence,
                    current_bid=0,
                    current_ask=0,
                    current_spread=0,
                    spread_status="UNKNOWN",
                    recommended_lot_multiplier=0,
                    validation_passed=False,
                    reason="Could not connect to MT5"
                )

        # Get current tick
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            # Try with common suffixes
            for suffix in [".a", ".r", "_SB", ".pro", ""]:
                test_symbol = symbol.replace("/", "") + suffix
                tick = mt5.symbol_info_tick(test_symbol)
                if tick is not None:
                    symbol = test_symbol
                    break

        if tick is None:
            return SignalValidation(
                timestamp=timestamp,
                symbol=symbol,
                direction=direction,
                confidence=confidence,
                current_bid=0,
                current_ask=0,
                current_spread=0,
                spread_status="UNKNOWN",
                recommended_lot_multiplier=0,
                validation_passed=False,
                reason=f"Cannot get tick data for {symbol}"
            )

        # Calculate spread
        symbol_info = mt5.symbol_info(symbol)
        point = symbol_info.point
        spread_points = tick.ask - tick.bid
        spread_pips = spread_points / point / 10  # Convert to pips

        # Determine spread status and lot multiplier
        spread_status, lot_multiplier = self._evaluate_spread(symbol.replace("/", ""), spread_pips)

        # Validate
        max_spread = TRADING_CONFIG.get("max_spread_pips", 10)
        min_confidence = 0.5

        validation_passed = True
        reason = "OK"

        if spread_pips > max_spread:
            validation_passed = False
            reason = f"Spread {spread_pips:.1f} pips exceeds max {max_spread}"
        elif confidence < min_confidence:
            validation_passed = False
            reason = f"Confidence {confidence:.2f} below minimum {min_confidence}"
        elif lot_multiplier == 0:
            validation_passed = False
            reason = f"Spread {spread_pips:.1f} pips is extreme - no trade"

        result = SignalValidation(
            timestamp=timestamp,
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            current_bid=tick.bid,
            current_ask=tick.ask,
            current_spread=spread_pips,
            spread_status=spread_status,
            recommended_lot_multiplier=lot_multiplier,
            validation_passed=validation_passed,
            reason=reason
        )

        # Store in database
        self._store_validation(result)

        return result

    def _evaluate_spread(self, symbol: str, spread_pips: float) -> tuple:
        """
        Evaluate spread and return status and lot multiplier

        Returns:
            (spread_status, lot_multiplier)
        """
        # Get typical spreads for symbol if available
        typical = TYPICAL_NEWS_SPREADS.get(symbol, {
            "normal": 2.0,
            "news": 8.0,
            "max_acceptable": 15.0
        })

        # Determine spread level
        if spread_pips <= SPREAD_LOT_REDUCTION["low"]["threshold"]:
            return "OK", SPREAD_LOT_REDUCTION["low"]["multiplier"]
        elif spread_pips <= SPREAD_LOT_REDUCTION["medium"]["threshold"]:
            return "MEDIUM", SPREAD_LOT_REDUCTION["medium"]["multiplier"]
        elif spread_pips <= SPREAD_LOT_REDUCTION["high"]["threshold"]:
            return "HIGH", SPREAD_LOT_REDUCTION["high"]["multiplier"]
        else:
            return "EXTREME", SPREAD_LOT_REDUCTION["extreme"]["multiplier"]

    def _store_validation(self, result: SignalValidation):
        """Store validation result in database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO signal_validations
                (timestamp, symbol, direction, confidence, current_bid, current_ask,
                 current_spread, spread_status, recommended_lot_multiplier,
                 validation_passed, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.timestamp.isoformat(),
                result.symbol,
                result.direction,
                result.confidence,
                result.current_bid,
                result.current_ask,
                result.current_spread,
                result.spread_status,
                result.recommended_lot_multiplier,
                1 if result.validation_passed else 0,
                result.reason
            ))

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to store validation: {e}")

    def get_statistics(self, days: int = 30) -> Dict:
        """
        Get trading statistics

        Args:
            days: Number of days to analyze

        Returns:
            Dict with statistics
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        # Validation stats
        cursor.execute("""
            SELECT COUNT(*), SUM(validation_passed)
            FROM signal_validations
            WHERE timestamp > ?
        """, (cutoff,))
        total_validations, passed_validations = cursor.fetchone()

        # Trade stats
        cursor.execute("""
            SELECT COUNT(*), SUM(profit), AVG(profit), AVG(pips),
                   SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)
            FROM trade_results
            WHERE entry_time > ? AND exit_price IS NOT NULL
        """, (cutoff,))
        total_trades, total_profit, avg_profit, avg_pips, winning_trades = cursor.fetchone()

        # Spread stats
        cursor.execute("""
            SELECT AVG(current_spread), MAX(current_spread), MIN(current_spread)
            FROM signal_validations
            WHERE timestamp > ?
        """, (cutoff,))
        avg_spread, max_spread, min_spread = cursor.fetchone()

        conn.close()

        return {
            "period_days": days,
            "validations": {
                "total": total_validations or 0,
                "passed": passed_validations or 0,
                "pass_rate": (passed_validations / total_validations * 100) if total_validations else 0
            },
            "trades": {
                "total": total_trades or 0,
                "winning": winning_trades or 0,
                "win_rate": (winning_trades / total_trades * 100) if total_trades else 0,
                "total_profit": total_profit or 0,
                "avg_profit": avg_profit or 0,
                "avg_pips": avg_pips or 0
            },
            "spreads": {
                "average": avg_spread or 0,
                "max": max_spread or 0,
                "min": min_spread or 0
            }
        }


# Convenience function for server integration
def validate_signal_for_server(symbol: str, direction: str, confidence: float) -> Dict:
    """
    Validate signal and return dict suitable for API response

    Args:
        symbol: Trading symbol
        direction: BUY or SELL
        confidence: Signal confidence

    Returns:
        Dict with validation results
    """
    validator = SignalValidator()
    result = validator.validate_signal(symbol, direction, confidence)
    validator.disconnect()

    return {
        "validation_passed": result.validation_passed,
        "current_spread": result.current_spread,
        "spread_status": result.spread_status,
        "recommended_lot_multiplier": result.recommended_lot_multiplier,
        "reason": result.reason,
        "timestamp": result.timestamp.isoformat()
    }


if __name__ == "__main__":
    # Test validation
    validator = SignalValidator()

    print("Testing signal validation...")
    result = validator.validate_signal("NZDUSD", "BUY", 0.65)

    print(f"\nValidation Result:")
    print(f"  Symbol: {result.symbol}")
    print(f"  Direction: {result.direction}")
    print(f"  Confidence: {result.confidence}")
    print(f"  Current Spread: {result.current_spread:.1f} pips")
    print(f"  Spread Status: {result.spread_status}")
    print(f"  Lot Multiplier: {result.recommended_lot_multiplier}")
    print(f"  Passed: {result.validation_passed}")
    print(f"  Reason: {result.reason}")

    # Get statistics
    stats = validator.get_statistics()
    print(f"\nStatistics (30 days):")
    print(json.dumps(stats, indent=2))

    validator.disconnect()
