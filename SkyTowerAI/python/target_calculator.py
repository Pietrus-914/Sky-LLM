"""
SkyTower-AI Target Calculator
Calculates optimal take profit and stop loss levels based on zone analysis.

Integrates with zone_analyzer.py to provide actionable trade targets.
"""

from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from enum import Enum
from loguru import logger

from zone_analyzer import ZoneAnalysisResult, Zone, ZoneType, ZoneStrength


class ExitStrategy(Enum):
    """Exit strategy modes"""
    ZONE_BASED = "zone_based"           # Exit at detected zones
    TIME_BASED = "time_based"           # Exit after time limit (legacy)
    HYBRID = "hybrid"                   # Zone-based with time fallback
    PARTIAL_TP = "partial_tp"           # Partial exits at multiple levels


@dataclass
class TradeTargets:
    """Calculated trade targets"""
    direction: str                      # "BUY" or "SELL"
    entry_price: float

    # Primary targets
    tp1: Optional[float] = None         # First take profit (partial)
    tp2: Optional[float] = None         # Second take profit (full)
    sl: Optional[float] = None          # Stop loss

    # Risk/Reward calculations
    tp1_pips: float = 0
    tp2_pips: float = 0
    sl_pips: float = 0
    risk_reward_tp1: float = 0
    risk_reward_tp2: float = 0

    # Partial exit percentages
    tp1_close_percent: int = 50         # Close 50% at TP1
    tp2_close_percent: int = 100        # Close remaining at TP2

    # Zone information
    tp1_zone_type: Optional[str] = None
    tp2_zone_type: Optional[str] = None
    sl_zone_type: Optional[str] = None

    # Break-even trigger
    move_sl_to_be_at_tp1: bool = True   # Move SL to entry after TP1 hit

    # Confidence in targets
    confidence: float = 0.5             # 0-1 based on zone quality

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "direction": self.direction,
            "entry_price": round(self.entry_price, 5),
            "tp1": round(self.tp1, 5) if self.tp1 else None,
            "tp2": round(self.tp2, 5) if self.tp2 else None,
            "sl": round(self.sl, 5) if self.sl else None,
            "tp1_pips": round(self.tp1_pips, 1),
            "tp2_pips": round(self.tp2_pips, 1),
            "sl_pips": round(self.sl_pips, 1),
            "risk_reward_tp1": round(self.risk_reward_tp1, 2),
            "risk_reward_tp2": round(self.risk_reward_tp2, 2),
            "tp1_close_percent": self.tp1_close_percent,
            "tp2_close_percent": self.tp2_close_percent,
            "tp1_zone_type": self.tp1_zone_type,
            "tp2_zone_type": self.tp2_zone_type,
            "sl_zone_type": self.sl_zone_type,
            "move_sl_to_be_at_tp1": self.move_sl_to_be_at_tp1,
            "confidence": round(self.confidence, 2),
        }


