"""
SkyTower-AI Configuration
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    """Float env var with the same empty/garbage fallback as _env_int."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        print(f"WARNING: {name}='{raw}' is not a valid number — using default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back on empty/garbage values instead of
    crashing the whole server at import time (a blank line in .env or
    docker-compose would otherwise put the container in a restart loop)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"WARNING: {name}='{raw}' is not a valid integer — using default {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Bool env var — accepts 1/true/yes/on (case-insensitive)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

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
    # preload_seconds: how early the background updater starts analyzing an event.
    # Default 150s (not 120): the updater scans every decision_check_interval (15s)
    # and the LLM call can take 30-60s — the decision must be ready before the
    # EA's entry at T-15s. Override via env for tuning.
    "preload_seconds": _env_int("SKYTOWER_PRELOAD_SECONDS", 150),
    "decision_check_interval": _env_int("SKYTOWER_CHECK_INTERVAL", 15),
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

# Dodatkowe eventy z .env, bez edycji kodu — np.
# SKYTOWER_EXTRA_EVENTS=PMI,PPI,Trade Balance,Consumer Confidence
# Nazwy dopasowywane są jako podciągi (case-insensitive), więc "PMI" złapie
# też "Flash Manufacturing PMI" i "ISM Services PMI".
_extra_events = os.getenv("SKYTOWER_EXTRA_EVENTS", "")
if _extra_events.strip():
    TIER2_EVENTS = TIER2_EVENTS + [e.strip() for e in _extra_events.split(",") if e.strip()]

# Wszystkie high impact events (dla kompatybilności)
HIGH_IMPACT_EVENTS = TIER1_EVENTS + TIER2_EVENTS

# Minimalny poziom impactu eventów branych do handlu (LOW/MEDIUM/HIGH).
# Zmienialny w locie przez dashboard (POST /api/config/events); wartość
# z .env jest stanem startowym po każdym restarcie serwera.
MIN_IMPACT_LEVEL = os.getenv("SKYTOWER_MIN_IMPACT", "MEDIUM").strip().upper()
if MIN_IMPACT_LEVEL not in ("LOW", "MEDIUM", "HIGH"):
    MIN_IMPACT_LEVEL = "MEDIUM"

# Tryb "wszystkie eventy": gdy True, do handlu bierzemy KAŻDY event od
# MIN_IMPACT_LEVEL w górę (whitelist nazw TIER1/TIER2 jest ignorowana) —
# z wyjątkiem "wypowiedzi" (patrz NON_DATA_EVENT_MARKERS), które nie dają
# twardej liczby do zaskoczenia. Do fazy zbierania danych na demo.
# Gdy False: klasyczny filtr po nazwach (produkcja). Zmienialny w panelu.
TRADE_ALL_EVENTS = _env_bool("SKYTOWER_TRADE_ALL_EVENTS", False)

# "Prognozy / wypowiedzi" — eventy bez publikowanej twardej liczby, których
# nie da się grać na zaskoczeniu actual-vs-forecast. Dopasowanie podciągiem,
# case-insensitive. NIGDY nie handlowane, niezależnie od TRADE_ALL_EVENTS.
NON_DATA_EVENT_MARKERS = [
    "speaks",
    "speech",
    "testifies",
    "testimony",
    "press conference",
    "q&a",
    "projections",
]

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
    "max_tokens": 1500,  # room for the ANALYSIS CHECKLIST reasoning
    "temperature": 0.3,  # Lower = more consistent decisions
}

