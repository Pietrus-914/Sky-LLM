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


def _env_ranged_float(name: str, default: float, lo: float, hi: float) -> float:
    """_env_float plus a range guard. An out-of-range value can silently
    DISABLE a guardrail (grace of 99999s means profit protection never arms),
    so the .env rung gets the same bounds the panel endpoint enforces."""
    value = _env_float(name, default)
    if not lo <= value <= hi:
        print(f"WARNING: {name}={value} out of range {lo}-{hi} — using default {default}")
        return default
    return value


def _env_ranged_int(name: str, default: int, lo: int, hi: int) -> int:
    """_env_int plus the same range guard as _env_ranged_float."""
    value = _env_int(name, default)
    if not lo <= value <= hi:
        print(f"WARNING: {name}={value} out of range {lo}-{hi} — using default {default}")
        return default
    return value


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
# UWAGA: feed ForexFactory nazywa decyzje stóp per bank centralny, NIE
# "Interest Rate Decision": USD = "Federal Funds Rate", GBP = "Official Bank
# Rate", CAD = "Overnight Rate", AUD = "Cash Rate", NZD = "Official Cash Rate".
# Brak tych nazw = FOMC niewidoczny dla selekcji (bug z 29.07.2026).
TIER1_EVENTS = [
    "Interest Rate Decision",
    "Federal Funds Rate",
    "Official Bank Rate",
    "Overnight Rate",
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

# Pełne rostery (niezmienne bazy do filtrowania). Panel zapisuje listę
# WYŁĄCZONYCH nazw (disabled_events w runtime_overrides.json); TIER1_EVENTS/
# TIER2_EVENTS są filtrowanym widokiem tych rosterów — nigdy odwrotnie,
# inaczej pierwszy zapis z panelu bezpowrotnie skróciłby bazę.
# Semantyka "wyłączone", nie "włączone": nazwa DODANA do rostera po ostatnim
# zapisie z panelu jest domyślnie AKTYWNA. Stary format (enabled_events =
# lista włączonych) przycinał roster do tego, co panel znał w chwili zapisu —
# dashboard z 13 hardcodowanymi nazwami wycinał w ten sposób "Federal Funds
# Rate"/"Official Bank Rate"/"Overnight Rate" dodane 29.07 (FOMC/BoE/BoC
# niehandlowalne po każdym Save). Migracja legacy w _apply_runtime_overrides.
TIER1_EVENTS_ALL = list(TIER1_EVENTS)
TIER2_EVENTS_ALL = list(TIER2_EVENTS)

# Nazwy, które dashboard sprzed 18.08.2026 renderował i wysyłał w `events`.
# Tylko TE mógł operator świadomie odznaczyć — brak każdej innej nazwy w
# legacy `enabled_events` to skutek nieświadomości panelu, nie decyzja.
LEGACY_PANEL_EVENT_ROSTER = (
    "Interest Rate Decision", "Cash Rate", "Official Cash Rate",
    "Non-Farm Payrolls", "NFP", "CPI",
    "Employment Change", "Unemployment Rate", "GDP", "Advance GDP",
    "Retail Sales", "New Home Sales", "Existing Home Sales",
)

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

# "Prognozy / wypowiedzi / zapisy głosowań" — eventy bez publikowanej twardej
# liczby, na której da się grać zaskoczenie actual-vs-forecast. Dopasowanie
# podciągiem, case-insensitive. NIGDY nie handlowane, niezależnie od
# TRADE_ALL_EVENTS.
NON_DATA_EVENT_MARKERS = [
    "speaks",
    "speech",
    "testifies",
    "testimony",
    "press conference",
    "q&a",
    "projections",
    # "MPC Official Bank Rate Votes" publikuje rozkład głosów ("0-0-9"), nie
    # stopę — parse_numeric czyta to jako 0 i UDAJE brak zmiany. Nazwa zawiera
    # podciąg "Official Bank Rate" z TIER1, więc bez tego markera event byłby
    # handlowalny i mógł PRZESŁONIĆ prawdziwą decyzję BoE z tej samej minuty
    # (serwer ma jeden slot next_decision). regime_tracker wyklucza "votes"
    # od dawna z tego samego powodu — tu brakowało symetrii.
    "votes",
]

# Eventy "odwrócone": WYŻSZA wartość jest NEGATYWNA dla waluty (bezrobocie,
# wnioski o zasiłek). Dopasowanie podciągiem, case-insensitive — dla tych
# eventów IMPROVEMENT/DETERIORATION w porównaniu forecast-vs-previous jest
# zamieniane miejscami. Świadomie krótka lista: tylko jednoznaczne przypadki.
LOWER_IS_BETTER_MARKERS = [
    "unemployment",
    "jobless",
    "claims",
]

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
# LLM CONFIGURATION
# =============================================================================
LLM_CONFIG = {
    "provider": "openrouter",  # "openrouter", "anthropic", "openai", "rule-based"
    # Entry-decision model. Default verified against the live OpenRouter
    # catalog 2026-07-18: claude-opus-4.8 ($5/$25 per M) is BOTH newer and
    # 3x cheaper than the previous default claude-opus-4 ($15/$75, legacy
    # pricing). Override per machine without code edits:
    #   SKYTOWER_ENTRY_MODEL=anthropic/claude-fable-5   (top model, $10/$50;
    #       with K=3 ensemble ~= $0.40/traded event all-in)
    # Measured token profile: ~3k in / ~0.7k out per entry call.
    # A/B candidates offline first: tools/replay_decisions.py --variant.
    "model": os.getenv("SKYTOWER_ENTRY_MODEL",
                       "anthropic/claude-opus-4.8").strip()
             or "anthropic/claude-opus-4.8",
    # Completion budget for one entry vote. Measured visible output is ~700
    # tokens, so 1500 was ample for a NON-reasoning model — but Google counts
    # thinking tokens against maxOutputTokens, so a mandatory-reasoning panel
    # member (gemini-3.1-pro-preview) burned the budget before emitting any
    # JSON and returned a 200 with unusable content. That is a vote silently
    # lost at T-150s, which is how the panel kept degrading to 2/3. Unused
    # headroom is free (only generated tokens are billed), so give it room and
    # cap the thinking separately via reasoning_effort below.
    "max_tokens": _env_int("SKYTOWER_ENTRY_MAX_TOKENS", 4000),
    "temperature": 0.3,  # Lower = more consistent decisions
    # Thinking budget for reasoning models, sent to OpenRouter as
    # {"reasoning": {"effort": ...}}. Accepted: max|xhigh|high|medium|low|
    # minimal|none; OpenRouter silently maps or drops it for models that do
    # not reason, so it is always safe to send.
    #
    # "low" on purpose. The entry panel runs against a WALL CLOCK: analysis
    # starts at PRELOAD_SECONDS before the release and the deadline is
    # T-20s, so a model that thinks for two minutes is not a slow vote, it is
    # a MISSING vote (and providers that count reasoning against max_tokens
    # return a 200 with an empty body — the silent drop this panel already
    # had to learn to log). The schema wants short reasoning-first JSON, not
    # a long private deliberation.
    "reasoning_effort": os.getenv("SKYTOWER_ENTRY_REASONING_EFFORT",
                                  "low").strip().lower() or "low",
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
    # Close if the whole trade's profit drops >X% from its peak
    "profit_protection_percent": _env_ranged_float("SKYTOWER_PROFIT_PROTECTION_PERCENT", 50.0, 10, 95),
    # Profit protection arms only after the peak reaches this % of the trade's
    # max-loss budget (min $10). A flat $20 floor was ~1.3 pips at 1.57 lots
    # and armed on spread noise (2026-08-04 NZD postmortem).
    "profit_protection_floor_pct": _env_ranged_float("SKYTOWER_PROFIT_PROTECTION_FLOOR_PCT", 30.0, 5, 200),
    # No profit-protection closes this many seconds after open: the release
    # whipsaw (retrace 25-45% per playbooks) lives inside this window. Matches
    # the EA's hot reporting period by default.
    "profit_protection_grace_seconds": _env_ranged_int("SKYTOWER_PROFIT_PROTECTION_GRACE_SECONDS", 120, 0, 600),

    # AI decision intervals
    "llm_check_interval_seconds": 30,  # How often to consult LLM for exit decisions
    "hot_period_seconds": 120,         # Fast polling period after position open
    "hot_poll_interval": 5,            # EA poll interval during hot period (seconds)
    "normal_poll_interval": 15,        # EA poll interval after hot period (seconds)

    # Daily limits (USD and count)
    "max_daily_loss_usd": _env_float("SKYTOWER_MAX_DAILY_LOSS_USD", 300.0),  # stop for the day past this
    "max_daily_trades": _env_int("SKYTOWER_MAX_DAILY_TRADES", 5),            # max trades per day

    # LLM model for exit decisions — called every ~30s while a position is
    # open (20-60x per trade), so the cheaper tier matters here. Default
    # verified 2026-07-18: sonnet-5 ($2/$10) is newer AND cheaper than the
    # previous sonnet-4 ($3/$15). Also used by the aux channel (reflections,
    # playbook distillation) via llm_util.
    "exit_llm_model": os.getenv("SKYTOWER_EXIT_MODEL",
                                "anthropic/claude-sonnet-5").strip()
                      or "anthropic/claude-sonnet-5",
    # Thinking budget for the exit model (same OpenRouter field as the entry
    # one). Even lower stakes for deliberation and higher stakes for latency:
    # this runs 20-60x per trade against a 30s timeout, and the answer is a
    # <=40-word reasoning plus one action — there is nothing here worth two
    # minutes of private thought.
    "exit_reasoning_effort": os.getenv("SKYTOWER_EXIT_REASONING_EFFORT",
                                       "low").strip().lower() or "low",
    "exit_max_tokens": _env_int("SKYTOWER_EXIT_MAX_TOKENS", 2000),
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
#
# SKYTOWER_OVERRIDES_FILE relocates it. This exists for TESTS: config.py is
# imported (and re-imported in a subprocess by test_config) from the real
# python/ directory, so without a hook every test that asserts a default or an
# env override actually reads the OPERATOR's live panel state — which is how
# two model-override tests came to fail on every run, silently retiring the
# only check that SKYTOWER_ENTRY_MODEL still reaches LLM_CONFIG.
_OVERRIDES_FILE = os.getenv("SKYTOWER_OVERRIDES_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'logs', 'runtime_overrides.json')

# Persistent closed-trades log (PositionManager) — daily statistics survive
# watchdog restarts. Lives next to the other JSONL stores in logs/.
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'logs', 'trade_history.jsonl')
ACTIVE_POSITION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'logs', 'active_position.json')

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


# Import-time notes that must reach server.log (config.py has no logger yet;
# print() is invisible on the 24/7 machine). server.py logs them at startup.
CONFIG_NOTES: list = []


def _note(msg: str) -> None:
    CONFIG_NOTES.append(msg)
    print(f"WARNING: {msg}")


def _read_runtime_overrides() -> dict:
    """The overrides file as a dict; {} when missing. A corrupt / non-dict
    file must never take the server down at import, but it must not be
    SILENT either: every consumer (risk limits, whitelist, models, routing)
    would quietly revert to env/defaults — so it is reported via CONFIG_NOTES
    and left in place (save_runtime_overrides moves it aside, never
    overwrites it)."""
    import json
    if not os.path.exists(_OVERRIDES_FILE):
        return {}
    try:
        with open(_OVERRIDES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        _note(f"runtime_overrides.json unreadable ({e.__class__.__name__}: {e}) — "
              f"panel settings NOT applied, env/defaults in force: {_OVERRIDES_FILE}")
        return {}
    if not isinstance(data, dict):
        _note(f"runtime_overrides.json is not a JSON object — ignored: {_OVERRIDES_FILE}")
        return {}
    return data


def save_runtime_overrides(updates: dict):
    """Merge updates into the overrides file (atomic write). A value of None
    REMOVES that key (used to retire superseded keys such as the legacy
    `enabled_events`). An existing but unparseable file is moved to
    *.corrupt-<timestamp> first so the operator's previous panel state is
    preserved for inspection instead of being replaced by a file holding only
    this one key."""
    import json
    data = _read_runtime_overrides()
    if not data and os.path.exists(_OVERRIDES_FILE) and os.path.getsize(_OVERRIDES_FILE) > 0:
        try:
            with open(_OVERRIDES_FILE, 'r', encoding='utf-8') as f:
                json.load(f)
        except (OSError, ValueError):
            from datetime import datetime as _dt
            aside = f"{_OVERRIDES_FILE}.corrupt-{_dt.utcnow():%Y%m%d%H%M%S}"
            try:
                os.replace(_OVERRIDES_FILE, aside)
                _note(f"corrupt runtime_overrides.json moved to {aside}")
            except OSError:
                pass
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    os.makedirs(os.path.dirname(_OVERRIDES_FILE), exist_ok=True)
    tmp = _OVERRIDES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _OVERRIDES_FILE)


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(e, str) for e in value)


