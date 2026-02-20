"""
MT5 Data Exporter - Python-MT5 Integration Module
Based on python-workspace skill patterns

Provides:
- OHLCV data export from MT5
- Built-in indicator fetching (RSI, SMA, etc.)
- Indicator validation against Python implementations
- Signal validation with MT5 price data
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from loguru import logger
from scipy.stats import pearsonr


class MT5DataExporter:
    """
    Export data from MetaTrader 5 for analysis and validation.

    Usage:
        exporter = MT5DataExporter()
        if exporter.connect():
            df = exporter.get_ohlcv("NZDUSD", mt5.TIMEFRAME_M1, 5000)
            exporter.disconnect()
    """

    def __init__(self, path: Optional[str] = None):
        """
        Initialize MT5 Data Exporter

        Args:
            path: Optional path to MT5 terminal (auto-detect if None)
        """
        self.path = path
        self.connected = False
        self.terminal_info = None

    def connect(self) -> bool:
        """
        Connect to MetaTrader 5 terminal

        Returns:
            True if connected successfully
        """
        try:
            if self.path:
                if not mt5.initialize(path=self.path):
                    logger.error(f"MT5 init failed with path: {mt5.last_error()}")
                    return False
            else:
                if not mt5.initialize():
                    logger.error(f"MT5 init failed: {mt5.last_error()}")
                    return False

            self.terminal_info = mt5.terminal_info()
            self.connected = True
            logger.info(f"Connected to MT5: {mt5.version()}")
            return True

        except Exception as e:
            logger.error(f"MT5 connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from MT5"""
        if self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info("Disconnected from MT5")

    def get_ohlcv(self, symbol: str, timeframe: int, bars: int = 5000) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV data from MT5

        Args:
            symbol: e.g., "NZDUSD"
            timeframe: mt5.TIMEFRAME_M1, mt5.TIMEFRAME_H1, etc.
            bars: number of bars to fetch (default 5000)

        Returns:
            pandas DataFrame with columns: time, open, high, low, close, tick_volume, spread, real_volume
        """
        if not self.connected:
            logger.error("Not connected to MT5")
            return None

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)

        if rates is None:
            logger.error(f"Failed to get rates for {symbol}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        logger.info(f"Fetched {len(df)} bars for {symbol}")
        return df

    def get_ohlcv_range(self, symbol: str, timeframe: int,
                        start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV data for specific date range

        Args:
            symbol: e.g., "NZDUSD"
            timeframe: MT5 timeframe constant
            start: start datetime
            end: end datetime

        Returns:
            pandas DataFrame
        """
        if not self.connected:
            logger.error("Not connected to MT5")
            return None

        rates = mt5.copy_rates_range(symbol, timeframe, start, end)

        if rates is None:
            logger.error(f"Failed to get rates for {symbol}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        return df

    def get_current_price(self, symbol: str) -> Optional[Dict]:
        """
        Get current bid/ask prices

        Args:
            symbol: e.g., "NZDUSD"

        Returns:
            Dict with bid, ask, spread, time
        """
        if not self.connected:
            return None

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None

        return {
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": (tick.ask - tick.bid) / mt5.symbol_info(symbol).point,
            "time": datetime.fromtimestamp(tick.time)
        }

    def get_rsi(self, symbol: str, timeframe: int, period: int = 14,
                bars: int = 5000) -> Optional[np.ndarray]:
        """
        Fetch RSI values from MT5 built-in indicator

        Args:
            symbol: e.g., "NZDUSD"
            timeframe: MT5 timeframe constant
            period: RSI period (default 14)
            bars: number of values to fetch

        Returns:
            numpy array of RSI values
        """
        if not self.connected:
            return None

        handle = mt5.iRSI(symbol, timeframe, period, mt5.PRICE_CLOSE)

        if handle == mt5.INVALID_HANDLE:
            logger.error(f"Failed to create RSI handle for {symbol}")
            return None

        values = mt5.copy_buffer(handle, 0, 0, bars)
        return np.array(values) if values is not None else None

    def get_sma(self, symbol: str, timeframe: int, period: int = 20,
                bars: int = 5000) -> Optional[np.ndarray]:
        """
        Fetch SMA values from MT5 built-in indicator

        Args:
            symbol: e.g., "NZDUSD"
            timeframe: MT5 timeframe constant
            period: SMA period (default 20)
            bars: number of values to fetch

        Returns:
            numpy array of SMA values
        """
        if not self.connected:
            return None

        handle = mt5.iMA(symbol, timeframe, period, 0, mt5.MODE_SMA, mt5.PRICE_CLOSE)

        if handle == mt5.INVALID_HANDLE:
            logger.error(f"Failed to create SMA handle for {symbol}")
            return None

        values = mt5.copy_buffer(handle, 0, 0, bars)
        return np.array(values) if values is not None else None

    def get_atr(self, symbol: str, timeframe: int, period: int = 14,
                bars: int = 5000) -> Optional[np.ndarray]:
        """
        Fetch ATR values from MT5 built-in indicator

        Args:
            symbol: e.g., "NZDUSD"
            timeframe: MT5 timeframe constant
            period: ATR period (default 14)
            bars: number of values to fetch

        Returns:
            numpy array of ATR values
        """
        if not self.connected:
            return None

        handle = mt5.iATR(symbol, timeframe, period)

        if handle == mt5.INVALID_HANDLE:
            logger.error(f"Failed to create ATR handle for {symbol}")
            return None

        values = mt5.copy_buffer(handle, 0, 0, bars)
        return np.array(values) if values is not None else None


class IndicatorValidator:
    """
    Validate Python indicator implementations against MT5 values

    Usage:
        validator = IndicatorValidator()
        result = validator.validate(mt5_values, python_values)
        print(f"Correlation: {result['correlation']}")
    """

    def __init__(self, correlation_threshold: float = 0.999):
        """
        Initialize validator

        Args:
            correlation_threshold: minimum correlation for PASS (default 0.999)
        """
        self.threshold = correlation_threshold

    def validate(self, mt5_values: np.ndarray, python_values: np.ndarray) -> Dict:
        """
        Validate Python implementation against MT5

        Args:
            mt5_values: numpy array from MT5
            python_values: numpy array from Python implementation

        Returns:
            dict with status, correlation, p_value, max_difference, samples
        """
        # Remove NaN values
        valid_mask = ~(np.isnan(mt5_values) | np.isnan(python_values))
        mt5_clean = mt5_values[valid_mask]
        py_clean = python_values[valid_mask]

        if len(mt5_clean) < 10:
            return {
                "status": "FAIL",
                "reason": "Not enough valid data",
                "samples": len(mt5_clean)
            }

        correlation, p_value = pearsonr(mt5_clean, py_clean)
        max_diff = np.max(np.abs(mt5_clean - py_clean))
        mean_diff = np.mean(np.abs(mt5_clean - py_clean))

        return {
            "status": "PASS" if correlation >= self.threshold else "FAIL",
            "correlation": correlation,
            "p_value": p_value,
            "max_difference": max_diff,
            "mean_difference": mean_diff,
            "samples": len(mt5_clean),
            "threshold": self.threshold
        }

    def validate_rsi(self, close: np.ndarray, period: int, mt5_rsi: np.ndarray) -> Dict:
        """
        Validate Python RSI against MT5 RSI

        Args:
            close: close prices array
            period: RSI period
            mt5_rsi: RSI values from MT5

        Returns:
            validation result dict
        """
        python_rsi = self._calculate_rsi(close, period)
        return self.validate(mt5_rsi, python_rsi)

    def _calculate_rsi(self, close: np.ndarray, period: int = 14) -> np.ndarray:
        """
        Calculate RSI matching MT5 implementation (Wilder's smoothing)
        """
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)

        avg_gain = np.zeros(len(close))
        avg_loss = np.zeros(len(close))

        # Initial SMA
        avg_gain[period] = np.mean(gain[:period])
        avg_loss[period] = np.mean(loss[:period])

        # Wilder's smoothing
        for i in range(period + 1, len(close)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i-1]) / period

        rs = np.divide(avg_gain, avg_loss, where=avg_loss != 0)
        rsi = 100 - (100 / (1 + rs))
        rsi[:period] = np.nan

        return rsi