class TargetCalculator:
    """
    Calculates trade targets based on zone analysis and configuration.

    Strategy:
    1. TP1 = Nearest liquidity pool in profit direction
    2. TP2 = Next significant zone (FVG or larger liquidity)
    3. SL = Below/above nearest liquidity pool in loss direction
    """

    def __init__(self, config: dict = None):
        """
        Initialize with configuration.

        Args:
            config: Target calculation parameters
        """
        self.config = config or {}

        # Default parameters (can be overridden)
        self.min_rr_ratio = self.config.get("min_rr_ratio", 1.5)
        self.max_sl_pips = self.config.get("max_sl_pips", 50)
        self.min_tp_pips = self.config.get("min_tp_pips", 10)
        self.default_sl_pips = self.config.get("default_sl_pips", 30)
        self.default_tp_pips = self.config.get("default_tp_pips", 40)
        self.tp1_close_percent = self.config.get("tp1_close_percent", 50)
        self.tp2_close_percent = self.config.get("tp2_close_percent", 100)

        # Zone preference weights
        self.zone_weights = {
            ZoneType.LIQUIDITY_HIGH: 1.0,
            ZoneType.LIQUIDITY_LOW: 1.0,
            ZoneType.FVG_BULLISH: 0.8,
            ZoneType.FVG_BEARISH: 0.8,
            ZoneType.ORDER_BLOCK_BULLISH: 0.7,
            ZoneType.ORDER_BLOCK_BEARISH: 0.7,
        }

    def _to_pips(self, price_diff: float, symbol: str = "") -> float:
        """Convert price difference to pips"""
        if "JPY" in symbol.upper():
            return abs(price_diff) * 100
        return abs(price_diff) * 10000

    def _from_pips(self, pips: float, symbol: str = "") -> float:
        """Convert pips to price difference"""
        if "JPY" in symbol.upper():
            return pips / 100
        return pips / 10000

    def _get_best_target_zone(
        self,
        zones: List[Zone],
        current_price: float,
        direction: str,
        min_distance_pips: float = 5,
        max_distance_pips: float = 100,
        symbol: str = ""
    ) -> Optional[Zone]:
        """
        Get the best zone for targeting based on strength and distance.

        Args:
            zones: List of zones to consider
            current_price: Current market price
            direction: "BUY" or "SELL"
            min_distance_pips: Minimum distance in pips
            max_distance_pips: Maximum distance in pips
            symbol: Symbol for pip calculation

        Returns:
            Best zone or None
        """
        min_dist = self._from_pips(min_distance_pips, symbol)
        max_dist = self._from_pips(max_distance_pips, symbol)

        valid_zones = []
        for zone in zones:
            distance = abs(zone.midpoint - current_price)

            # Check distance constraints
            if distance < min_dist or distance > max_dist:
                continue

            # For BUY, target should be above; for SELL, below
            if direction == "BUY" and zone.midpoint <= current_price:
                continue
            if direction == "SELL" and zone.midpoint >= current_price:
                continue

            # Calculate score (closer + stronger = better)
            base_weight = self.zone_weights.get(zone.zone_type, 0.5)
            strength_bonus = zone.strength.value * 0.2
            distance_penalty = (distance / max_dist) * 0.3

            score = base_weight + strength_bonus - distance_penalty
            valid_zones.append((zone, score))

        if not valid_zones:
            return None

        # Return highest scoring zone
        valid_zones.sort(key=lambda x: x[1], reverse=True)
        return valid_zones[0][0]

    def _get_sl_zone(
        self,
        zones: List[Zone],
        current_price: float,
        direction: str,
        symbol: str = ""
    ) -> Optional[Zone]:
        """
        Get the best zone for stop loss placement.

        SL should be placed beyond the nearest liquidity in the loss direction.

        Args:
            zones: List of zones to consider
            current_price: Current market price
            direction: "BUY" or "SELL"
            symbol: Symbol for pip calculation

        Returns:
            Best SL zone or None
        """
        max_sl_dist = self._from_pips(self.max_sl_pips, symbol)

        valid_zones = []
        for zone in zones:
            # For BUY, SL is below; for SELL, SL is above
            if direction == "BUY" and zone.midpoint >= current_price:
                continue
            if direction == "SELL" and zone.midpoint <= current_price:
                continue

            distance = abs(zone.midpoint - current_price)
            if distance > max_sl_dist:
                continue

            # Prefer stronger zones (more touches = more important)
            score = zone.strength.value + zone.touches * 0.5
            valid_zones.append((zone, score, distance))

        if not valid_zones:
            return None

        # Return nearest strong zone
        valid_zones.sort(key=lambda x: (-x[1], x[2]))  # Highest score, then nearest
        return valid_zones[0][0]

    def calculate(
        self,
        zone_analysis: ZoneAnalysisResult,
        direction: str,
        entry_price: Optional[float] = None,
        strategy: ExitStrategy = ExitStrategy.HYBRID
    ) -> TradeTargets:
        """
        Calculate trade targets based on zone analysis.

        Args:
            zone_analysis: Result from ZoneAnalyzer
            direction: "BUY" or "SELL"
            entry_price: Entry price (uses current price if None)
            strategy: Exit strategy to use

        Returns:
            TradeTargets with calculated levels
        """
        symbol = zone_analysis.symbol
        current_price = entry_price or zone_analysis.current_price

        targets = TradeTargets(
            direction=direction,
            entry_price=current_price,
            tp1_close_percent=self.tp1_close_percent,
            tp2_close_percent=self.tp2_close_percent,
        )

        # Get zones based on direction
        if direction == "BUY":
            tp_zones = (
                zone_analysis.liquidity_above +
                zone_analysis.fvg_above +
                zone_analysis.order_blocks_above
            )
            sl_zones = (
                zone_analysis.liquidity_below +
                zone_analysis.order_blocks_below
            )
        else:  # SELL
            tp_zones = (
                zone_analysis.liquidity_below +
                zone_analysis.fvg_below +
                zone_analysis.order_blocks_below
            )
            sl_zones = (
                zone_analysis.liquidity_above +
                zone_analysis.order_blocks_above
            )

        # Calculate TP1 (nearest target)
        tp1_zone = self._get_best_target_zone(
            tp_zones, current_price, direction,
            min_distance_pips=self.min_tp_pips,
            max_distance_pips=60,
            symbol=symbol
        )

        if tp1_zone:
            targets.tp1 = tp1_zone.midpoint
            targets.tp1_zone_type = tp1_zone.zone_type.value
            targets.tp1_pips = self._to_pips(targets.tp1 - current_price, symbol)
            if direction == "SELL":
                targets.tp1_pips = self._to_pips(current_price - targets.tp1, symbol)
        else:
            # Default TP1 if no zone found
            default_tp = self._from_pips(self.default_tp_pips, symbol)
            if direction == "BUY":
                targets.tp1 = current_price + default_tp
            else:
                targets.tp1 = current_price - default_tp
            targets.tp1_pips = self.default_tp_pips
            targets.tp1_zone_type = "default"

        # Calculate TP2 (further target, past TP1)
        tp2_zone = self._get_best_target_zone(
            tp_zones, current_price, direction,
            min_distance_pips=targets.tp1_pips + 10,
            max_distance_pips=100,
            symbol=symbol
        )

        if tp2_zone:
            targets.tp2 = tp2_zone.midpoint
            targets.tp2_zone_type = tp2_zone.zone_type.value
            targets.tp2_pips = self._to_pips(targets.tp2 - current_price, symbol)
            if direction == "SELL":
                targets.tp2_pips = self._to_pips(current_price - targets.tp2, symbol)
        else:
            # TP2 = TP1 * 1.5 as fallback
            targets.tp2 = targets.tp1
            targets.tp2_pips = targets.tp1_pips
            targets.tp2_zone_type = targets.tp1_zone_type

        # Calculate SL
        sl_zone = self._get_sl_zone(sl_zones, current_price, direction, symbol)

        if sl_zone:
            # Place SL just beyond the zone
            buffer = self._from_pips(3, symbol)  # 3 pip buffer
            if direction == "BUY":
                targets.sl = sl_zone.price_low - buffer
            else:
                targets.sl = sl_zone.price_high + buffer
            targets.sl_zone_type = sl_zone.zone_type.value
            targets.sl_pips = self._to_pips(current_price - targets.sl, symbol)
            if direction == "SELL":
                targets.sl_pips = self._to_pips(targets.sl - current_price, symbol)
        else:
            # Default SL if no zone found
            default_sl = self._from_pips(self.default_sl_pips, symbol)
            if direction == "BUY":
                targets.sl = current_price - default_sl
            else:
                targets.sl = current_price + default_sl
            targets.sl_pips = self.default_sl_pips
            targets.sl_zone_type = "default"

        # Calculate Risk/Reward ratios
        if targets.sl_pips > 0:
            targets.risk_reward_tp1 = targets.tp1_pips / targets.sl_pips
            targets.risk_reward_tp2 = targets.tp2_pips / targets.sl_pips

        # Calculate confidence based on zone quality
        confidence = 0.5  # Base confidence

        if tp1_zone:
            confidence += 0.1 * tp1_zone.strength.value
        if sl_zone:
            confidence += 0.1 * sl_zone.strength.value
        if targets.risk_reward_tp1 >= self.min_rr_ratio:
            confidence += 0.1

        # Cap at 0.95
        targets.confidence = min(0.95, confidence)

        logger.info(
            f"Targets calculated for {symbol} {direction}: "
            f"TP1={targets.tp1:.5f} ({targets.tp1_pips:.1f}p), "
            f"TP2={targets.tp2:.5f} ({targets.tp2_pips:.1f}p), "
            f"SL={targets.sl:.5f} ({targets.sl_pips:.1f}p), "
            f"RR={targets.risk_reward_tp1:.2f}"
        )

        return targets

    def validate_targets(self, targets: TradeTargets) -> Tuple[bool, str]:
        """
        Validate calculated targets against trading rules.

        Args:
            targets: Calculated targets to validate

        Returns:
            Tuple of (is_valid, reason)
        """
        # Check minimum TP distance
        if targets.tp1_pips < self.min_tp_pips:
            return False, f"TP1 too close: {targets.tp1_pips:.1f} pips < {self.min_tp_pips}"

        # Check maximum SL distance
        if targets.sl_pips > self.max_sl_pips:
            return False, f"SL too far: {targets.sl_pips:.1f} pips > {self.max_sl_pips}"

        # Check minimum risk/reward
        if targets.risk_reward_tp1 < self.min_rr_ratio:
            return False, f"Risk/Reward too low: {targets.risk_reward_tp1:.2f} < {self.min_rr_ratio}"

        # Validate direction consistency
        if targets.direction == "BUY":
            if targets.tp1 <= targets.entry_price:
                return False, "BUY TP1 must be above entry"
            if targets.sl >= targets.entry_price:
                return False, "BUY SL must be below entry"
        else:
            if targets.tp1 >= targets.entry_price:
                return False, "SELL TP1 must be below entry"
            if targets.sl <= targets.entry_price:
                return False, "SELL SL must be above entry"

        return True, "Targets validated"