# =============================================================================
# AI POSITION MANAGEMENT (USD-based guardrails)
# =============================================================================
POSITION_MANAGEMENT_CONFIG = {
    # Safety guardrails in USD. max_loss_usd is the single source of truth:
    # it is sent to the EA in every /api/signal response, where it both caps
    # the lot-sizing risk budget and arms the EA's offline max-loss guardrail.
    "max_loss_usd": _env_float("SKYTOWER_MAX_LOSS_USD", 100.0),        # per trade → forced close
    "max_hold_minutes": 30,            # Max position duration
    "emergency_spread_pips": 15,       # Close if spread spikes above this
    "profit_protection_percent": 50,   # Close if profit drops >50% from peak

    # AI decision intervals
    "llm_check_interval_seconds": 30,  # How often to consult LLM for exit decisions
    "hot_period_seconds": 120,         # Fast polling period after position open
    "hot_poll_interval": 5,            # EA poll interval during hot period (seconds)
    "normal_poll_interval": 15,        # EA poll interval after hot period (seconds)

    # Daily limits (USD and count)
    "max_daily_loss_usd": _env_float("SKYTOWER_MAX_DAILY_LOSS_USD", 300.0),  # stop for the day past this
    "max_daily_trades": _env_int("SKYTOWER_MAX_DAILY_TRADES", 5),            # max trades per day

    # LLM model for exit decisions (can be cheaper/faster than entry model)
    "exit_llm_model": "anthropic/claude-sonnet-4",
}

# =============================================================================
# SERVER CONFIGURATION
# =============================================================================
# Host/port are env-overridable: Docker sets SKYTOWER_HOST=0.0.0.0 so the
# published port is reachable from the Windows host; native runs keep loopback.
SERVER_CONFIG = {
    "host": os.getenv("SKYTOWER_HOST", "127.0.0.1"),
    "port": _env_int("SKYTOWER_PORT", 5555),
    "debug": False,
}

# =============================================================================
# RUNTIME OVERRIDES (dashboard-editable settings that survive restarts)
# =============================================================================
# The dashboard saves operational settings (risk limits, min impact) here so
# nobody has to edit .env by hand. Precedence: default < env < this file.
_OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'logs', 'runtime_overrides.json')

# Persistent closed-trades log (PositionManager) — daily statistics survive
# watchdog restarts. Lives next to the other JSONL stores in logs/.
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'logs', 'trade_history.jsonl')

# Curated event playbooks (hand/Claude-distilled patterns from historical
# charts) injected into the entry prompt. Optional — missing file = no
# EVENT PLAYBOOK section. See SkyTowerAI/research/screens/README.md.
# Lives in knowledge/ (tracked in git), NOT logs/ (gitignored) — the ZIP-based
# 24/7 deploy must ship this file; it is still hot-reloaded on edit (mtime).
EVENT_PLAYBOOKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'knowledge', 'event_playbooks.json')

# Machine-built frequency statistics (tools/build_learned_stats.py aggregates
# knowledge/historical_paths.jsonl.gz + logs/event_paths.jsonl) injected into
# the entry prompt as LEARNED EVENT STATISTICS. Generated file — NEVER edit by
# hand and never mix with the curated playbook above; regenerate offline with
# the tool. Hot-reloaded by mtime like the playbook. Missing file = section
# absent (prompt degrades gracefully).
LEARNED_STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'knowledge', 'learned_stats.json')

# Post-trade reflections (F5): quarantined n=1 journal entries written by
# the exit-tier model after each closed NON-forced trade; injected into the
# entry prompt only under an explicit "anecdotes, not rules" header.
# SKYTOWER_REFLECTIONS=0 disables generation (the section simply dries up).
REFLECTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'logs', 'trade_reflections.jsonl')
REFLECTIONS_ENABLED = _env_bool("SKYTOWER_REFLECTIONS", True)

# Playbook distillation proposals (F5): machine-drafted playbook updates
# awaiting the operator's approve/reject on the dashboard. NEVER
# auto-applied — approval writes into knowledge/event_playbooks.json.
PLAYBOOK_PROPOSALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'logs', 'playbook_proposals.jsonl')