def _apply_disabled_events(disabled) -> None:
    """Rebuild the effective TIER lists from the immutable rosters minus the
    disabled names (never shrinks the *_ALL baselines)."""
    global TIER1_EVENTS, TIER2_EVENTS, HIGH_IMPACT_EVENTS
    disabled_set = set(disabled or ())
    TIER1_EVENTS = [e for e in TIER1_EVENTS_ALL if e not in disabled_set]
    TIER2_EVENTS = [e for e in TIER2_EVENTS_ALL if e not in disabled_set]
    HIGH_IMPACT_EVENTS = TIER1_EVENTS + TIER2_EVENTS


def disabled_event_names() -> list:
    """Roster names currently switched off (what the panel persists)."""
    effective = set(TIER1_EVENTS) | set(TIER2_EVENTS)
    return [n for n in TIER1_EVENTS_ALL + TIER2_EVENTS_ALL if n not in effective]


# Sentinel for `known_roster`: "I speak for the WHOLE roster, disable
# everything I did not list." Scripted callers that really want the full
# complement must say so explicitly — see set_enabled_events.
ROSTER_ALL = "*"


def set_enabled_events(enabled: list, known_roster=None) -> list:
    """Panel contract: the dashboard posts the names it shows CHECKED (out of
    the full rosters it received from /api/config/events). Everything else the
    caller SPEAKS FOR becomes disabled; unknown names are ignored. Persists
    `disabled_events` and retires the legacy `enabled_events` key. Returns the
    disabled list.

    `known_roster` — the names the CLIENT actually rendered, i.e. the scope it
    is entitled to switch off. Names outside it keep whatever state they have.
    A tab loaded before a roster-changing restart (code deploy,
    SKYTOWER_EXTRA_EVENTS edit) still holds the old roster, and without this
    scope its Save would disable every name added since — the same silent-loss
    shape as the bug this key replaced.

    Accepted values:
      * a list of names — the client's rendered roster (what the panel sends);
      * ROSTER_ALL ("*") — "the full roster", for scripted callers that
        deliberately want the complement of everything;
      * omitted/invalid — LEGACY_PANEL_EVENT_ROSTER, the only roster a
        client that does not announce one could plausibly have rendered.

    The default is deliberately NOT the full roster. The pre-18.08.2026
    dashboard posts `events` without `roster`, and an operator whose tab (or
    browser cache) predates the fix would otherwise re-disable the six names
    it never displayed — Federal Funds Rate, Official Bank Rate, Overnight
    Rate, Nonfarm Payrolls, Consumer Price Index, Gross Domestic Product —
    and persist them under the NEW key, where the legacy migration can no
    longer rescue them. That turns a self-healing bug into a permanent one.
    """
    enabled_set = set(enabled)
    roster = TIER1_EVENTS_ALL + TIER2_EVENTS_ALL
    if known_roster == ROSTER_ALL:
        known = set(roster)
    elif _is_str_list(known_roster):
        known = set(known_roster)
    else:
        known = set(LEGACY_PANEL_EVENT_ROSTER)
    still_off = set(disabled_event_names())
    disabled = []
    for name in roster:
        # A name the client rendered: its checkbox is the verdict.
        # A name it never saw: leave it exactly as it is.
        off = (name not in enabled_set) if name in known else (name in still_off)
        if off:
            disabled.append(name)
    _apply_disabled_events(disabled)
    save_runtime_overrides({"disabled_events": disabled, "enabled_events": None})
    return disabled