def calculate_trade_targets(
    zone_analysis: ZoneAnalysisResult,
    direction: str,
    config: dict = None
) -> TradeTargets:
    """
    Convenience function to calculate trade targets.

    Args:
        zone_analysis: Result from zone analyzer
        direction: "BUY" or "SELL"
        config: Calculator configuration

    Returns:
        TradeTargets
    """
    calculator = TargetCalculator(config)
    return calculator.calculate(zone_analysis, direction)


# For testing
if __name__ == "__main__":
    from zone_analyzer import ZoneAnalyzer, PriceBar
    import random

    # Generate mock M1 data with some structure
    mock_bars = []
    base_price = 0.6200

    # Create some obvious levels
    for i in range(100):
        # Add some equal highs around 0.6250
        if i in [20, 35, 50]:
            open_price = 0.6245
            high_price = 0.6252
            low_price = 0.6240
            close_price = 0.6248
        # Add some equal lows around 0.6180
        elif i in [25, 40, 55]:
            open_price = 0.6185
            high_price = 0.6190
            low_price = 0.6178
            close_price = 0.6182
        else:
            open_price = base_price + random.uniform(-0.0015, 0.0015)
            close_price = open_price + random.uniform(-0.0010, 0.0010)
            high_price = max(open_price, close_price) + random.uniform(0, 0.0008)
            low_price = min(open_price, close_price) - random.uniform(0, 0.0008)

        mock_bars.append(PriceBar(
            time=1705600000 + i * 60,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=random.randint(100, 1000),
        ))

        if i not in [20, 35, 50, 25, 40, 55]:
            base_price = close_price

    # Run zone analysis
    analyzer = ZoneAnalyzer()
    zone_result = analyzer.analyze(mock_bars, "NZDUSD")

    print("\n=== ZONE ANALYSIS ===")
    print(f"Liquidity Above: {len(zone_result.liquidity_above)}")
    print(f"Liquidity Below: {len(zone_result.liquidity_below)}")
    print(f"Direction Bias: {zone_result.direction_bias}")

    # Calculate targets for BUY
    calculator = TargetCalculator()
    targets_buy = calculator.calculate(zone_result, "BUY")

    print("\n=== BUY TARGETS ===")
    print(f"Entry: {targets_buy.entry_price:.5f}")
    print(f"TP1: {targets_buy.tp1:.5f} ({targets_buy.tp1_pips:.1f} pips) - {targets_buy.tp1_zone_type}")
    print(f"TP2: {targets_buy.tp2:.5f} ({targets_buy.tp2_pips:.1f} pips) - {targets_buy.tp2_zone_type}")
    print(f"SL: {targets_buy.sl:.5f} ({targets_buy.sl_pips:.1f} pips) - {targets_buy.sl_zone_type}")
    print(f"Risk/Reward: {targets_buy.risk_reward_tp1:.2f}")
    print(f"Confidence: {targets_buy.confidence:.2f}")

    # Validate
    is_valid, reason = calculator.validate_targets(targets_buy)
    print(f"Valid: {is_valid} - {reason}")

    # Calculate targets for SELL
    targets_sell = calculator.calculate(zone_result, "SELL")

    print("\n=== SELL TARGETS ===")
    print(f"Entry: {targets_sell.entry_price:.5f}")
    print(f"TP1: {targets_sell.tp1:.5f} ({targets_sell.tp1_pips:.1f} pips) - {targets_sell.tp1_zone_type}")
    print(f"TP2: {targets_sell.tp2:.5f} ({targets_sell.tp2_pips:.1f} pips) - {targets_sell.tp2_zone_type}")
    print(f"SL: {targets_sell.sl:.5f} ({targets_sell.sl_pips:.1f} pips) - {targets_sell.sl_zone_type}")
    print(f"Risk/Reward: {targets_sell.risk_reward_tp1:.2f}")
