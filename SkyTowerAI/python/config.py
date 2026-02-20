"""
SkyTower-AI Configuration
"""
import os
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# API KEYS (set in .env file or environment)
# =============================================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")  # Free tier available

# =============================================================================
# MT5 CONFIGURATION - Purple Trading
# =============================================================================
MT5_CONFIG = {
    "login": int(os.getenv("MT5_LOGIN", 0)),
    "password": os.getenv("MT5_PASSWORD", ""),
    "server": os.getenv("MT5_SERVER", "PurpleTrading-MT5"),
    "path": os.getenv("MT5_PATH", r"C:\Program Files\Purple Trading MT5\terminal64.exe")
}

# =============================================================================
# TRADING PARAMETERS (based on SkyTower-FX strategy)
# =============================================================================
TRADING_CONFIG = {
    # Risk Management
    "max_risk_percent": 10.0,       # Max 10% of capital per trade (as per strategy)
    "default_lot_percent": 80.0,    # 80% of max lot (safety margin for margin call)
    "leverage": 500,                 # 1:500 leverage

    # Entry/Exit Timing
    "decision_seconds_before": 120, # Decision must be ready 2 min before event
    "entry_seconds_before": 15,     # Enter 15 seconds before news
    "exit_minutes_after": 10,       # Default exit after 10 minutes (10 candles M1)
    "max_hold_minutes": 30,         # Maximum hold time

    # Spread Protection
    "max_spread_pips": 10,          # Don't enter if spread > 10 pips
    "spread_check_seconds": 5,      # Check spread 5 seconds before entry
}

# =============================================================================
# CURRENCY PAIRS (ranked by strategy effectiveness)
# =============================================================================
CURRENCY_PAIRS = {
    # Primary pairs (best reactions per strategy)
    "NZD": ["NZD/USD", "AUD/NZD", "NZD/CAD"],
    "CAD": ["USD/CAD", "NZD/CAD", "AUD/CAD"],
    "AUD": ["AUD/USD", "AUD/CAD", "AUD/NZD"],
    "USD": ["USD/CAD", "AUD/USD", "NZD/USD"],
    "GBP": ["GBP/USD", "EUR/GBP", "GBP/CAD"],

    # Advanced (more volatile, higher risk)
    "SEK": ["USD/SEK", "NOK/SEK"],
    "NOK": ["USD/NOK", "SEK/NOK"],
}

# Preferred pair per currency (lowest spread typically)
DEFAULT_PAIRS = {
    "NZD": "NZD/USD",
    "CAD": "USD/CAD",
    "AUD": "AUD/USD",
    "USD": "USD/CAD",
    "GBP": "GBP/USD",
}

# =============================================================================
# HIGH IMPACT EVENTS (per SkyTower-FX strategy)
# =============================================================================
# Tier 1 - Najlepsze reakcje, zawsze tradować
TIER1_EVENTS = [
    "Interest Rate Decision",
    "Cash Rate",
    "Official Cash Rate",
    "Non-Farm Payrolls",
    "NFP",
    "Nonfarm Payrolls",
    "CPI",
    "Consumer Price Index",
]

# Tier 2 - Dobre reakcje, tradować z ostrożnością
TIER2_EVENTS = [
    "Employment Change",
    "Unemployment Rate",
    "GDP",
    "Gross Domestic Product",
    "Advance GDP",
    "Retail Sales",
    "New Home Sales",
    "Existing Home Sales",
]

# Wszystkie high impact events (dla kompatybilności)
HIGH_IMPACT_EVENTS = TIER1_EVENTS + TIER2_EVENTS

# Events suitable for LITE version (hedging both directions)
LITE_EVENTS = {
    "AUD": ["GDP", "CPI"],
    "CAD": ["Interest Rate Decision", "Cash Rate"],
    "NZD": ["CPI", "Official Cash Rate", "Employment Change"],
    "SEK": ["Interest Rate Decision"],
    "USD": ["Interest Rate Decision", "Non-Farm Payrolls"],
}

# =============================================================================
# SPREAD & LIQUIDITY CONFIGURATION
# =============================================================================
# Typowe spready na newsach (w pipsach) - używane do oceny ryzyka
TYPICAL_NEWS_SPREADS = {
    "EURUSD": {"normal": 0.8, "news": 3.0, "max_acceptable": 6},
    "USDJPY": {"normal": 1.0, "news": 4.0, "max_acceptable": 8},
    "GBPUSD": {"normal": 1.2, "news": 5.0, "max_acceptable": 10},
    "USDCAD": {"normal": 1.5, "news": 4.0, "max_acceptable": 8},
    "AUDUSD": {"normal": 1.2, "news": 5.0, "max_acceptable": 10},
    "NZDUSD": {"normal": 1.8, "news": 8.0, "max_acceptable": 12},
    "AUDNZD": {"normal": 3.0, "news": 12.0, "max_acceptable": 18},
    "AUDCAD": {"normal": 2.5, "news": 8.0, "max_acceptable": 14},
    "GBPCAD": {"normal": 3.0, "news": 10.0, "max_acceptable": 16},
    "NZDCAD": {"normal": 3.5, "news": 12.0, "max_acceptable": 18},
}

# Współczynniki redukcji lota przy wysokim spreadzie
SPREAD_LOT_REDUCTION = {
    "low": {"threshold": 3, "multiplier": 1.0},      # Normalny spread
    "medium": {"threshold": 6, "multiplier": 0.8},   # -20% lota
    "high": {"threshold": 10, "multiplier": 0.6},    # -40% lota
    "extreme": {"threshold": 15, "multiplier": 0.0}, # Nie wchodź
}