def _apply_runtime_overrides():
    data = _read_runtime_overrides()
    if not data:
        return
    global MIN_IMPACT_LEVEL, TRADE_ALL_EVENTS
    if str(data.get('min_impact', '')).upper() in ("LOW", "MEDIUM", "HIGH"):
        MIN_IMPACT_LEVEL = str(data['min_impact']).upper()
    if isinstance(data.get('trade_all_events'), bool):
        TRADE_ALL_EVENTS = data['trade_all_events']
    # Panel-persisted event whitelist: list of DISABLED names, applied as a
    # filter over the immutable *_ALL rosters (unknown names are ignored,
    # roster additions stay enabled). Legacy `enabled_events` (a list of
    # ENABLED names written by the pre-18.08.2026 dashboard, which only knew
    # LEGACY_PANEL_EVENT_ROSTER) is migrated: only names that panel could
    # actually show count as deliberately disabled — every other roster name
    # it silently dropped (Federal Funds Rate, Official Bank Rate, Overnight
    # Rate, ...) is re-enabled, and the migration is reported.
    disabled = data.get('disabled_events')
    if _is_str_list(disabled):
        _apply_disabled_events(disabled)
    elif _is_str_list(data.get('enabled_events')):
        legacy_enabled = set(data['enabled_events'])
        migrated = [n for n in LEGACY_PANEL_EVENT_ROSTER if n not in legacy_enabled]
        rescued = [n for n in TIER1_EVENTS_ALL + TIER2_EVENTS_ALL
                   if n not in legacy_enabled and n not in LEGACY_PANEL_EVENT_ROSTER]
        _apply_disabled_events(migrated)
        _note(f"runtime_overrides.json: legacy 'enabled_events' migrated to "
              f"'disabled_events' ({len(migrated)} name(s) stay disabled: {migrated}); "
              f"roster names the old panel could not display are re-enabled: "
              f"{rescued}. Save the Event Config panel once to persist the new key.")
    for key, lo, hi, cast in (("max_daily_trades", 1, 100, int),
                              ("max_daily_loss_usd", 10, 1_000_000, float),
                              ("max_loss_usd", 5, 100_000, float),
                              ("profit_protection_percent", 10, 95, float),
                              ("profit_protection_floor_pct", 5, 200, float),
                              ("profit_protection_grace_seconds", 0, 600, int)):
        if key in data:
            try:
                value = cast(data[key])
                if lo <= value <= hi:
                    POSITION_MANAGEMENT_CONFIG[key] = value
            except (TypeError, ValueError):
                pass


