"""
SkyTower-AI Zone Analyzer
Detects liquidity pools, FVG (Fair Value Gaps), and order blocks for smart exit targets.

Based on ICT/Smart Money Concepts:
- Liquidity pools = clusters of stop losses (equal highs/lows)
- FVG = price inefficiency gaps that price tends to fill
- Order blocks = last candle before impulsive move
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from enum import Enum
from loguru import logger


class ZoneType(Enum):
    """Types of market structure zones"""
    LIQUIDITY_HIGH = "liquidity_high"      # Equal highs (sell-side liquidity)
    LIQUIDITY_LOW = "liquidity_low"        # Equal lows (buy-side liquidity)
    FVG_BULLISH = "fvg_bullish"            # Bullish fair value gap
    FVG_BEARISH = "fvg_bearish"            # Bearish fair value gap
    ORDER_BLOCK_BULLISH = "ob_bullish"     # Bullish order block
    ORDER_BLOCK_BEARISH = "ob_bearish"     # Bearish order block


class ZoneStrength(Enum):
    """Zone strength based on touches/confirmations"""
    WEAK = 1
    MODERATE = 2
    STRONG = 3


@dataclass
class PriceBar:
    """OHLC price bar data"""
    time: int           # Unix timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass
class Zone:
    """Market structure zone"""
    zone_type: ZoneType
    price_high: float           # Upper boundary
    price_low: float            # Lower boundary
    creation_time: int          # When zone was created
    strength: ZoneStrength = ZoneStrength.MODERATE
    touches: int = 1            # Number of times price touched this zone
    is_filled: bool = False     # Whether FVG has been filled
    invalidated: bool = False   # Whether zone is no longer valid

    @property
    def midpoint(self) -> float:
        """Middle of the zone"""
        return (self.price_high + self.price_low) / 2

    @property
    def size_pips(self) -> float:
        """Zone size in pips (assumes 4/5 digit broker)"""
        return (self.price_high - self.price_low) * 10000

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "type": self.zone_type.value,
            "price_high": round(self.price_high, 5),
            "price_low": round(self.price_low, 5),
            "midpoint": round(self.midpoint, 5),
            "size_pips": round(self.size_pips, 1),
            "strength": self.strength.value,
            "touches": self.touches,
            "is_filled": self.is_filled,
            "creation_time": self.creation_time,
        }


@dataclass
class ZoneAnalysisResult:
    """Complete zone analysis for a symbol"""
    symbol: str
    current_price: float
    liquidity_above: List[Zone] = field(default_factory=list)
    liquidity_below: List[Zone] = field(default_factory=list)
    fvg_above: List[Zone] = field(default_factory=list)
    fvg_below: List[Zone] = field(default_factory=list)
    order_blocks_above: List[Zone] = field(default_factory=list)
    order_blocks_below: List[Zone] = field(default_factory=list)
    suggested_target_long: Optional[float] = None
    suggested_target_short: Optional[float] = None
    suggested_sl_long: Optional[float] = None
    suggested_sl_short: Optional[float] = None
    direction_bias: Optional[str] = None  # "BUY", "SELL", or None
    bias_strength: int = 0  # 0-3 points for zone confluence

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "symbol": self.symbol,
            "current_price": round(self.current_price, 5),
            "liquidity_above": [z.to_dict() for z in self.liquidity_above[:3]],
            "liquidity_below": [z.to_dict() for z in self.liquidity_below[:3]],
            "fvg_above": [z.to_dict() for z in self.fvg_above[:3]],
            "fvg_below": [z.to_dict() for z in self.fvg_below[:3]],
            "order_blocks_above": [z.to_dict() for z in self.order_blocks_above[:2]],
            "order_blocks_below": [z.to_dict() for z in self.order_blocks_below[:2]],
            "targets": {
                "long": {
                    "tp1": round(self.suggested_target_long, 5) if self.suggested_target_long else None,
                    "sl": round(self.suggested_sl_long, 5) if self.suggested_sl_long else None,
                },
                "short": {
                    "tp1": round(self.suggested_target_short, 5) if self.suggested_target_short else None,
                    "sl": round(self.suggested_sl_short, 5) if self.suggested_sl_short else None,
                },
            },
            "direction_bias": self.direction_bias,
            "bias_strength": self.bias_strength,
        }


class ZoneAnalyzer:
    """
    Analyzes price data to detect market structure zones.

    Parameters from config control detection sensitivity.
    """

    def __init__(self, config: dict = None):
        """
        Initialize with configuration.

        Args:
            config: Zone detection parameters (uses defaults if None)
        """
        self.config = config or {}

        # Detection parameters (can be overridden by config)
        self.equal_level_tolerance_pips = self.config.get("equal_level_tolerance_pips", 3.0)
        self.min_touches_for_liquidity = self.config.get("min_touches_for_liquidity", 2)
        self.lookback_bars = self.config.get("lookback_bars", 50)
        self.min_fvg_size_pips = self.config.get("min_fvg_size_pips", 2.0)
        self.min_impulse_multiplier = self.config.get("min_impulse_multiplier", 2.0)

    def _to_pips(self, price_diff: float, symbol: str = "") -> float:
        """Convert price difference to pips"""
        # Assume 4/5 digit broker for major pairs
        if "JPY" in symbol.upper():
            return price_diff * 100
        return price_diff * 10000

    def _from_pips(self, pips: float, symbol: str = "") -> float:
        """Convert pips to price difference"""
        if "JPY" in symbol.upper():
            return pips / 100
        return pips / 10000

    def find_liquidity_pools(self, bars: List[PriceBar], symbol: str = "") -> Tuple[List[Zone], List[Zone]]:
        """
        Find equal highs (sell-side liquidity) and equal lows (buy-side liquidity).

        Equal levels indicate clusters of stop losses that institutions target.

        Args:
            bars: List of PriceBar objects (oldest first)
            symbol: Symbol name for pip calculation

        Returns:
            Tuple of (liquidity_highs, liquidity_lows)
        """
        if len(bars) < 5:
            return [], []

        tolerance = self._from_pips(self.equal_level_tolerance_pips, symbol)
        liquidity_highs = []
        liquidity_lows = []

        # Find swing highs and lows
        swing_highs = []
        swing_lows = []

        for i in range(2, len(bars) - 2):
            # Swing high: higher than 2 bars on each side
            if (bars[i].high > bars[i-1].high and bars[i].high > bars[i-2].high and
                bars[i].high > bars[i+1].high and bars[i].high > bars[i+2].high):
                swing_highs.append((i, bars[i].high, bars[i].time))

            # Swing low: lower than 2 bars on each side
            if (bars[i].low < bars[i-1].low and bars[i].low < bars[i-2].low and
                bars[i].low < bars[i+1].low and bars[i].low < bars[i+2].low):
                swing_lows.append((i, bars[i].low, bars[i].time))

        # Group equal highs
        used_highs = set()
        for i, (idx1, price1, time1) in enumerate(swing_highs):
            if i in used_highs:
                continue

            equal_count = 1
            max_price = price1
            min_price = price1

            for j, (idx2, price2, time2) in enumerate(swing_highs[i+1:], i+1):
                if j in used_highs:
                    continue
                if abs(price1 - price2) <= tolerance:
                    equal_count += 1
                    max_price = max(max_price, price2)
                    min_price = min(min_price, price2)
                    used_highs.add(j)

            if equal_count >= self.min_touches_for_liquidity:
                strength = ZoneStrength.WEAK if equal_count == 2 else (
                    ZoneStrength.MODERATE if equal_count == 3 else ZoneStrength.STRONG
                )
                zone = Zone(
                    zone_type=ZoneType.LIQUIDITY_HIGH,
                    price_high=max_price + tolerance,
                    price_low=min_price - tolerance,
                    creation_time=time1,
                    strength=strength,
                    touches=equal_count,
                )
                liquidity_highs.append(zone)

        # Group equal lows
        used_lows = set()
        for i, (idx1, price1, time1) in enumerate(swing_lows):
            if i in used_lows:
                continue

            equal_count = 1
            max_price = price1
            min_price = price1

            for j, (idx2, price2, time2) in enumerate(swing_lows[i+1:], i+1):
                if j in used_lows:
                    continue
                if abs(price1 - price2) <= tolerance:
                    equal_count += 1
                    max_price = max(max_price, price2)
                    min_price = min(min_price, price2)
                    used_lows.add(j)

            if equal_count >= self.min_touches_for_liquidity:
                strength = ZoneStrength.WEAK if equal_count == 2 else (
                    ZoneStrength.MODERATE if equal_count == 3 else ZoneStrength.STRONG
                )
                zone = Zone(
                    zone_type=ZoneType.LIQUIDITY_LOW,
                    price_high=max_price + tolerance,
                    price_low=min_price - tolerance,
                    creation_time=time1,
                    strength=strength,
                    touches=equal_count,
                )
                liquidity_lows.append(zone)

        # Sort by distance from current price (last bar)
        current_price = bars[-1].close
        liquidity_highs.sort(key=lambda z: abs(z.midpoint - current_price))
        liquidity_lows.sort(key=lambda z: abs(z.midpoint - current_price))

        return liquidity_highs, liquidity_lows

    def find_fvg(self, bars: List[PriceBar], symbol: str = "") -> Tuple[List[Zone], List[Zone]]:
        """
        Find Fair Value Gaps (FVG) - price inefficiency gaps.

        Bullish FVG: gap between bar[i-1].low and bar[i+1].high
        Bearish FVG: gap between bar[i-1].high and bar[i+1].low

        Args:
            bars: List of PriceBar objects (oldest first)
            symbol: Symbol name for pip calculation

        Returns:
            Tuple of (bullish_fvg, bearish_fvg)
        """
        if len(bars) < 3:
            return [], []

        min_gap = self._from_pips(self.min_fvg_size_pips, symbol)
        bullish_fvg = []
        bearish_fvg = []
        current_price = bars[-1].close

        for i in range(1, len(bars) - 1):
            prev_bar = bars[i - 1]
            curr_bar = bars[i]
            next_bar = bars[i + 1]

            # Bullish FVG: gap up (prev low > next high)
            if prev_bar.low > next_bar.high:
                gap_size = prev_bar.low - next_bar.high
                if gap_size >= min_gap:
                    zone = Zone(
                        zone_type=ZoneType.FVG_BULLISH,
                        price_high=prev_bar.low,
                        price_low=next_bar.high,
                        creation_time=curr_bar.time,
                        strength=ZoneStrength.MODERATE if gap_size > min_gap * 2 else ZoneStrength.WEAK,
                    )
                    # Check if already filled
                    for check_bar in bars[i+2:]:
                        if check_bar.low <= zone.midpoint:
                            zone.is_filled = True
                            break

                    if not zone.is_filled:
                        bullish_fvg.append(zone)

            # Bearish FVG: gap down (prev high < next low)
            if prev_bar.high < next_bar.low:
                gap_size = next_bar.low - prev_bar.high
                if gap_size >= min_gap:
                    zone = Zone(
                        zone_type=ZoneType.FVG_BEARISH,
                        price_high=next_bar.low,
                        price_low=prev_bar.high,
                        creation_time=curr_bar.time,
                        strength=ZoneStrength.MODERATE if gap_size > min_gap * 2 else ZoneStrength.WEAK,
                    )
                    # Check if already filled
                    for check_bar in bars[i+2:]:
                        if check_bar.high >= zone.midpoint:
                            zone.is_filled = True
                            break

                    if not zone.is_filled:
                        bearish_fvg.append(zone)

        # Sort by distance from current price
        bullish_fvg.sort(key=lambda z: abs(z.midpoint - current_price))
        bearish_fvg.sort(key=lambda z: abs(z.midpoint - current_price))

        return bullish_fvg, bearish_fvg

    def find_order_blocks(self, bars: List[PriceBar], symbol: str = "") -> Tuple[List[Zone], List[Zone]]:
        """
        Find order blocks - last opposite candle before impulsive move.

        Bullish OB: last bearish candle before strong bullish move
        Bearish OB: last bullish candle before strong bearish move

        Args:
            bars: List of PriceBar objects (oldest first)
            symbol: Symbol name for pip calculation

        Returns:
            Tuple of (bullish_ob, bearish_ob)
        """
        if len(bars) < 5:
            return [], []

        bullish_ob = []
        bearish_ob = []
        current_price = bars[-1].close

        # Calculate average range for impulse detection
        avg_range = sum(b.range_size for b in bars) / len(bars)
        impulse_threshold = avg_range * self.min_impulse_multiplier

        for i in range(2, len(bars) - 2):
            curr_bar = bars[i]
            next_bar = bars[i + 1]

            # Bullish order block: bearish candle followed by strong bullish move
            if curr_bar.is_bearish and next_bar.is_bullish:
                if next_bar.body_size >= impulse_threshold:
                    zone = Zone(
                        zone_type=ZoneType.ORDER_BLOCK_BULLISH,
                        price_high=curr_bar.open,
                        price_low=curr_bar.low,
                        creation_time=curr_bar.time,
                        strength=ZoneStrength.STRONG if next_bar.body_size >= impulse_threshold * 1.5 else ZoneStrength.MODERATE,
                    )
                    # Check if revisited and held
                    for check_bar in bars[i+2:]:
                        if check_bar.low < zone.price_low:
                            zone.invalidated = True
                            break
                        elif zone.price_low <= check_bar.low <= zone.price_high:
                            zone.touches += 1

                    if not zone.invalidated:
                        bullish_ob.append(zone)

            # Bearish order block: bullish candle followed by strong bearish move
            if curr_bar.is_bullish and next_bar.is_bearish:
                if next_bar.body_size >= impulse_threshold:
                    zone = Zone(
                        zone_type=ZoneType.ORDER_BLOCK_BEARISH,
                        price_high=curr_bar.high,
                        price_low=curr_bar.open,
                        creation_time=curr_bar.time,
                        strength=ZoneStrength.STRONG if next_bar.body_size >= impulse_threshold * 1.5 else ZoneStrength.MODERATE,
                    )
                    # Check if revisited and held
                    for check_bar in bars[i+2:]:
                        if check_bar.high > zone.price_high:
                            zone.invalidated = True
                            break
                        elif zone.price_low <= check_bar.high <= zone.price_high:
                            zone.touches += 1

                    if not zone.invalidated:
                        bearish_ob.append(zone)

        # Sort by distance from current price
        bullish_ob.sort(key=lambda z: abs(z.midpoint - current_price))
        bearish_ob.sort(key=lambda z: abs(z.midpoint - current_price))

        return bullish_ob, bearish_ob

    def analyze(self, bars: List[PriceBar], symbol: str = "") -> ZoneAnalysisResult:
        """
        Perform complete zone analysis.

        Args:
            bars: List of PriceBar objects (oldest first)
            symbol: Symbol name

        Returns:
            ZoneAnalysisResult with all detected zones and suggested targets
        """
        if len(bars) < 10:
            logger.warning(f"Not enough bars for analysis: {len(bars)}")
            return ZoneAnalysisResult(
                symbol=symbol,
                current_price=bars[-1].close if bars else 0,
            )

        current_price = bars[-1].close

        # Detect all zone types
        liq_highs, liq_lows = self.find_liquidity_pools(bars, symbol)
        fvg_bullish, fvg_bearish = self.find_fvg(bars, symbol)
        ob_bullish, ob_bearish = self.find_order_blocks(bars, symbol)

        # Separate zones above and below current price
        liquidity_above = [z for z in liq_highs if z.midpoint > current_price]
        liquidity_below = [z for z in liq_lows if z.midpoint < current_price]
        fvg_above = [z for z in fvg_bearish if z.midpoint > current_price]  # Bearish FVG above = target for longs
        fvg_below = [z for z in fvg_bullish if z.midpoint < current_price]  # Bullish FVG below = target for shorts
        ob_above = [z for z in ob_bearish if z.midpoint > current_price]
        ob_below = [z for z in ob_bullish if z.midpoint < current_price]

        # Calculate suggested targets
        target_long = None
        target_short = None
        sl_long = None
        sl_short = None

        # For LONG: target is nearest liquidity above, SL below nearest liquidity
        if liquidity_above:
            target_long = liquidity_above[0].midpoint
        elif fvg_above:
            target_long = fvg_above[0].midpoint

        if liquidity_below:
            sl_long = liquidity_below[0].price_low

        # For SHORT: target is nearest liquidity below, SL above nearest liquidity
        if liquidity_below:
            target_short = liquidity_below[0].midpoint
        elif fvg_below:
            target_short = fvg_below[0].midpoint

        if liquidity_above:
            sl_short = liquidity_above[0].price_high

        # Calculate direction bias from zones
        bias = None
        bias_strength = 0

        # Larger liquidity pool above = likely to sweep up = BUY bias
        liq_above_size = sum(z.size_pips * z.strength.value for z in liquidity_above[:2])
        liq_below_size = sum(z.size_pips * z.strength.value for z in liquidity_below[:2])

        if liq_above_size > liq_below_size * 1.5:
            bias = "BUY"
            bias_strength += 1
        elif liq_below_size > liq_above_size * 1.5:
            bias = "SELL"
            bias_strength += 1

        # Unfilled FVG adds to bias
        if fvg_above and not fvg_below:
            if bias == "BUY":
                bias_strength += 1
            elif bias is None:
                bias = "BUY"
                bias_strength += 1
        elif fvg_below and not fvg_above:
            if bias == "SELL":
                bias_strength += 1
            elif bias is None:
                bias = "SELL"
                bias_strength += 1

        # Strong order block adds to bias
        strong_ob_above = [z for z in ob_above if z.strength == ZoneStrength.STRONG]
        strong_ob_below = [z for z in ob_below if z.strength == ZoneStrength.STRONG]

        if strong_ob_below and not strong_ob_above:
            if bias == "BUY":
                bias_strength += 1
        elif strong_ob_above and not strong_ob_below:
            if bias == "SELL":
                bias_strength += 1

        logger.info(f"Zone analysis for {symbol}: bias={bias} (strength={bias_strength}), "
                   f"liq_above={len(liquidity_above)}, liq_below={len(liquidity_below)}, "
                   f"fvg_above={len(fvg_above)}, fvg_below={len(fvg_below)}")

        return ZoneAnalysisResult(
            symbol=symbol,
            current_price=current_price,
            liquidity_above=liquidity_above,
            liquidity_below=liquidity_below,
            fvg_above=fvg_above,
            fvg_below=fvg_below,
            order_blocks_above=ob_above,
            order_blocks_below=ob_below,
            suggested_target_long=target_long,
            suggested_target_short=target_short,
            suggested_sl_long=sl_long,
            suggested_sl_short=sl_short,
            direction_bias=bias,
            bias_strength=bias_strength,
        )


def analyze_from_ohlc_data(
    ohlc_data: List[Dict],
    symbol: str = "",
    config: dict = None
) -> ZoneAnalysisResult:
    """
    Convenience function to analyze OHLC data from various sources.

    Args:
        ohlc_data: List of dicts with keys: time, open, high, low, close, volume (optional)
        symbol: Symbol name
        config: Zone analyzer configuration

    Returns:
        ZoneAnalysisResult
    """
    bars = []
    for candle in ohlc_data:
        bar = PriceBar(
            time=candle.get("time", 0),
            open=float(candle.get("open", 0)),
            high=float(candle.get("high", 0)),
            low=float(candle.get("low", 0)),
            close=float(candle.get("close", 0)),
            volume=int(candle.get("volume", 0)),
        )
        bars.append(bar)

    analyzer = ZoneAnalyzer(config)
    return analyzer.analyze(bars, symbol)


# For testing
if __name__ == "__main__":
    # Example usage with mock data
    import random

    # Generate mock M1 data
    mock_bars = []
    base_price = 0.6200  # NZD/USD example

    for i in range(100):
        open_price = base_price + random.uniform(-0.0020, 0.0020)
        close_price = open_price + random.uniform(-0.0015, 0.0015)
        high_price = max(open_price, close_price) + random.uniform(0, 0.0010)
        low_price = min(open_price, close_price) - random.uniform(0, 0.0010)

        mock_bars.append(PriceBar(
            time=1705600000 + i * 60,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=random.randint(100, 1000),
        ))

        base_price = close_price

    # Run analysis
    analyzer = ZoneAnalyzer()
    result = analyzer.analyze(mock_bars, "NZDUSD")

    print("\n=== ZONE ANALYSIS RESULT ===")
    print(f"Symbol: {result.symbol}")
    print(f"Current Price: {result.current_price:.5f}")
    print(f"\nLiquidity Above: {len(result.liquidity_above)}")
    for z in result.liquidity_above[:3]:
        print(f"  - {z.midpoint:.5f} ({z.size_pips:.1f} pips, {z.touches} touches)")
    print(f"\nLiquidity Below: {len(result.liquidity_below)}")
    for z in result.liquidity_below[:3]:
        print(f"  - {z.midpoint:.5f} ({z.size_pips:.1f} pips, {z.touches} touches)")
    print(f"\nFVG Above: {len(result.fvg_above)}")
    print(f"FVG Below: {len(result.fvg_below)}")
    print(f"\nDirection Bias: {result.direction_bias} (strength: {result.bias_strength})")
    print(f"Target Long: {result.suggested_target_long}")
    print(f"Target Short: {result.suggested_target_short}")