# Monetary-policy regime per currency: "hiking" / "cutting" / "hold".
# SEED ONLY — from here on the RegimeTracker (regime_tracker.py) maintains the
# live value automatically: every recorded rate decision updates it (hike/cut
# are facts from the calendar; an ambiguous hold is adjudicated by the LLM
# using the measured market reaction). State: logs/currency_regimes.json;
# view/override: GET|POST /api/regimes. This map is applied only to
# currencies the state file doesn't know yet (fresh deploy) — editing it
# later does NOT override tracked values; use the panel/API for that.
# Stamped into every event_paths.jsonl record so reaction statistics can be
# split by regime (the same event often reacts OPPOSITE ways in hiking vs
# cutting cycles). Unknown currency -> null tag.
# Seed verified 2026-07-16 (energy-shock tightening wave after Iran conflict):
#   USD hiking = bias call (no hike delivered yet; June SEP dots + ~50% July-29
#       market odds). AUD hiking = weakest call (3 hikes in 2026, 2-meeting
#       pause framed as within-cycle; Aug 11 live). CAD next decision Sep 2.
CURRENCY_REGIMES = {
    "USD": "hiking",   # Fed 3.50-3.75%, cut bias removed, hike expected H2
    "NZD": "hiking",   # RBNZ +25bp -> 2.50% (Jul 8), more signalled
    "CAD": "hold",     # BoC 2.25%, 5th straight hold (Jul 15)
    "AUD": "hiking",   # RBA 4.35% after 3 hikes, pause within cycle
    "GBP": "hold",     # BoE 3.75% (7-2), cutting cycle over, no near move
    "EUR": "hiking",   # ECB +25bp -> 2.25% depo (Jun 11), first in 3 years
    "JPY": "hiking",   # BoJ +25bp -> 1.00% (Jun 16), more tightening guided
    "CHF": "hold",     # SNB 0.00%, prefers FX intervention
}


def save_runtime_overrides(updates: dict):
    """Merge updates into the overrides file (atomic write)."""
    import json
    data = {}
    try:
        if os.path.exists(_OVERRIDES_FILE):
            with open(_OVERRIDES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.update(updates)
    os.makedirs(os.path.dirname(_OVERRIDES_FILE), exist_ok=True)
    tmp = _OVERRIDES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _OVERRIDES_FILE)


def _apply_runtime_overrides():
    import json
    try:
        if not os.path.exists(_OVERRIDES_FILE):
            return
        with open(_OVERRIDES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    global MIN_IMPACT_LEVEL, TRADE_ALL_EVENTS
    if str(data.get('min_impact', '')).upper() in ("LOW", "MEDIUM", "HIGH"):
        MIN_IMPACT_LEVEL = str(data['min_impact']).upper()
    if isinstance(data.get('trade_all_events'), bool):
        TRADE_ALL_EVENTS = data['trade_all_events']
    for key, lo, hi, cast in (("max_daily_trades", 1, 100, int),
                              ("max_daily_loss_usd", 10, 1_000_000, float),
                              ("max_loss_usd", 5, 100_000, float)):
        if key in data:
            try:
                value = cast(data[key])
                if lo <= value <= hi:
                    POSITION_MANAGEMENT_CONFIG[key] = value
            except (TypeError, ValueError):
                pass


_apply_runtime_overrides()

# =============================================================================
# ENSEMBLE (F4): K-call self-consistency at entry
# =============================================================================
# K >= 2 makes the entry engine fire K PARALLEL LLM calls per decision:
# unanimity (all valid votes BUY or all SELL) = trade, any split = SKIP.
# Verbal LLM confidence is systematically overconfident — vote agreement is
# the calibrated gate. COST: K x the entry-model price per analyzed event.
# Default 1 = classic single call. Wall-clock stays ~one call (parallel).
# In FORCE_DECISION demo mode the majority direction wins instead (SKIP is
# not available there) and agreement scales the reported confidence.
ENSEMBLE_K = _env_int("SKYTOWER_ENSEMBLE_K", 1)
if not 1 <= ENSEMBLE_K <= 5:
    print(f"WARNING: SKYTOWER_ENSEMBLE_K={ENSEMBLE_K} out of range 1-5 — using 1")
    ENSEMBLE_K = 1

# =============================================================================
# TEST MODE: FORCE DECISION (never SKIP)
# =============================================================================
# When enabled, the entry decision engine must always pick BUY or SELL —
# SKIP is disabled at every level (LLM prompt, parse fallback, rule fallback).
# Intended ONLY for the demo-account data-collection phase. Set explicitly in
# docker-compose.yml, not in .env, so it stays visible and deliberate.
FORCE_DECISION = _env_bool("SKYTOWER_FORCE_DECISION", False)

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