def risk_limit_conflicts(cfg: dict) -> list:
    """Cross-field sanity for the risk limits. Each field is range-checked in
    isolation (max_loss_usd accepts 5..100_000), but the RELATION between them
    was never checked: a per-trade cap ABOVE the daily cap lets a single trade
    spend the whole day's budget, because the daily limit only blocks the NEXT
    entry. max_loss_usd is also what the EA uses to size the lot, so an
    oversized value inflates the position AND raises every guardrail that was
    supposed to cap the loss.

    Returns a list of human-readable conflicts (empty = consistent). Callers
    decide the remedy: import-time clamps, the panel endpoint rejects.
    """
    conflicts = []
    per_trade = cfg.get("max_loss_usd")
    daily = cfg.get("max_daily_loss_usd")
    if (isinstance(per_trade, (int, float))
            and isinstance(daily, (int, float))
            and per_trade > daily):
        conflicts.append(
            f"max_loss_usd (${per_trade:,.0f} per trade) exceeds "
            f"max_daily_loss_usd (${daily:,.0f} per day) — one trade could "
            f"spend the entire daily loss budget"
        )
    return conflicts


_apply_runtime_overrides()

def _clamp_risk_limits() -> list:
    """Apply the cross-field rule at import so a stale runtime_overrides.json
    can never arm a per-trade budget bigger than the whole day's. Returns the
    notes for the server to log at startup — config.py has no logger, and a
    silent clamp would be just as opaque as the silent override it fixes."""
    notes = []
    for conflict in risk_limit_conflicts(POSITION_MANAGEMENT_CONFIG):
        clamped = float(POSITION_MANAGEMENT_CONFIG["max_daily_loss_usd"])
        POSITION_MANAGEMENT_CONFIG["max_loss_usd"] = clamped
        notes.append(f"{conflict}; clamped to ${clamped:,.0f}")
        print(f"WARNING: {notes[-1]}")
    return notes


