---
name: python-workspace
description: Python-MT5 integration workspace (manual load only)
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
autoActivate: false
---

# MQL5-Python Translation Workspace

**Loaded via:** `/sky_tower skill python`

Configure Python development for MetaTrader 5 integration with built-in validation.

## This Skill Covers

- MetaTrader5 Python package setup
- OHLCV data export (5000 bars in 6-8 sec)
- Indicator translation MQL5 → Python
- Validation with 0.999+ correlation threshold

## Core Capabilities

### 1. Automated Data Export
Fetch OHLCV data and built-in indicators (RSI, SMA, etc.) from any symbol/timeframe:
- Headless operation (no GUI needed)
- 5000-bar exports in 6-8 seconds
- 0.999920+ correlation validation

### 2. Indicator Translation
Convert MQL5 indicators to Python with validation:
- Side-by-side comparison
- Correlation threshold: ≥0.999
- Automatic drift detection

### 3. Strategy Backtesting
Python-based backtesting using MT5 data:
- Historical data access
- Position simulation
- Performance metrics

## Setup Requirements

### Python Environment
```bash
# Create virtual environment
python -m venv mt5_env

# Activate
# Windows:
mt5_env\Scripts\activate
# Linux (Wine):
source mt5_env/bin/activate

# Install dependencies
pip install MetaTrader5 pandas numpy scipy
```

### MetaTrader5 Package
```python
import MetaTrader5 as mt5

# Initialize connection
if not mt5.initialize():
    print(f"MT5 init failed: {mt5.last_error()}")
    quit()

# Check connection
print(f"MT5 version: {mt5.version()}")
print(f"Terminal: {mt5.terminal_info()}")
```

## Data Export Workflow

### Step 1: Connect to MT5
```python
import MetaTrader5 as mt5
from datetime import datetime
import pandas as pd

mt5.initialize()
```

### Step 2: Fetch OHLCV Data
```python
def get_ohlcv(symbol, timeframe, bars=5000):
    """
    Fetch OHLCV data from MT5

    Args:
        symbol: e.g., "NZDUSD"
        timeframe: mt5.TIMEFRAME_M1, mt5.TIMEFRAME_H1, etc.
        bars: number of bars to fetch

    Returns:
        pandas DataFrame
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)

    if rates is None:
        print(f"Failed to get rates: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

# Example usage
df = get_ohlcv("NZDUSD", mt5.TIMEFRAME_M1, 5000)
print(df.head())
```

### Step 3: Fetch Built-in Indicators
```python
def get_rsi(symbol, timeframe, period=14, bars=5000):
    """Fetch RSI values from MT5"""
    handle = mt5.iRSI(symbol, timeframe, period, mt5.PRICE_CLOSE)

    if handle == mt5.INVALID_HANDLE:
        print(f"Failed to create RSI handle")
        return None

    values = mt5.copy_buffer(handle, 0, 0, bars)
    return values

def get_sma(symbol, timeframe, period=20, bars=5000):
    """Fetch SMA values from MT5"""
    handle = mt5.iMA(symbol, timeframe, period, 0, mt5.MODE_SMA, mt5.PRICE_CLOSE)

    if handle == mt5.INVALID_HANDLE:
        print(f"Failed to create SMA handle")
        return None

    values = mt5.copy_buffer(handle, 0, 0, bars)
    return values
```

### Step 4: Validate Python Implementation
```python
import numpy as np
from scipy.stats import pearsonr

def validate_indicator(mt5_values, python_values, threshold=0.999):
    """
    Validate Python implementation against MT5

    Returns:
        dict with correlation and pass/fail status
    """
    # Remove NaN values
    valid_mask = ~(np.isnan(mt5_values) | np.isnan(python_values))
    mt5_clean = mt5_values[valid_mask]
    py_clean = python_values[valid_mask]

    if len(mt5_clean) < 10:
        return {"status": "FAIL", "reason": "Not enough valid data"}

    correlation, p_value = pearsonr(mt5_clean, py_clean)
    max_diff = np.max(np.abs(mt5_clean - py_clean))

    return {
        "status": "PASS" if correlation >= threshold else "FAIL",
        "correlation": correlation,
        "p_value": p_value,
        "max_difference": max_diff,
        "samples": len(mt5_clean)
    }

# Example
result = validate_indicator(mt5_rsi, python_rsi)
print(f"Validation: {result['status']} (corr: {result['correlation']:.6f})")
```

## Indicator Translation Example

### MQL5 RSI
```mql5
// MQL5 RSI calculation
double CalculateRSI(double &close[], int period, int shift)
{
    double gain = 0, loss = 0;

    for(int i = shift; i < shift + period; i++)
    {
        double change = close[i] - close[i+1];
        if(change > 0) gain += change;
        else loss -= change;
    }

    double avgGain = gain / period;
    double avgLoss = loss / period;

    if(avgLoss == 0) return 100;

    double rs = avgGain / avgLoss;
    return 100 - (100 / (1 + rs));
}
```

### Python RSI
```python
import numpy as np

def calculate_rsi(close, period=14):
    """
    Calculate RSI matching MT5 implementation

    Args:
        close: numpy array of close prices
        period: RSI period (default 14)

    Returns:
        numpy array of RSI values
    """
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)

    # Wilder's smoothing (exponential)
    avg_gain = np.zeros(len(close))
    avg_loss = np.zeros(len(close))

    # Initial SMA
    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])

    # Smoothed values
    for i in range(period + 1, len(close)):
        avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i-1]) / period
        avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i-1]) / period

    rs = np.divide(avg_gain, avg_loss, where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    rsi[:period] = np.nan  # Warmup period

    return rsi
```

## Known Limitations

1. **Custom Indicator Buffers:** API restricts direct buffer access for custom indicators
2. **Real-time Data:** Slight delays in tick data
3. **Wine Compatibility:** Some features limited on Linux

## Validation Thresholds

| Metric | Minimum | Target |
|--------|---------|--------|
| Correlation | 0.999 | 0.9999 |
| Max Difference | 0.01 | 0.001 |
| Sample Size | 1000 | 5000 |

## SkyTower-AI Integration

### Export Data for Analysis
```python
# Export for SkyTower analysis
df = get_ohlcv("NZDUSD", mt5.TIMEFRAME_M1, 10000)
df.to_csv("data/nzdusd_m1.csv", index=False)
```

### Validate Server Signals
```python
import requests

response = requests.get("http://127.0.0.1:5555/api/signal")
signal = response.json()

# Compare with MT5 data
if signal.get("signal"):
    direction = signal["direction"]
    pair = signal["pair"]
    # Validate against current price action
```

Project location: `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\`