def export_for_analysis(symbol: str, timeframe: int, bars: int = 10000,
                        output_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Convenience function to export data for SkyTower analysis

    Args:
        symbol: e.g., "NZDUSD"
        timeframe: MT5 timeframe constant (use mt5.TIMEFRAME_M1 etc.)
        bars: number of bars
        output_path: optional CSV output path

    Returns:
        DataFrame with OHLCV data
    """
    exporter = MT5DataExporter()

    if not exporter.connect():
        logger.error("Could not connect to MT5")
        return None

    try:
        df = exporter.get_ohlcv(symbol, timeframe, bars)

        if df is not None and output_path:
            df.to_csv(output_path, index=False)
            logger.info(f"Exported {len(df)} bars to {output_path}")

        return df

    finally:
        exporter.disconnect()


# Example usage
if __name__ == "__main__":
    # Test connection and export
    exporter = MT5DataExporter()

    if exporter.connect():
        print(f"Terminal: {exporter.terminal_info}")

        # Get OHLCV data
        df = exporter.get_ohlcv("NZDUSD", mt5.TIMEFRAME_M1, 1000)
        if df is not None:
            print(f"OHLCV data shape: {df.shape}")
            print(df.tail())

        # Get current price
        price = exporter.get_current_price("NZDUSD")
        if price:
            print(f"Current NZDUSD: Bid={price['bid']}, Ask={price['ask']}, Spread={price['spread']:.1f}")

        # Get RSI
        rsi = exporter.get_rsi("NZDUSD", mt5.TIMEFRAME_M1, 14, 100)
        if rsi is not None:
            print(f"RSI values: {rsi[-5:]}")

        exporter.disconnect()
    else:
        print("Could not connect to MT5")