RISK_LIMIT_NOTES: list = _clamp_risk_limits()

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

# ENSEMBLE PANEL (F4c): heterogeneous committee instead of K same-model
# samples. Comma-separated OpenRouter model ids; empty = classic K-call
# self-consistency above. With >= 2 ids the entry engine fires ONE call per
# listed model. The FIRST id is the ANCHOR: a trade requires the anchor's
# BUY/SELL to be confirmed by >= 1 other model with NO opposite vote (SKIP
# votes abstain, an opposite vote vetoes), and the traded decision reports
# the ANCHOR's confidence/reasoning — confidence scales are not comparable
# across vendors, so they are never mixed or averaged. Same-model sampling
# catches one-off flukes; a mixed panel also catches the anchor's
# systematic blind spots (correlated errors are what unanimity cannot see).
# OpenRouter provider only — one client serves every vendor there.
# SKYTOWER_ENSEMBLE_K is ignored while the panel is active.
ENSEMBLE_MODELS = [m.strip() for m in
                   os.getenv("SKYTOWER_ENSEMBLE_MODELS", "").split(",")
                   if m.strip()]
if len(ENSEMBLE_MODELS) == 1:
    print("WARNING: SKYTOWER_ENSEMBLE_MODELS needs >= 2 models — ignoring it "
          "(use SKYTOWER_ENTRY_MODEL to change the single-call model)")
    ENSEMBLE_MODELS = []