# Godziny niskiej płynności (UTC) - unikaj tradowania
LOW_LIQUIDITY_HOURS = {
    "asian_gap": (21, 23),      # Przerwa między sesjami
    "weekend_gap": (21, 22),    # Niedziela wieczór
    "holiday_all_day": True,    # Święta
}

# Pary do unikania przy niskiej płynności
AVOID_LOW_LIQUIDITY_PAIRS = ["AUDNZD", "NZDCAD", "GBPNZD", "EURNZD"]

# =============================================================================
# ZONE ANALYSIS CONFIGURATION (Smart Money Concepts)
# =============================================================================
ZONE_CONFIG = {
    # Detection parameters
    "equal_level_tolerance_pips": 3.0,  # Tolerance for equal highs/lows
    "min_touches_for_liquidity": 2,      # Min touches to form liquidity pool
    "lookback_bars": 50,                 # Bars to analyze for zones
    "min_fvg_size_pips": 2.0,           # Minimum FVG size to consider
    "min_impulse_multiplier": 2.0,       # Multiplier for order block detection

    # Target calculation
    "min_rr_ratio": 1.5,                 # Minimum risk/reward ratio
    "max_sl_pips": 50,                   # Maximum stop loss distance
    "min_tp_pips": 10,                   # Minimum take profit distance
    "default_sl_pips": 30,               # Default SL if no zone found
    "default_tp_pips": 40,               # Default TP if no zone found
    "tp1_close_percent": 50,             # Close 50% at TP1
    "tp2_close_percent": 100,            # Close remaining at TP2

    # Zone bias scoring (added to decision engine)
    "zone_bias_weight": 2,               # Points added when zones confirm direction
    "enable_zone_bias": True,            # Enable zone-based direction bias
}

# =============================================================================
# SMART EXIT CONFIGURATION
# =============================================================================
EXIT_CONFIG = {
    # Exit strategy mode
    "exit_strategy": "hybrid",           # "zone_based", "time_based", "hybrid", "partial_tp"

    # Zone-based exit settings
    "use_zone_targets": True,            # Use zone-detected TP levels
    "partial_close_at_tp1": True,        # Partial close at first target
    "move_sl_to_be_at_tp1": True,        # Move SL to break-even after TP1

    # Time-based fallback (used in hybrid mode)
    "max_hold_minutes": 30,              # Maximum position hold time
    "fallback_exit_minutes": 15,         # Exit if no target hit within this time

    # Safety settings
    "trail_after_tp1": True,             # Enable trailing stop after TP1
    "trail_distance_pips": 10,           # Trailing stop distance
}

# =============================================================================
# DATA SOURCES (FREE)
# =============================================================================
DATA_SOURCES = {
    # Economic Calendar
    "investing_calendar": "https://www.investing.com/economic-calendar/",
    "myfxbook_calendar": "https://www.myfxbook.com/forex-economic-calendar",
    "finnhub_calendar": "https://finnhub.io/api/v1/calendar/economic",

    # COT Data (CFTC)
    "cftc_cot": "https://publicreporting.cftc.gov/",

    # Sentiment Data
    "myfxbook_sentiment": "https://www.myfxbook.com/community/outlook",
    "fxssi_sentiment": "https://fxssi.com/tools/current-ratio",
    "dukascopy_sentiment": "https://www.dukascopy.com/swiss/english/marketwatch/sentiment/",
}

# =============================================================================
# LLM CONFIGURATION
# =============================================================================
LLM_CONFIG = {
    "provider": "openrouter",  # "openrouter", "anthropic", "openai", "rule-based"
    "model": "anthropic/claude-opus-4",  # Best for critical financial decisions
    # Alternative models (via OpenRouter):
    # "anthropic/claude-sonnet-4" - faster, cheaper, still very good
    # "deepseek/deepseek-r1-0528" - excellent reasoning, very cheap
    # "openai/gpt-4.1" - fast, large context
    "max_tokens": 1000,
    "temperature": 0.3,  # Lower = more consistent decisions
}

# =============================================================================
# AI POSITION MANAGEMENT (USD-based guardrails)
# =============================================================================
POSITION_MANAGEMENT_CONFIG = {
    # Safety guardrails in USD (server-side + EA-side backup)
    "max_loss_usd": 100.0,             # Max loss $ per trade → forced close
    "max_hold_minutes": 30,            # Max position duration
    "emergency_spread_pips": 15,       # Close if spread spikes above this
    "profit_protection_percent": 50,   # Close if profit drops >50% from peak

    # AI decision intervals
    "llm_check_interval_seconds": 30,  # How often to consult LLM for exit decisions
    "hot_period_seconds": 120,         # Fast polling period after position open
    "hot_poll_interval": 5,            # EA poll interval during hot period (seconds)
    "normal_poll_interval": 15,        # EA poll interval after hot period (seconds)

    # Daily limits (USD and count)
    "max_daily_loss_usd": 300.0,       # Stop trading if daily losses exceed this
    "max_daily_trades": 5,             # Max trades per day

    # LLM model for exit decisions (can be cheaper/faster than entry model)
    "exit_llm_model": "anthropic/claude-sonnet-4",
}

# =============================================================================
# SERVER CONFIGURATION
# =============================================================================
SERVER_CONFIG = {
    "host": "127.0.0.1",
    "port": 5555,
    "debug": False,
}

# =============================================================================
# LOGGING
# =============================================================================
LOG_CONFIG = {
    "level": "INFO",
    "format": "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    "rotation": "1 day",
    "retention": "30 days",
    "path": "logs/skytower.log",
}