elif len(ENSEMBLE_MODELS) > 5:
    print(f"WARNING: SKYTOWER_ENSEMBLE_MODELS has {len(ENSEMBLE_MODELS)} "
          f"models — capping at 5")
    ENSEMBLE_MODELS = ENSEMBLE_MODELS[:5]


def _apply_model_runtime_overrides():
    """Panel-set AI model config (dashboard: Event Config -> AI Models,
    endpoint /api/config/models). Runs AFTER the env-based defaults above —
    _apply_runtime_overrides() at line ~415 fires BEFORE ENSEMBLE_K/
    ENSEMBLE_MODELS exist, so these keys need their own late pass to keep
    the default < env < panel precedence. Junk in the file is ignored;
    validation mirrors the endpoint's."""
    global ENSEMBLE_K, ENSEMBLE_MODELS
    data = _read_runtime_overrides()
    if not data:
        return
    entry = data.get('entry_model')
    if isinstance(entry, str) and '/' in entry and entry.strip():
        LLM_CONFIG['model'] = entry.strip()
    exit_m = data.get('exit_model')
    if isinstance(exit_m, str) and '/' in exit_m and exit_m.strip():
        POSITION_MANAGEMENT_CONFIG['exit_llm_model'] = exit_m.strip()
    k = data.get('ensemble_k')
    if isinstance(k, int) and 1 <= k <= 5:
        ENSEMBLE_K = k
    models = data.get('ensemble_models')
    if isinstance(models, list):
        cleaned = [str(m).strip() for m in models if str(m).strip()]
        if len(cleaned) == 0 or 2 <= len(cleaned) <= 5:
            ENSEMBLE_MODELS = cleaned


_apply_model_runtime_overrides()

# =============================================================================
# EVENT -> INSTRUMENT ROUTING (multi-instrument)
# =============================================================================
# For an event of currency X, the decision is normally made for
# DEFAULT_PAIRS[X] (a forex pair). INSTRUMENT_ROUTING lets the operator put
# OTHER instruments in front of that default: the first routed symbol whose
# EA chart has pushed FRESH market data wins; if none has data the flow is
# exactly today's (DEFAULT_PAIRS + base-currency fallback). Why: the same USD
# print moves gold/US500 as much as the forex pairs in %, at 2-8% of the move
# in cost instead of 40-100% (research/DAX_OPEN_PLAN.md §10). A routed symbol
# must be a forex pair or a profiled instrument (instrument_profiles.py) AND
# must carry the event currency as a leg — the same rule for env, the
# overrides file and the panel endpoint (normalize_instrument_routing).
#
#   SKYTOWER_INSTRUMENT_ROUTING="USD:XAUUSD;NZD:NZDUSD"
#     -> {"USD": ["XAUUSD"], "NZD": ["NZDUSD"]}
#
# Empty (default) = routing OFF = byte-identical behaviour. Panel-persisted
# value (runtime_overrides.json key "instrument_routing") outranks env.


def normalize_instrument_routing(value, strict: bool = False) -> dict:
    """Canonical routing table from a string ('USD:XAUUSD;NZD:NZDUSD') or a
    dict ({'usd': ['xau/usd', 'USDCAD']} — symbol lists may also be
    comma-separated strings). Currency keys upper 3-letter alpha; symbols
    upper, '/' removed, de-duped, order preserved. Every symbol is validated
    with instrument_profiles.validate_routing_symbol (forex pair or profiled
    instrument, and it must carry the currency).

    strict=False (env / overrides file): invalid keys/symbols are DROPPED and
    reported via print (import time — no logger yet).
    strict=True (panel endpoint): the first problem raises ValueError so the
    operator sees exactly what was refused and nothing is applied.
    """
    from instrument_profiles import validate_routing_symbol
    if value is None:
        return {}
    if isinstance(value, str):
        raw = {}
        for chunk in value.split(";"):
            if ":" not in chunk:
                if chunk.strip() and strict:
                    raise ValueError(f"routing chunk '{chunk.strip()}' is not CUR:SYM,SYM")
                continue
            cur, syms = chunk.split(":", 1)
            raw.setdefault(cur, []).extend(syms.split(","))
    elif isinstance(value, dict):
        raw = {}
        for cur, syms in value.items():
            if isinstance(syms, str):
                syms = syms.split(",")
            if not isinstance(syms, (list, tuple)):
                if strict:
                    raise ValueError(f"{cur}: expected a list of symbols")
                continue
            raw[cur] = list(syms)
    else:
        if strict:
            raise ValueError("instrument_routing: expected dict or string")
        return {}

    out = {}
    for cur, syms in raw.items():
        cur_u = str(cur).strip().upper()
        if len(cur_u) != 3 or not cur_u.isalpha():
            if strict:
                raise ValueError(f"bad currency key '{cur}'")
            _note(f"instrument routing — bad currency key '{cur}' ignored")
            continue
        cleaned = []
        for sym in syms:
            sym_u = str(sym).strip().upper().replace("/", "")
            if not sym_u or sym_u in cleaned:
                continue
            problem = validate_routing_symbol(sym_u, cur_u)
            if problem:
                if strict:
                    raise ValueError(problem)
                _note(f"instrument routing — {problem}; entry ignored")
                continue
            cleaned.append(sym_u)
        if cleaned:
            out[cur_u] = cleaned
    return out


def parse_instrument_routing(text: str) -> dict:
    """Env-string form of normalize_instrument_routing (non-strict)."""
    return normalize_instrument_routing(text, strict=False)


def routing_candidates(currency: str) -> list:
    """Ordered instrument roots to try before DEFAULT_PAIRS for this currency
    (empty list = routing off for that currency)."""
    return list(INSTRUMENT_ROUTING.get((currency or "").upper(), []) or [])


INSTRUMENT_ROUTING = parse_instrument_routing(os.getenv("SKYTOWER_INSTRUMENT_ROUTING", ""))


def _apply_routing_runtime_overrides():
    """Panel-set routing table (endpoint /api/config/routing). Same late-pass
    pattern as the model overrides: default < env < panel."""
    global INSTRUMENT_ROUTING
    value = _read_runtime_overrides().get('instrument_routing')
    if isinstance(value, (dict, str)):
        INSTRUMENT_ROUTING = normalize_instrument_routing(value, strict=False)


_apply_routing_runtime_overrides()

# =============================================================================
# TEST MODE: FORCE DECISION (never SKIP)
# =============================================================================
# When enabled, the entry decision engine must always pick BUY or SELL —
# SKIP is disabled at every level (LLM prompt, parse fallback, rule fallback).
# Intended ONLY for the demo-account data-collection phase. Lives in
# python/.env (SKYTOWER_FORCE_DECISION=true); the native server is the
# primary run mode and docker-compose deliberately does not set it.
# GOING LIVE = delete that .env line (see RUNBOOK "Przejście na LIVE").
FORCE_DECISION = _env_bool("SKYTOWER_FORCE_DECISION", False)
