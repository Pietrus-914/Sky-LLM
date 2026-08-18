"""
LLM Decision Engine for SkyTower-AI
Uses AI to analyze multiple data sources and make trading decisions
"""
import json
from datetime import datetime
from timeutil import utcnow
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict, field
from loguru import logger
from uuid import uuid4
import os
import threading

# Import our data modules
from calendar_fetcher import CalendarAggregator, EconomicEvent
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from event_reaction_history import (EventReactionHistory, normalize_event_name,
                                    is_test_event_name)
from decision_history import DecisionHistory
from llm_util import openrouter_headers, reasoning_body
from market_context import normalize_pair
from instrument_profiles import profile_for, same_asset_class
from event_cluster import co_release_brief
from config import (LLM_CONFIG, TRADING_CONFIG, DEFAULT_PAIRS,
                    HIGH_IMPACT_EVENTS, OPENROUTER_API_KEY, FORCE_DECISION,
                    ENSEMBLE_K, ENSEMBLE_MODELS)


# Bump MANUALLY on every substantive entry-prompt change. Recorded with each
# decision so calibration/replay can be grouped per prompt version — without
# it, prompt edits silently mix regimes inside one calibration ledger.
ENTRY_PROMPT_VERSION = "2026-08-05.1"

# Minimum VALID votes for an ensemble result to be called a panel decision.
# Below this the committee has silently shrunk (a vendor timed out, or replied
# 200 with unusable content) and the outcome is one model's opinion. Normal
# mode already refuses to trade there; FORCE_DECISION demo mode may not SKIP,
# so it trades but labels the decision honestly (see _ensemble_decision).
ENSEMBLE_MIN_QUORUM = 2

# Wall-clock budget for the whole panel. Entry fires at T-15s, so the decision
# must be recorded before that with a little room for aggregation and serving.
ENSEMBLE_DEADLINE_MARGIN = 20
# Floor, so a late-discovered event still gets a real chance to answer instead
# of being abandoned instantly.
ENSEMBLE_MIN_WAIT_SECONDS = 10


@dataclass
class TradingDecision:
    """Represents the AI's trading decision"""
    event: str
    currency: str
    pair: str
    direction: str  # "BUY", "SELL", "SKIP"
    confidence: float  # 0.0 to 1.0
    lot_percent: float  # Suggested lot percentage
    entry_seconds_before: int
    exit_minutes_after: int
    stop_loss_percent: float  # Legacy: kept for backward compatibility
    stop_loss_pips: float = 0  # SL in pips (preferred)
    take_profit_pips: float = 0  # TP in pips (preferred)
    reasoning: str = ""
    data_summary: Dict = None
    timestamp: datetime = None
    forced: bool = False  # True when FORCE_DECISION test mode influenced this decision
    # Stable id joining this decision to its signal, trade and reaction
    # records (decision_history / trade_history / event_reactions JSONL)
    decision_id: str = field(default_factory=lambda: uuid4().hex)
    # Raw LLM reply text — persisted to logs/decision_context/ for post-hoc
    # audit ("what did the model actually say"), empty for rule-based decisions
    raw_response: str = ""
    # Ensemble metadata (F4, SKYTOWER_ENSEMBLE_K >= 2): {"k", "valid",
    # "votes": [{"direction", "confidence"}, ...]}; None for single-call
    ensemble: Dict = None
    # Which engine produced the direction ("anthropic/claude-fable-5",
    # "panel:<models>", "rule-based") and under which entry-prompt version —
    # calibration must be computable PER MODEL and PER PROMPT VERSION, or
    # every prompt/model change silently resets the meaning of the ledger.
    model: str = ""
    prompt_version: str = ""

    def to_dict(self):
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        # Raw LLM text lives in logs/decision_context/ — duplicating it into
        # every /api/decision poll (serialized under decision_lock) is waste
        d.pop('raw_response', None)
        return d


class LLMDecisionEngine:
    """
    AI-powered decision engine that analyzes:
    1. Economic calendar events
    2. Forecast trends
    3. COT institutional positioning
    4. Retail sentiment (contrarian)
    5. Historical patterns

    Makes trading decisions based on combined analysis
    """

    # Contract ranges the LLM's numeric fields are clamped to before they can
    # reach the EA (forex defaults; documented in the SYSTEM_PROMPT schema).
    # A non-forex instrument (gold, indices) supplies its own ranges via
    # instrument_profiles — see _limits_for(). Kept as class attributes so the
    # values are pinnable in tests and overridable by a subclass.
    LOT_MAX = 85
    EXIT_RANGE = (5, 15)          # exit_minutes
    SL_RANGE = (25.0, 80.0)       # stop_loss_pips
    TP_RANGE = (8.0, 120.0)       # take_profit_pips

    # System prompt for the LLM
    SYSTEM_PROMPT = """You are an expert forex trading analyst specializing in news trading strategies.
Your task is to analyze economic data and make trading decisions for the SkyTower-FX strategy.

STRATEGY OVERVIEW:
- Trade high-impact economic news releases (Interest Rates, CPI, NFP, GDP, Retail Sales)
- Focus on currencies: NZD, CAD, AUD, USD, GBP (in order of effectiveness)
- Enter positions 10-20 seconds before news release
- Exit positions 5-15 minutes after release
- Use high leverage (1:500) with strict risk management

KEY PRINCIPLES:
1. FORECAST ANALYSIS: If forecasts have been trending better, expect currency strength
2. COT DATA: Follow institutional money (non-commercial traders)
3. RETAIL SENTIMENT: Trade AGAINST retail majority (contrarian)
4. PRICE ACTION: If price moved up before release, often moves opposite after (bank manipulation)
5. MARKET STRUCTURE: When current market data is provided, prefer the direction aligned
   with the higher-timeframe (H1) trend, and use nearby support/resistance and ATR
   volatility to judge entry quality and size stop_loss_pips / take_profit_pips
6. HISTORICAL REACTIONS: When past reactions to this event are provided, weigh how the
   pair actually moved on similar beat/miss outcomes
7. QUOTING (critical): "direction" refers to the SUGGESTED PAIR. When the event
   currency is the QUOTE currency of the pair (e.g. CAD in USDCAD), a bullish event
   currency means SELL the pair and a bearish one means BUY. Double-check which side
   of the pair the event currency is on before choosing the direction.

ANALYSIS CHECKLIST — think through EVERY point against the provided data before
answering (the reasoning field should reflect this analysis, output ONLY the JSON):
a. Surprise setup: how big is the forecast-vs-previous gap? Which outcome (beat/miss)
   has the asymmetric payoff, and is one side already priced in?
b. Pre-news drift: what do the last 30-60 min candles show? A strong run INTO the
   event often reverses on release (pre-positioning); a quiet coil often breaks hard.
   In our historical chart study the release move usually went AGAINST the final 1-3
   M1 candles — a sharp counter-move in the last minutes before release is more often
   the trap side than a leak (per-event measured rates are in the EVENT PLAYBOOK
   section when present). Read the stretch from the raw candles: when a one-sided
   pre-release run, the last-candle fade and your fundamental read all agree, treat
   it as strong confirmation. When technicals contradict a GENUINE policy surprise
   (a rate decision deviating from what markets priced), the fundamentals won
   historically — a merely as-forecast print is NOT such a surprise.
c. Chart evidence: read the raw candles — momentum, wicks, rejection levels,
   where the stops likely sit relative to nearest support/resistance. When the
   market-structure "zones" block reports liquidity_above / liquidity_below
   (measured equal-high/low stop clusters, with pip distance and strength),
   treat a cluster in the SURPRISE direction as potential FUEL that can extend
   the move if price runs it — never as a target the move must reach.
d. Volatility fit: are your stop_loss_pips/take_profit_pips consistent with ATR and
   the current spread (a stop inside 1x spread+ATR noise will be swept)? Entry happens
   seconds BEFORE the release, so also budget the stop for an adverse stop-run wick
   in the release seconds — the EVENT PLAYBOOK gives measured wick sizes for many
   events; if two estimates could apply, budget the LARGER. A stop tighter than
   wick+spread dies at the print even when the direction is right.
   TAKE-PROFIT REACHABILITY: the TP becomes a broker limit order at entry — an
   opportunistic banker of the spike that the 5-15s report cycle would give
   back; the server exit engine still closes the trade on schedule either way.
   Size it from the measured "favorable run" stats when provided: between the
   MEDIAN (hit in roughly half of correctly-called releases) and the p80 (the
   strong-spike tail, hit in ~20%), and never above the p80 for your exit
   window. The 30-min figures often print near T+30 — within a 5-15 min hold
   treat them as a ceiling, not a target. A TP beyond the measured run is a
   lottery ticket. UNITS CAVEAT: the measured run is BID-path travel, while a
   TP is a P/L distance that fills only after price travels TP + spread — so
   subtract the current spread from the measured stat before anchoring, and
   always keep TP >= ~2x current spread.
e. Historical reactions, LEARNED EVENT STATISTICS (when provided) and YOUR
   TRACK RECORD: the measured medians and hit-rates are your statistical prior —
   anchor on them, then adjust for today's specifics. Respect sample sizes:
   n under ~10 is weak evidence, never a rule. Were your own recent calls on
   this currency right or wrong? Do not repeat a documented mistake.
f. Quoting check (principle 7): confirm the direction maps correctly to the pair.

DECISION OUTPUT FORMAT:
You must respond with a JSON object. Write the "reasoning" field FIRST — work
through the checklist there (historical base rate, then your adjustments, then
a brief over/under-confidence self-check) — and only then commit the numeric
and direction fields:
{
    "reasoning": "Base rate -> adjustments -> confidence self-check -> conclusion. CONCISE: max ~120 words — the response must never be cut off before the fields below",
    "stop_loss_pips": %SL_RANGE%,
    "take_profit_pips": %TP_RANGE% (when measured stats are provided, size it between
        the MEDIAN and the p80 of the favorable run for your exit window — near the
        median for a high-probability bank, toward the p80 only with strong
        continuation evidence, never above it. NOT a fixed multiple of SL — but if
        the reachable TP is under ~0.5x your SL, question whether the trade is
        worth its risk at all),
    "exit_minutes": %EXIT_RANGE%,
    "lot_percent": 60 to 85 (percent of max lot),
    "direction": %DIRECTION_VALUES%,
    "confidence": 0.0 to 1.0
}

%SKIP_POLICY%"""

    SKIP_POLICY_NORMAL = """SKIP the trade if:
- Confidence is below 0.5
- Data is conflicting with no clear signal
- Event is marked as preliminary ("P" flag)
- No clear directional bias from combined analysis"""

    SKIP_POLICY_FORCED = """IMPORTANT — TEST MODE (data collection on a demo account):
You MUST choose BUY or SELL. SKIP is NOT allowed.
If the evidence is weak or conflicting, pick the MORE PROBABLE direction and
express your uncertainty through a LOW confidence value. Report honest
confidence — do NOT inflate it just because a direction is required."""

    def _system_prompt(self, pair: Optional[str] = None) -> str:
        """Build the system prompt for the current mode (normal vs
        FORCE_DECISION) and for the instrument the decision is for: the
        numeric schema ranges are the SAME ranges the clamps enforce
        (_limits_for), so a gold decision is never told "25 to 80" while the
        INSTRUMENT block says 50-100. Forex text is byte-identical to before
        the ranges became placeholders."""
        lim = self._limits_for(pair)
        if profile_for(pair) is None:
            sl_txt = "25 to 80 (wider for JPY pairs: 40-80)"
            tp_txt = "8 to 120"
            exit_txt = "5 to 15"
        else:
            sl_txt = f"{lim['sl'][0]:g} to {lim['sl'][1]:g} ({lim['units']})"
            tp_txt = f"{lim['tp'][0]:g} to {lim['tp'][1]:g} ({lim['units']})"
            exit_txt = f"{lim['exit'][0]} to {lim['exit'][1]}"
        text = (self.SYSTEM_PROMPT
                .replace("%SL_RANGE%", sl_txt)
                .replace("%TP_RANGE%", tp_txt)
                .replace("%EXIT_RANGE%", exit_txt))
        if FORCE_DECISION:
            return (text
                    .replace("%DIRECTION_VALUES%", '"BUY" or "SELL"')
                    .replace("%SKIP_POLICY%", self.SKIP_POLICY_FORCED))
        return (text
                .replace("%DIRECTION_VALUES%", '"BUY" or "SELL" or "SKIP"')
                .replace("%SKIP_POLICY%", self.SKIP_POLICY_NORMAL))

    def __init__(self, api_key: str = None, provider: str = None,
                 decision_log=None, trade_history_file: str = None,
                 regime_provider=None, paths_provider=None):
        """
        Initialize the decision engine

        Args:
            api_key: API key for the LLM provider
            provider: "openrouter", "anthropic", "openai", or "rule-based"
            decision_log: shared DecisionHistory instance. MUST be the same
                object the server records decisions into — a private copy
                only sees what was on disk at startup, so the TRACK RECORD
                prompt section would never include in-session decisions.
            trade_history_file: path to the closed-trades JSONL written by
                PositionManager; feeds the RECENT TRADE OUTCOMES section.
            regime_provider: callable currency -> regime|None (the live
                RegimeTracker). Selects the current-regime bucket of the
                LEARNED EVENT STATISTICS; falls back to the static
                config.CURRENCY_REGIMES seed when not wired (tests).
            paths_provider: callable () -> list of measured event-path
                records (the live EventPathRecorder's in-memory copies).
                Feeds the CALIBRATION prompt line; None (tests) = no line.
        """
        # Auto-detect provider from config or environment
        self.provider = provider or LLM_CONFIG.get("provider", "openrouter")
        self.api_key = api_key or OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")

        # Initialize data sources
        self.calendar = CalendarAggregator()
        self.cot_analyzer = COTAnalyzer()
        self.sentiment = SentimentAggregator()
        self.reaction_history = EventReactionHistory()
        # Past decisions — feeds the TRACK RECORD prompt section so the
        # model can see (and correct for) its own hit rate
        self.decision_log = decision_log or DecisionHistory()
        # Closed trades (realized P/L) — feeds RECENT TRADE OUTCOMES
        from config import (TRADE_HISTORY_FILE, EVENT_PLAYBOOKS_FILE,
                            LEARNED_STATS_FILE, REFLECTIONS_FILE)
        self.trade_history_file = trade_history_file or TRADE_HISTORY_FILE
        # Curated event playbooks (optional knowledge file)
        self.playbooks_file = EVENT_PLAYBOOKS_FILE
        self._playbooks_cache = None
        self._playbooks_mtime = None
        # Machine-built frequency stats (LEARNED EVENT STATISTICS section)
        self.learned_stats_file = LEARNED_STATS_FILE
        self._learned_cache = None
        self._learned_mtime = None
        self._regime_provider = regime_provider
        self._paths_provider = paths_provider
        # Post-trade reflections (F5) — mtime-cached JSONL reader
        from reflections import ReflectionStore
        self.reflection_store = ReflectionStore(REFLECTIONS_FILE)

        # Initialize LLM client
        self._init_llm_client()

    def _init_llm_client(self):
        """Initialize the appropriate LLM client"""
        if self.provider == "openrouter":
            # OpenRouter uses OpenAI-compatible API
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url="https://openrouter.ai/api/v1",
                    # The SDK default (2) silently TRIPLES the per-attempt
                    # timeout, so a 60s call could hold a panel slot for ~180s
                    # plus backoff — past the release. The ensemble does its own
                    # smarter retry (it re-prompts for valid JSON), so transport
                    # retries here only cost the decision its time budget.
                    max_retries=0,
                )
                self.model = LLM_CONFIG.get("model", "anthropic/claude-opus-4")
                logger.info(f"Initialized OpenRouter client with model: {self.model}")
            except ImportError:
                logger.warning("OpenAI package not installed, falling back to rule-based")
                self.client = None
        elif self.provider == "anthropic":
            try:
                from anthropic import Anthropic
                self.client = Anthropic(api_key=self.api_key)
                self.model = "claude-3-5-sonnet-20241022"
                logger.info("Initialized Anthropic client")
            except ImportError:
                logger.warning("Anthropic package not installed, falling back to rule-based")
                self.client = None
        elif self.provider == "openai":
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
                self.model = "gpt-4o"
                logger.info("Initialized OpenAI client")
            except ImportError:
                logger.warning("OpenAI package not installed, falling back to rule-based")
                self.client = None
        else:
            self.client = None
            logger.info("Using rule-based decision engine (no LLM)")

    def analyze_event(self, event: EconomicEvent, market_context: Dict = None,
                      co_released: List[Dict] = None) -> TradingDecision:
        """
        Analyze an upcoming economic event and make a trading decision

        Args:
            event: The economic event to analyze — for a same-minute cluster
                this is the DOMINANT member (event_cluster.pick_dominant), the
                one the trade and its statistics are filed under.
            market_context: Optional dict from market_context.build_market_context()
                (trend, ATR, S/R zones from EA-pushed OHLC). None when no EA
                has pushed data for this event yet.
            co_released: Other prints of the SAME release minute
                (event_cluster.co_release_brief) — the market prices their
                combined surprise, so the model must see them. Empty/None for
                a solitary event, and then the prompt is unchanged.

        Returns:
            TradingDecision object
        """
        # Gather all data
        data_context = self._gather_data(event, market_context, co_released)

        # Make decision (LLM or rule-based)
        if self.client:
            logger.info(f"Using LLM ({self.provider}) for decision on {event.event_name}")
            decision = self._llm_decision(event, data_context)
        else:
            logger.info(f"Using rule-based decision for {event.event_name}")
            decision = self._rule_based_decision(event, data_context)

        return self._enforce_instrument_floor(decision)

    @staticmethod
    def _enforce_instrument_floor(decision: 'TradingDecision') -> 'TradingDecision':
        """A tradeable decision for a NON-forex instrument must carry an
        explicit stop in the instrument's own unit.

        The forex contract lets stop_loss_pips=0 mean "not set — EA fallback"
        (the EA then uses its 25-pip default), which is a sane forex stop but
        $2.50 on XAUUSD (1 pip = $0.10): the lot would be sized against a stop
        the release spread alone can take out. Every path that can leave the
        sentinel (rule-based fallback, unparseable panel reply, FORCE remap)
        converges here, so the floor is applied once for all of them. Forex
        decisions are returned untouched.
        """
        if decision is None or decision.direction not in ("BUY", "SELL"):
            return decision
        prof = profile_for(decision.pair)
        if prof is None:
            return decision
        sl = float(decision.stop_loss_pips or 0)
        if sl <= 0:
            decision.stop_loss_pips = float(prof.default_sl_pips)
            decision.reasoning = (f"{decision.reasoning or ''} [no SL from the model — "
                                  f"{prof.name} profile default {prof.default_sl_pips:g} pips applied]")
            logger.warning(f"{prof.name}: decision without stop_loss_pips — applying "
                           f"profile default {prof.default_sl_pips:g} pips")
        return decision

    def _gather_data(self, event: EconomicEvent, market_context: Dict = None,
                     co_released: List[Dict] = None) -> Dict:
        """Gather all relevant data for decision making"""
        currency = event.currency.upper()
        source_status = {}

        if market_context:
            age = market_context.get('data_age_minutes')
            source_status["market"] = "ok" if age is None or age <= 15 else f"stale ({age} min old)"
        else:
            source_status["market"] = "no_data"

        # Get COT positioning
        cot_data = self.cot_analyzer.analyze_currency(currency)
        if isinstance(cot_data, dict) and 'error' in cot_data:
            logger.warning(f"COT data unavailable for {currency}: {cot_data.get('error')}")
            source_status["cot"] = f"error: {cot_data.get('error', 'unknown')}"
        elif isinstance(cot_data, dict):
            logger.info(f"COT data: {currency} signal={cot_data.get('signal', 'UNKNOWN')}, "
                        f"confidence={cot_data.get('confidence', 0):.0%}")
            source_status["cot"] = "ok"
        else:
            source_status["cot"] = "unknown_format"

        # Get sentiment
        pair = DEFAULT_PAIRS.get(currency, f"{currency}/USD")
        # The instrument this decision is FOR (routing may have put a
        # non-forex CFD in front of the default pair) — every pooled statistic
        # below is filtered to its asset class
        suggested = (market_context or {}).get('pair') or pair
        sentiment_data = self.sentiment.get_currency_sentiment(currency)
        if isinstance(sentiment_data, dict):
            pairs_analyzed = sentiment_data.get('pairs_analyzed', 0)
            if pairs_analyzed == 0:
                logger.warning(f"Sentiment data unavailable for {currency} (0 pairs analyzed)")
                source_status["sentiment"] = "no_data"
            else:
                logger.info(f"Sentiment: {currency} signal={sentiment_data.get('signal', 'UNKNOWN')}, "
                            f"pairs={pairs_analyzed}")
                source_status["sentiment"] = "ok"
        else:
            source_status["sentiment"] = "unknown_format"

        # Get forecast info
        forecast_info = {
            "current_forecast": event.forecast,
            "previous_value": event.previous,
            "forecast_vs_previous": self._compare_values(
                event.forecast, event.previous, event.event_name)
        }
        if not event.forecast and not event.previous:
            logger.warning(f"No forecast/previous values for {event.event_name}")
            source_status["forecast"] = "no_data"
        else:
            source_status["forecast"] = "ok"

        # Historical reactions to this event (builds up over time).
        # First-time event names have no direct history — fall back to a
        # currency-level volatility/behavior prior so the model is not blind.
        reaction_summary = None
        try:
            reaction_summary = self.reaction_history.summarize(event.event_name, currency,
                                                               pair=suggested)
        except Exception as e:
            logger.debug(f"Reaction history lookup failed: {e}")
        if reaction_summary:
            source_status["reaction_history"] = "ok"
        else:
            try:
                reaction_summary = self.reaction_history.summarize_currency_fallback(
                    currency, pair=suggested)
            except Exception as e:
                logger.debug(f"Reaction currency fallback failed: {e}")
            source_status["reaction_history"] = ("currency_fallback" if reaction_summary
                                                 else "no_data")

        # The model's own recent calls on this currency, with measured outcomes
        track_record = self._build_track_record(currency)
        source_status["track_record"] = "ok" if track_record else "no_data"

        # Realized P/L of recent closed trades for this currency
        trade_outcomes = None
        try:
            trade_outcomes = self._trade_outcomes_section(currency)
        except Exception as e:
            logger.debug(f"Trade outcomes build failed: {e}")
        source_status["trade_outcomes"] = "ok" if trade_outcomes else "no_data"

        # Curated playbook for this event (optional knowledge file)
        playbook = None
        try:
            playbook = self._playbook_section(event.event_name, currency)
        except Exception as e:
            logger.debug(f"Playbook lookup failed: {e}")
        if playbook:
            source_status["playbook"] = "ok"

        # Machine-measured frequency statistics for this event/pair, plus a
        # compact recap repeated at the END of the prompt (models weigh the
        # tail of long prompts far more than the middle)
        learned_stats = learned_recap = learned_error = None
        try:
            learned = self._learned_stats_section(event.event_name, currency,
                                                  suggested)
            if learned:
                learned_stats, learned_recap = learned
        except Exception as e:
            # WARNING, not debug: a bad stats regeneration would silently
            # strip the statistical prior from EVERY decision — the audit
            # trail must distinguish "no stats exist" from "lookup broke"
            logger.warning(f"Learned stats lookup failed: {e}")
            learned_error = str(e) or e.__class__.__name__
        source_status["learned_stats"] = (
            "ok" if learned_stats
            else f"error: {learned_error}" if learned_error
            else "no_data")

        # Measured calibration of the model's own past confidence (F4) —
        # rendered only once the sample passes the n-gate inside prompt_line
        calibration_line = None
        try:
            calibration_line = self._calibration_line()
        except Exception as e:
            logger.warning(f"Calibration line build failed: {e}")
        source_status["calibration"] = "ok" if calibration_line else "no_data"

        # Episodic memory (F5): the few most similar prior releases of this
        # event, each with setup -> surprise -> path -> own past call
        episodes = None
        try:
            episodes = self._episodes_section(event, currency, suggested)
        except Exception as e:
            logger.warning(f"Episode retrieval failed: {e}")
        source_status["episodes"] = "ok" if episodes else "no_data"

        # Post-trade reflections (F5): quarantined n=1 anecdotes
        reflections = None
        try:
            reflections = self._reflections_section(event.event_name, currency)
        except Exception as e:
            logger.warning(f"Reflections lookup failed: {e}")
        source_status["reflections"] = "ok" if reflections else "no_data"

        # Non-forex instrument (event->instrument routing): the model must
        # know the unit it is quoting SL/TP in and the contract ranges for
        # THIS instrument. Absent for forex (prompt-invisibility).
        instrument = self._instrument_brief(suggested, currency)

        ctx = {
            "event": {
                "name": event.event_name,
                "currency": currency,
                "datetime": event.datetime_utc.isoformat(),
                "impact": event.impact,
                "forecast": event.forecast,
                "previous": event.previous,
            },
            "cot_analysis": cot_data,
            "sentiment_analysis": sentiment_data,
            "forecast_info": forecast_info,
            "suggested_pair": (market_context or {}).get('pair') or pair,
            "market_context": market_context,
            "reaction_history": reaction_summary,
            "track_record": track_record,
            "trade_outcomes": trade_outcomes,
            "playbook": playbook,
            "learned_stats": learned_stats,
            "learned_recap": learned_recap,
            "calibration_line": calibration_line,
            "episodes": episodes,
            "reflections": reflections,
            "_source_status": source_status,
        }
        if instrument:
            ctx["instrument"] = instrument
            source_status["instrument"] = instrument["name"]
        # Same-minute co-releases (event_cluster): only present for a cluster,
        # so a solitary event's prompt is byte-identical to before.
        if co_released:
            ctx["co_released"] = list(co_released)
            source_status["co_released"] = len(ctx["co_released"])
        return ctx

    @staticmethod
    def _instrument_brief(pair: Optional[str], event_currency: str) -> Optional[Dict]:
        """Prompt-facing facts for a NON-forex decision instrument, or None
        for forex. Stored in data_summary so replay re-renders the same
        prompt. Direction semantics are spelled out because the forex
        prompt's base/quote reasoning does not carry over by itself."""
        prof = profile_for(pair)
        if prof is None:
            return None
        cur = (event_currency or "").upper()
        if cur == prof.quote_currency:
            side = (f"{cur} is the QUOTE currency of {prof.name}: a {cur}-POSITIVE "
                    f"surprise (stronger {cur}) normally means SELL {prof.name}, a "
                    f"{cur}-negative surprise means BUY {prof.name}")
        else:
            side = (f"{cur} is not a leg of {prof.name}; reason about how the "
                    f"release transmits to {prof.name} ({prof.asset_class}) explicitly")
        return {
            "name": prof.name,
            "asset_class": prof.asset_class,
            "units": prof.units_label,
            "pip_size": prof.pip_size,
            "quote_currency": prof.quote_currency,
            "sl_range_pips": [prof.sl_range[0], prof.sl_range[1]],
            "tp_range_pips": [prof.tp_range[0], prof.tp_range[1]],
            "exit_range_minutes": [prof.exit_range[0], prof.exit_range[1]],
            "direction_semantics": side,
        }

    def _instrument_section(self, data_context: Dict) -> str:
        inst = data_context.get('instrument')
        if not inst:
            return ""
        pip = float(inst.get('pip_size') or 0)
        sl_lo, sl_hi = inst['sl_range_pips']
        tp_lo, tp_hi = inst['tp_range_pips']
        ex_lo, ex_hi = inst['exit_range_minutes']

        def _px(v):
            return f"{v * pip:g}" if pip else "?"

        return (f"\nINSTRUMENT (this decision is for a NON-forex CFD — the ranges below "
                f"OVERRIDE the forex defaults in the schema):\n"
                f"- symbol: {inst['name']} ({inst['asset_class']}), quoted in {inst['quote_currency']}\n"
                f"- units: {inst['units']}; every *_pips field you return and every pip figure "
                f"in the market data above uses this unit\n"
                f"- stop_loss_pips: {sl_lo:g}-{sl_hi:g} (= {_px(sl_lo)}-{_px(sl_hi)} price units); "
                f"take_profit_pips: {tp_lo:g}-{tp_hi:g} (= {_px(tp_lo)}-{_px(tp_hi)}); "
                f"exit_minutes: {ex_lo}-{ex_hi}\n"
                f"- direction: {inst['direction_semantics']}\n"
                f"- playbook/statistics blocks measured on FOREX pairs do not transfer 1:1 "
                f"(no 'fade the last candles' edge is established for this instrument); "
                f"prefer statistics that name {inst['name']} explicitly\n")

    def _market_context_section(self, data_context: Dict) -> str:
        """Prompt section for live market data (summary + raw candles)."""
        market = data_context.get('market_context')
        if not market:
            return "NOT AVAILABLE (no price data pushed by the EA for this event)"

        market = dict(market)
        market.pop('cross_pairs', None)  # rendered by _cross_pair_section
        candles = market.pop('candles', None)

        text = json.dumps(market, indent=2)
        age = market.get('data_age_minutes')
        if age is not None and age > 15:
            text += f"\nNOTE: this market data is {age} minutes old — treat with caution."

        if candles:
            text += "\n\nRECENT CANDLES (UTC time open/high/low/close, oldest -> newest):"
            for tf, bars in candles.items():
                text += f"\n[{tf}]\n" + "\n".join(bars)
        return text

    @staticmethod
    def _cross_pair_section(data_context: Dict) -> str:
        """CROSS-PAIR PICTURE prompt section: brief technical view of other
        fresh pairs containing the event currency."""
        market = data_context.get('market_context') or {}
        cross = market.get('cross_pairs') or []
        if not cross:
            return "No other fresh pairs available"
        return "\n".join(cross)

    def _build_track_record(self, currency: str, limit: int = 5):
        """
        Last few BUY/SELL decisions for this currency joined with the measured
        post-release reaction — lets the model see whether its recent calls
        were right and adjust instead of repeating a documented mistake.
        Realized trade P/L is appended when a closed trade matches a decision.
        """
        try:
            # forced=true rows come from FORCE_DECISION test mode — the model
            # HAD to pick a side, so they are not its genuine calls and must
            # not masquerade as live track record
            recent = [d for d in self.decision_log.get_recent(50)
                      if d.get('currency') == currency
                      and not d.get('forced')
                      and d.get('direction') in ('BUY', 'SELL')]
            if not recent:
                return None

            trades = self._trades_for_currency(currency, limit=20)

            lines = []
            for d in recent[:limit]:
                outcome = " -> outcome not measured yet"
                evt_minute = (d.get('event_datetime') or '')[:16]
                d_id = d.get('decision_id')
                for r in self.reaction_history.get_matching(d.get('event_name', ''), currency, limit=10):
                    r_id = r.get('decision_id')
                    if d_id and r_id:
                        # Exact lineage join (F2 EA echo) — immune to feed
                        # timestamp drift; minute matching stays the fallback
                        # for records from before the echo existed
                        if r_id != d_id:
                            continue
                    elif (r.get('event_time') or '')[:16] != evt_minute:
                        continue
                    move = r.get('move_5min_pips')
                    if move is None:
                        break
                    if normalize_pair(r.get('pair', '')) == normalize_pair(d.get('pair', '')):
                        correct = (move > 0) == (d.get('direction') == 'BUY')
                        outcome = (f" -> {r.get('pair')} moved {move:+.1f} pips/5min after release"
                                   f" -> your call was {'CORRECT' if correct else 'WRONG'}")
                    else:
                        outcome = f" -> {r.get('pair')} moved {move:+.1f} pips/5min after release"
                    break
                realized = self._find_trade_for_decision(d, trades)
                if realized is not None:
                    outcome += f", realized ${realized:+.2f}"
                lines.append(f"{(d.get('timestamp') or '')[:16]} {d.get('event_name')}: "
                             f"{d.get('direction')} {d.get('pair')} "
                             f"@{int((d.get('confidence') or 0) * 100)}%{outcome}")
            return "Your recent decisions for this currency:\n" + "\n".join(lines)
        except Exception as e:
            logger.debug(f"Track record build failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Realized trade outcomes (closed trades from PositionManager's JSONL)
    # ------------------------------------------------------------------

    def _load_recent_trades(self, limit: int = 50) -> List[Dict]:
        """Tail of the closed-trades JSONL. Tolerant of corrupt lines —
        same degradation contract as PositionManager._load_history."""
        path = self.trade_history_file
        if not path or not os.path.exists(path):
            return []
        records: List[Dict] = []
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
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
            logger.debug(f"Trade history read failed: {e}")
            return []
        return records[-limit:]

    def _trades_for_currency(self, currency: str, limit: int = 5) -> List[Dict]:
        """Most recent closed trades whose symbol contains the currency
        (base or quote), newest first. FORCE_DECISION test-mode trades and
        FAKE TEST dry-run trades are excluded — the prompt tells the model to
        calibrate against these outcomes, and neither a demo coin-flip nor a
        trade on a synthetic release may pose as real experience."""
        currency = (currency or '').upper()
        out = []
        for rec in reversed(self._load_recent_trades()):
            if rec.get('forced') or is_test_event_name(rec.get('event_name')):
                continue
            pair = normalize_pair(str(rec.get('symbol') or ''))
            if len(pair) >= 6 and currency in (pair[:3], pair[3:6]):
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _find_trade_for_decision(decision: Dict, trades: List[Dict]) -> Optional[float]:
        """Realized P/L of the closed trade matching a past decision.
        Exact match by decision_id when both sides carry it (post-F0 rows);
        legacy fallbacks: normalized event name + same UTC date, then same
        pair opened within 30 min of the decision."""
        d_id = decision.get('decision_id')
        if d_id:
            for t in trades:
                if t.get('decision_id') == d_id:
                    try:
                        return float(t.get('profit_usd'))
                    except (TypeError, ValueError):
                        return None

        d_event = normalize_event_name(decision.get('event_name') or '')
        d_date = (decision.get('timestamp') or '')[:10]
        d_pair = normalize_pair(decision.get('pair') or '')

        for t in trades:
            if (d_event
                    and normalize_event_name(t.get('event_name') or '') == d_event
                    and str(t.get('closed_at') or '')[:10] == d_date):
                try:
                    return float(t.get('profit_usd'))
                except (TypeError, ValueError):
                    return None

        try:
            d_time = datetime.fromisoformat(
                (decision.get('timestamp') or '').replace('Z', ''))
        except ValueError:
            return None
        for t in trades:
            if normalize_pair(str(t.get('symbol') or '')) != d_pair:
                continue
            try:
                opened = datetime.fromisoformat(
                    str(t.get('opened_at') or '').replace('Z', ''))
                if abs((opened - d_time).total_seconds()) <= 1800:
                    return float(t.get('profit_usd'))
            except (TypeError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------
    # Event playbooks (curated knowledge distilled from historical charts)
    # ------------------------------------------------------------------

    def _load_playbooks(self) -> Dict:
        """logs/event_playbooks.json, cached by mtime so panel-side edits
        are picked up without a server restart. Missing/broken file = {}."""
        path = self.playbooks_file
        try:
            if not path or not os.path.exists(path):
                return {}
            mtime = os.path.getmtime(path)
            if self._playbooks_cache is not None and mtime == self._playbooks_mtime:
                return self._playbooks_cache
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            self._playbooks_cache = data
            self._playbooks_mtime = mtime
            return data
        except Exception as e:
            logger.warning(f"Could not load event playbooks: {e}")
            return {}

    def _playbook_section(self, event_name: str, currency: str) -> Optional[str]:
        """Playbook entry for this event: exact normalized-name match first,
        then the currency-wide fallback key 'CURRENCY:<XXX>'."""
        playbooks = self._load_playbooks()
        if not playbooks:
            return None
        entry = None
        wanted = normalize_event_name(event_name)
        for key, value in playbooks.items():
            if key.upper().startswith("CURRENCY:"):
                continue
            if normalize_event_name(key) == wanted:
                entry = value
                break
        if entry is None:
            entry = playbooks.get(f"CURRENCY:{(currency or '').upper()}")
        if not isinstance(entry, dict):
            return None
        lines = []
        for field in ("pattern", "typical_behavior", "notes"):
            if entry.get(field):
                lines.append(f"{field.replace('_', ' ')}: {entry[field]}")
        return "\n".join(lines) if lines else None

    # ------------------------------------------------------------------
    # Learned event statistics (machine-built by tools/build_learned_stats.py
    # from historical + live measured post-release paths)
    # ------------------------------------------------------------------

    # Rendering gates: medians need a real sample and rates need more — a
    # 3-of-4 "75%" would anchor the model far harder than the data justifies
    STATS_MIN_N = 5
    STATS_RATE_MIN_N = 10

    def _load_learned_stats(self) -> Dict:
        """knowledge/learned_stats.json, cached by mtime so an offline
        regeneration is picked up without a restart. Missing/broken/foreign
        schema = {} (the prompt section simply disappears)."""
        path = self.learned_stats_file
        try:
            if not path or not os.path.exists(path):
                return {}
            mtime = os.path.getmtime(path)
            if self._learned_cache is not None and mtime == self._learned_mtime:
                return self._learned_cache
            # utf-8-sig: a stray Notepad/PowerShell resave on the deploy box
            # adds a BOM that plain utf-8 json.load rejects
            with open(path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            if not isinstance(data, dict) or \
                    (data.get('_meta') or {}).get('schema_version') != 1:
                logger.warning("learned_stats.json has an unexpected schema — ignoring")
                data = {}
            self._learned_cache = data
            self._learned_mtime = mtime
            return data
        except Exception as e:
            logger.warning(f"Could not load learned stats: {e}")
            return {}

    def _current_regime(self, currency: str) -> Optional[str]:
        """Live regime from the tracker when wired, else the config seed —
        same fallback contract as EventPathRecorder._regime_for."""
        if self._regime_provider is not None:
            try:
                return self._regime_provider(currency)
            except Exception as e:
                logger.debug(f"Regime provider failed for {currency}: {e}")
        import config as cfg
        return (getattr(cfg, 'CURRENCY_REGIMES', {}) or {}).get(currency)

    def _usable_pairs(self, entry) -> Dict:
        """Pair blocks of a stats entry that pass the sample-size gate."""
        if not isinstance(entry, dict):
            return {}
        return {p: b for p, b in (entry.get('pairs') or {}).items()
                if isinstance(b, dict) and b.get('n', 0) >= self.STATS_MIN_N}

    def _fmt_dist(self, dist) -> Optional[str]:
        """'median 18.2 (IQR 9.1-30.0, n=64)' — None below the gate."""
        if not isinstance(dist, dict) or dist.get('n', 0) < self.STATS_MIN_N:
            return None
        text = f"median {dist['median']:g}"
        if 'p25' in dist and 'p75' in dist:
            text += f" (IQR {dist['p25']:g}-{dist['p75']:g}, n={dist['n']})"
        else:
            text += f" (n={dist['n']})"
        return text

    def _fmt_rate(self, stat) -> Optional[str]:
        """'68% (n=40)' — None below the rate gate."""
        if not isinstance(stat, dict) or stat.get('n', 0) < self.STATS_RATE_MIN_N:
            return None
        return f"{round(stat['rate'] * 100)}% (n={stat['n']})"

    def _learned_stats_section(self, event_name: str, currency: str, pair: str):
        """(full_section_text, recap_text) for this event/pair, or None when
        nothing passes the sample gates. Lookup order: exact event key, then
        the bundle-dominant alias (e.g. 'core cpi m/m' resolves to the CPI
        release bundle whose shared path was attributed to 'cpi m/m').
        recap_text may be None when no headline stat qualifies."""
        data = self._load_learned_stats()
        events = data.get('events') or {}
        if not events:
            return None
        cur = (currency or '').upper()
        key = f"{cur}|{normalize_event_name(event_name)}"
        note = None
        entry = events.get(key)
        usable = self._usable_pairs(entry)
        if not usable:
            alias = (data.get('bundle_alias') or {}).get(key)
            entry = events.get((alias or {}).get('to'))
            usable = self._usable_pairs(entry)
            if not usable:
                return None
            note = (f"No standalone sample for this exact event — it co-released "
                    f"with {entry.get('currency')} \"{entry.get('event_name')}\" "
                    f"{alias.get('n')}x and the stats below describe that SHARED "
                    f"release path (attributed to the dominant event).")

        pair_norm = normalize_pair(pair or '')
        block = usable.get(pair_norm)
        pair_label = pair_norm
        if block is None:
            # Fall back to another pair's block only INSIDE the same asset
            # class: a forex decision must never anchor on gold's $0.10-pip
            # numbers, and a profiled instrument gets no forex substitute
            comparable = {k: v for k, v in usable.items()
                          if same_asset_class(k, pair_norm)}
            if not comparable:
                return None
            pair_label, block = max(comparable.items(), key=lambda kv: kv[1]['n'])
            note = ((note + "\n") if note else "") + \
                (f"No {pair_norm} sample for this event — stats shown for "
                 f"{pair_label} instead; scale pip magnitudes with care.")

        span = entry.get('span') or ['?', '?']
        lines = []
        if note:
            lines.append(f"NOTE: {note}")
        lines.append(f"{cur} \"{entry.get('event_name')}\" on {pair_label}: "
                     f"n={block['n']} measured releases, {span[0]}..{span[1]}:")

        moves = []
        for label, field in (("1min", "abs_move_1min"), ("5min", "abs_move_5min"),
                             ("15min", "abs_move_15min"), ("30min", "abs_move_30min")):
            txt = self._fmt_dist(block.get(field))
            if txt:
                moves.append(f"{label} {txt}")
        if moves:
            lines.append("- |move| after release: " + "; ".join(moves))

        wick = block.get('adverse_wick_5min')
        if isinstance(wick, dict) and wick.get('n', 0) >= self.STATS_MIN_N:
            wick_txt = f"median {wick['median']:g}"
            if 'p75' in wick:
                wick_txt += f", p75 {wick['p75']:g}"
            if 'p90' in wick:
                wick_txt += f", p90 {wick['p90']:g}"
            lines.append(f"- adverse wick in first 5min (excursion AGAINST the "
                         f"eventual 5-min direction): {wick_txt} pips (n={wick['n']}) "
                         f"— the stop must survive the tail of this, not the median")

        fav_parts = []
        fav_has_p80 = False
        fav_has_5min = False
        for label, stat_key in (("first 5min", "favorable_run_5min"),
                                ("first 30min", "favorable_run_30min")):
            fav = block.get(stat_key)
            if not isinstance(fav, dict) or fav.get('n', 0) < self.STATS_MIN_N:
                continue
            fav_txt = f"{label} median {fav['median']:g}"
            if 'p75' in fav:
                fav_txt += f", p75 {fav['p75']:g}"
            if 'p80' in fav:
                fav_txt += f", p80 {fav['p80']:g}"
                fav_has_p80 = True
            if stat_key == "favorable_run_5min":
                fav_has_5min = True
            fav_parts.append(fav_txt + f" (n={fav['n']})")
        if fav_parts:
            # Never direct the model at a quantile this block does not show:
            # p80 needs n>=10 while the render gate is n>=5, and 30-min-only
            # blocks must not turn the ceiling into the anchor.
            if fav_has_p80:
                guidance = ("size take_profit_pips between the median and the "
                            "p80 for your exit window, never above the p80")
            else:
                guidance = ("size take_profit_pips near the median with the "
                            "p75 as the ceiling (sample too small for a p80)")
            if not fav_has_5min:
                guidance += ("; CAUTION: only the 30-min window is measured "
                             "here and its extreme often prints near T+30 — "
                             "beyond a 5-15 min hold, treat it as a hard "
                             "ceiling, not a target")
            lines.append("- favorable run (excursion WITH the eventual "
                         "direction — what a take-profit can capture when the "
                         "direction is right): " + "; ".join(fav_parts)
                         + " pips — " + guidance)

        cont = self._fmt_rate(block.get('continuation_5to30'))
        if cont:
            lines.append(f"- 5->30min continuation (same direction still at 30min): {cont}")
        fade = self._fmt_rate(block.get('fade_pre_drift'))
        if fade:
            lines.append(f"- release moved AGAINST the last-3-min pre-release drift: {fade}")

        beat = self._fmt_rate(block.get('beat_currency_up_5min'))
        miss = self._fmt_rate(block.get('miss_currency_down_5min'))
        if beat or miss:
            parts = []
            if beat:
                parts.append(f"BEAT -> {cur} stronger: {beat}")
            if miss:
                parts.append(f"MISS -> {cur} weaker: {miss}")
            lines.append("- surprise direction within 5min (currency-strength, "
                         "base/quote already accounted for): " + "; ".join(parts))

        regime = self._current_regime(cur)
        if regime:
            regime_line = f"- current {cur} policy regime: {regime}"
            bucket = (block.get('by_regime') or {}).get(regime)
            if isinstance(bucket, dict):
                sub = []
                move5 = self._fmt_dist(bucket.get('abs_move_5min'))
                if move5:
                    sub.append(f"5min |move| {move5}")
                r_beat = self._fmt_rate(bucket.get('beat_currency_up_5min'))
                if r_beat:
                    sub.append(f"BEAT->{cur} stronger {r_beat}")
                r_miss = self._fmt_rate(bucket.get('miss_currency_down_5min'))
                if r_miss:
                    sub.append(f"MISS->{cur} weaker {r_miss}")
                if sub:
                    regime_line += (f". In past {regime}-regime releases: "
                                    + "; ".join(sub))
            lines.append(regime_line)

        sigma = entry.get('surprise_sigma')
        if sigma is not None:
            lines.append(f"- typical surprise size for this event: "
                         f"sigma(actual-forecast)={sigma:g} in the event's own "
                         f"units (n={entry.get('surprise_sigma_n')}) — small "
                         f"surprises (well under 1 sigma) historically tend to "
                         f"fade, large ones to follow through")

        if entry.get('bundled_with'):
            lines.append("- usually co-releases with: "
                         + ", ".join(entry['bundled_with'])
                         + " (the shared path is attributed to the dominant release)")

        if len(lines) <= 1 + (1 if note else 0):
            return None   # header alone (all stats below the gates) is noise

        recap_parts = []
        move5 = self._fmt_dist(block.get('abs_move_5min'))
        if move5:
            recap_parts.append(f"5-min |move| {move5}")
        # The recap has the highest recency weight in the prompt — without the
        # favorable run here, the last magnitude the model sees is the (much
        # smaller) boundary move it was told NOT to size the TP from.
        fav5 = block.get('favorable_run_5min')
        if (isinstance(fav5, dict) and fav5.get('n', 0) >= self.STATS_MIN_N
                and 'p80' in fav5):
            recap_parts.append(f"favorable-run median {fav5['median']:g}"
                               f"/p80 {fav5['p80']:g}p (TP anchor..ceiling)")
        if beat:
            recap_parts.append(f"BEAT->{cur} stronger {beat}")
        if miss:
            recap_parts.append(f"MISS->{cur} weaker {miss}")
        if fade:
            recap_parts.append(f"faded pre-drift {fade}")
        recap = None
        if recap_parts:
            recap = (f"{cur} {entry.get('event_name')} on {pair_label}: "
                     + "; ".join(recap_parts))
        return "\n".join(lines), recap

    def _current_model_signature(self) -> str:
        """The `model` string a decision would be recorded under RIGHT NOW.
        Must mirror how _llm_decision and _ensemble_decision stamp it, or the
        calibration filter below would silently match nothing."""
        panel = (ENSEMBLE_MODELS if len(ENSEMBLE_MODELS) >= 2
                 and self.provider == "openrouter" else None)
        if panel:
            return "panel:" + ",".join(panel)
        if ENSEMBLE_K >= 2:
            return f"{self.model} x{ENSEMBLE_K}"
        return str(self.model or "")

    def _calibration_line(self) -> Optional[str]:
        """Measured calibration of past directional calls vs recorded event
        paths (calibration.py). Needs the live recorder's records via
        paths_provider — absent (tests / cold start), there is no line.

        Scoped to THIS model and THIS prompt version. The line speaks in the
        second person ("you have been OVERCONFIDENT by N points — state lower
        confidence"), so feeding it another model's or another prompt's history
        tells the running configuration to correct an error it never made.
        Decision rows carry `model` and `prompt_version` for exactly this
        reason; the dashboard summary stays global and keeps its by_model
        breakdown. Cost: after a model or prompt change the line goes quiet
        until the new configuration has its own n>=50 — silence is the honest
        state, a borrowed verdict is not."""
        if self._paths_provider is None:
            return None
        paths = self._paths_provider()
        if not paths:
            return None
        from calibration import build_summary, prompt_line
        signature = self._current_model_signature()
        own = [d for d in self.decision_log.get_recent(300)
               if d.get('model') == signature
               and d.get('prompt_version') == ENTRY_PROMPT_VERSION]
        if not own:
            return None
        return prompt_line(build_summary(own, paths))

    def _episodes_section(self, event, currency: str,
                          pair: str) -> Optional[str]:
        """SIMILAR PAST EPISODES body: the few most similar prior releases
        (episodic memory) — needs the live recorder via paths_provider."""
        if self._paths_provider is None:
            return None
        paths = self._paths_provider()
        if not paths:
            return None
        from episode_retrieval import find_similar_episodes, render_episodes
        episodes = find_similar_episodes(
            paths, event.event_name, currency, pair,
            self._current_regime((currency or '').upper()),
            forecast=getattr(event, 'forecast', None),
            previous=getattr(event, 'previous', None))
        if not episodes:
            return None
        return render_episodes(episodes, self.decision_log.get_recent(300),
                               self._load_recent_trades(200))

    def _reflections_section(self, event_name: str,
                             currency: str) -> Optional[str]:
        """POST-TRADE REFLECTIONS body — quarantined n=1 anecdotes."""
        from reflections import render_for_prompt
        rows = self.reflection_store.get_matching(event_name, currency)
        return render_for_prompt(rows)

    def _trade_outcomes_section(self, currency: str) -> Optional[str]:
        """RECENT TRADE OUTCOMES prompt block: last few closed trades of this
        currency with realized P/L and close reason, plus an aggregate line."""
        trades = self._trades_for_currency(currency, limit=5)
        if not trades:
            return None

        lines = []
        wins = losses = 0
        net = 0.0
        for t in trades:
            try:
                profit = float(t.get('profit_usd') or 0.0)
            except (TypeError, ValueError):
                profit = 0.0
            net += profit
            if profit > 0:
                wins += 1
            elif profit < 0:
                losses += 1
            closed = str(t.get('closed_at') or '')[:16].replace('T', ' ')
            lines.append(f"{closed} {t.get('event_name') or '?'}: "
                         f"{t.get('direction') or '?'} {t.get('symbol') or '?'} "
                         f"{t.get('lots') if t.get('lots') is not None else '?'} lots "
                         f"-> ${profit:+.2f} ({t.get('reason') or 'unknown'})")
        lines.append(f"Aggregate: {wins} wins / {losses} losses, "
                     f"net ${net:+.2f} on {currency} trades")
        return "\n".join(lines)

    def _compare_values(self, forecast: str, previous: str,
                        event_name: str = "") -> str:
        """Compare forecast to previous value.

        For inverse indicators (Unemployment Rate, Jobless/Unemployment
        Claims — config.LOWER_IS_BETTER_MARKERS) a HIGHER number is
        currency-negative, so the label is swapped."""
        if not forecast or not previous:
            return "UNKNOWN"

        # Shared unit-aware parser (handles 210K vs 1.2M correctly)
        from event_reaction_history import parse_numeric
        forecast_num = parse_numeric(forecast)
        previous_num = parse_numeric(previous)
        if forecast_num is None or previous_num is None:
            return "UNKNOWN"

        if forecast_num > previous_num:
            result = "IMPROVEMENT"
        elif forecast_num < previous_num:
            result = "DETERIORATION"
        else:
            return "UNCHANGED"

        from config import LOWER_IS_BETTER_MARKERS
        name = (event_name or "").lower()
        if any(marker in name for marker in LOWER_IS_BETTER_MARKERS):
            result = ("DETERIORATION" if result == "IMPROVEMENT"
                      else "IMPROVEMENT")
        return result

    def _entry_prompt(self, data_context: Dict) -> str:
        """Assemble the full entry-decision user prompt. Shared by the
        single-call path and the K-call ensemble (identical prompt per
        voter — self-consistency comes from sampling, not prompt variants)."""
        # Optional curated-knowledge section — only included when a playbook
        # entry exists for this event/currency (token budget). Framed as
        # observed frequencies, NOT instructions — the model must weigh them
        # against the measured statistics instead of obeying them.
        playbook_block = ""
        if data_context.get('playbook'):
            # Cross-reference the stats section only when it is actually in
            # the prompt — pointing the model at an absent section invites
            # confusion
            stats_xref = ("; where they disagree with LEARNED EVENT STATISTICS, "
                          "trust the statistics"
                          if data_context.get('learned_stats') else "")
            playbook_block = (f"\nEVENT PLAYBOOK (curated observations from historical charts "
                              f"of this event type — small-sample frequencies, NOT rules"
                              f"{stats_xref}):\n{data_context['playbook']}\n")
        # Same-minute co-releases: the tape prices the COMBINED surprise, so
        # a decision made on the headline alone can be blindsided by a
        # contradicting sibling (CAD CPI m/m prints with Median/Trimmed/Common
        # CPI; NFP with Average Hourly Earnings and the Unemployment Rate).
        # Present only for a cluster — a solitary event's prompt is unchanged.
        co_release_block = ""
        if data_context.get('co_released'):
            co_release_block = (
                f"\nCO-RELEASED AT THE SAME MINUTE (these print SIMULTANEOUSLY with the "
                f"event above — the market reacts to the COMBINED surprise, and a "
                f"conflicting sibling can flatten or reverse the move implied by the "
                f"headline alone. You are trading the whole release, not one line of it):\n"
                f"{json.dumps(data_context['co_released'], indent=2)}\n")
        # Machine-measured base rates for this event/pair (F3 learning loop)
        learned_block = ""
        if data_context.get('learned_stats'):
            learned_block = (f"\nLEARNED EVENT STATISTICS (machine-measured from recorded "
                             f"post-release price paths 2021->today; frequencies with sample "
                             f"sizes — treat as your statistical prior):\n"
                             f"{data_context['learned_stats']}\n")
        # The same headline numbers repeated at the END of the prompt —
        # long-prompt attention favors the tail; buried mid-prompt stats
        # measurably get ignored ("lost in the middle")
        recap_block = ""
        if data_context.get('learned_recap'):
            recap_block = (f"\nKEY BASE RATES (measured; re-read and weigh these before "
                           f"committing): {data_context['learned_recap']}\n")
        # Measured calibration of the model's own stated confidence (F4);
        # sits near the prompt tail on purpose — it must influence the
        # confidence field the model is ABOUT to write
        calibration_block = ""
        if data_context.get('calibration_line'):
            calibration_block = f"\n{data_context['calibration_line']}\n"
        # Episodic memory (F5): individual similar releases, explicitly
        # subordinated to the aggregate statistics
        episodes_block = ""
        if data_context.get('episodes'):
            episodes_block = (f"\nSIMILAR PAST EPISODES (the most similar prior releases of "
                              f"this event — individual examples; the aggregate LEARNED EVENT "
                              f"STATISTICS outrank any single one):\n"
                              f"{data_context['episodes']}\n")
        # Post-trade reflections (F5): QUARANTINED n=1 journal anecdotes
        reflections_block = ""
        if data_context.get('reflections'):
            reflections_block = (f"\nPOST-TRADE REFLECTIONS (your own journal notes from single "
                                 f"past trades — QUARANTINED n=1 anecdotes, NOT rules; measured "
                                 f"statistics outrank them):\n"
                                 f"{data_context['reflections']}\n")

        prompt = f"""Analyze this upcoming economic event and make a trading decision:

EVENT DETAILS:
{json.dumps(data_context['event'], indent=2)}
{co_release_block}
COT (INSTITUTIONAL) ANALYSIS:
{json.dumps(data_context['cot_analysis'], indent=2)}

RETAIL SENTIMENT (USE AS CONTRARIAN):
{json.dumps(data_context['sentiment_analysis'], indent=2)}

FORECAST COMPARISON:
{json.dumps(data_context['forecast_info'], indent=2)}

CURRENT MARKET STRUCTURE ({data_context['suggested_pair']}):
{self._market_context_section(data_context)}

CROSS-PAIR PICTURE ({data_context['event']['currency']}) — other fresh pairs containing the event currency.
IMPORTANT: read each pair's stated '{data_context['event']['currency']}-strength direction'; do NOT assume price up = currency strong:
{self._cross_pair_section(data_context)}

HISTORICAL REACTIONS TO THIS EVENT:
{data_context.get('reaction_history') or "No history yet (the system is building this dataset)"}
{learned_block}{episodes_block}{playbook_block}
YOUR TRACK RECORD ({data_context['event']['currency']} events):
{data_context.get('track_record') or "No prior decisions for this currency yet"}

RECENT TRADE OUTCOMES ({data_context['event']['currency']}, realized P/L):
{data_context.get('trade_outcomes') or "No completed trades for this currency yet"}
{reflections_block}
SUGGESTED PAIR: {data_context['suggested_pair']}
{self._instrument_section(data_context)}{calibration_block}{recap_block}
Work through the ANALYSIS CHECKLIST against this data, then provide your trading
decision in JSON format."""
        return prompt

    def _llm_decision(self, event: EconomicEvent, data_context: Dict) -> TradingDecision:
        """Use LLM to make trading decision.

        Analyses are SERIALIZED per engine: _active_pair (read lazily by every
        panel vote in _chat to pick the per-instrument schema) is instance
        state, and /api/decision/refresh can call this engine from a Flask
        thread while the updater is mid-panel. Without the lock a second call
        would swap the pair under the first call's votes. Contention is rare
        (refresh is manual) and logged."""
        lock = self.__dict__.setdefault("_analysis_lock", threading.RLock())
        if not lock.acquire(blocking=False):
            logger.warning(f"Entry analysis for {event.event_name} waits for another "
                           f"analysis in flight (engine is single-decision at a time)")
            lock.acquire()
        try:
            self._active_pair = data_context.get('suggested_pair')
            try:
                return self._llm_decision_inner(event, data_context)
            finally:
                self._active_pair = None
        finally:
            lock.release()

    def _llm_decision_inner(self, event: EconomicEvent, data_context: Dict) -> TradingDecision:
        prompt = self._entry_prompt(data_context)

        # Self-consistency ensemble (F4) / heterogeneous panel (F4c):
        # parallel votes gate the trade. Single-call classic path below
        # stays byte-identical. The panel needs the openrouter provider —
        # one client serves every vendor there; with any other provider a
        # models-only config degrades to the classic single call instead
        # of a degenerate 1-vote committee that would SKIP everything.
        if ENSEMBLE_K >= 2 or (len(ENSEMBLE_MODELS) >= 2
                               and self.provider == "openrouter"):
            return self._ensemble_decision(event, data_context, prompt)

        try:
            logger.info(f"Calling LLM ({self.provider}/{self.model}) for trading decision...")
            response_text = self._chat(prompt)

            # Parse JSON response
            decision_data = self._parse_llm_response(response_text)

            # FORCE_DECISION: unparseable response must not silently become SKIP —
            # retry once (only if the event is far enough for a second 60s call),
            # then fall back to the rule-based engine
            if FORCE_DECISION and decision_data.get('_parse_failed'):
                if self._seconds_until(event) > 90:
                    logger.warning("LLM response unparseable — retrying once (force mode)")
                    response_text = self._chat(
                        prompt + "\n\nReturn ONLY a valid JSON object matching the schema. No other text."
                    )
                    decision_data = self._parse_llm_response(response_text)
                else:
                    logger.warning("LLM response unparseable and event too close for a retry")
                if decision_data.get('_parse_failed'):
                    logger.warning("LLM response unparseable — using rule-based fallback")
                    return self._rule_based_decision(event, data_context)

            # Whitelist chokepoint: anything that isn't exactly BUY/SELL
            # (HOLD, NO_TRADE, lowercase skip, ...) is treated as SKIP so it
            # can't slip past the server and die silently in the EA
            direction = self._normalize_direction(decision_data.get('direction'))
            reasoning = decision_data.get('reasoning', 'LLM decision')

            # FORCE_DECISION: if the model returned SKIP anyway, remap to the
            # rule-based direction but keep the model's (honest) confidence
            if FORCE_DECISION and direction == 'SKIP':
                direction, note = self._forced_direction(data_context)
                reasoning = f"{reasoning} (LLM chose SKIP; {note})"
                logger.warning(f"LLM returned SKIP despite force mode — remapped to {direction}")

            # LLM numeric fields are clamped to the ranges documented in the
            # output schema (exit 5-15, lot <=85, SL 25-80, TP 8-120) — the
            # playbook texts quote sub-5-minute historical exits and the model
            # must not be able to push an out-of-contract value to the EA.
            # The TP floor is 8, not 30: many tradeable events (NZD jobs:
            # 30-min |move| p75 = 20 pips) never travel 30 pips, and a floor
            # above the measured favorable run made every TP unreachable.
            # SL/TP of 0 mean "not set" and are passed through for EA fallback.
            sl_pips = self._num(decision_data.get('stop_loss_pips'), 0)
            tp_pips = self._num(decision_data.get('take_profit_pips'), 0)
            lim = self._limits_for(data_context.get('suggested_pair'))
            return TradingDecision(
                event=event.event_name,
                currency=event.currency,
                pair=data_context['suggested_pair'],
                direction=direction,
                confidence=self._num(decision_data.get('confidence'), 0.0, 0.0, 1.0),
                model=str(self.model or ""),
                prompt_version=ENTRY_PROMPT_VERSION,
                lot_percent=int(self._num(decision_data.get('lot_percent'), 70, 0, lim["lot_max"])),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=int(self._num(decision_data.get('exit_minutes'), 10,
                                                 lim["exit"][0], lim["exit"][1])),
                stop_loss_percent=self._num(decision_data.get('stop_loss_percent'), 40, 0, 100),
                stop_loss_pips=(sl_pips if sl_pips <= 0
                                else min(max(sl_pips, lim["sl"][0]), lim["sl"][1])),
                take_profit_pips=(tp_pips if tp_pips <= 0
                                  else min(max(tp_pips, lim["tp"][0]), lim["tp"][1])),
                reasoning=reasoning,
                data_summary=data_context,
                timestamp=utcnow(),
                forced=FORCE_DECISION,
                raw_response=response_text or ""
            )

        except Exception as e:
            logger.error(f"LLM decision error: {e}")
            return self._rule_based_decision(event, data_context)

    def _ensemble_decision(self, event: EconomicEvent, data_context: Dict,
                           prompt: str) -> TradingDecision:
        """K-call self-consistency (F4): fire K parallel identical calls and
        gate the trade on vote agreement instead of verbal confidence.

        Normal mode: ALL valid votes BUY (or all SELL) with at least 2 valid
        votes = trade; any split, SKIP votes, or a lone survivor = SKIP.
        FORCE_DECISION demo: SKIP is unavailable — the majority direction
        wins and agreement scales the reported confidence.
        Numeric fields of a traded decision are per-voter clamped medians;
        all raw replies go to the decision-context dump."""
        from concurrent.futures import ThreadPoolExecutor, wait
        panel = (ENSEMBLE_MODELS if len(ENSEMBLE_MODELS) >= 2
                 and self.provider == "openrouter" else None)
        k = len(panel) if panel else ENSEMBLE_K
        if panel:
            logger.info(f"Ensemble entry: mixed panel of {k} models, "
                        f"anchor {panel[0]} ({', '.join(panel)})...")
        else:
            logger.info(f"Ensemble entry: {k} parallel LLM calls "
                        f"({self.provider}/{self.model})...")

        def one_call(i):
            """One panel vote. Failures are RETURNED, never raised, so one
            flaky vendor cannot take the whole committee down — but every
            drop is now logged with the model name and the reason, and is
            retried once when the release is still far enough away.

            Both failure modes matter and used to be indistinguishable: an
            exception (timeout/429) was logged, while an HTTP 200 carrying
            unusable content — a reasoning model burning max_tokens before
            emitting any JSON — was dropped with NO trace at all. That is
            how a 3-model panel silently became a 2-vote coin flip."""
            call_model = panel[i] if panel else None
            label = call_model or self.model
            reason = ""
            text = ""
            for attempt in (1, 2):
                try:
                    text = (self._chat(prompt, call_model) if call_model
                            else self._chat(prompt))
                except Exception as e:
                    text, reason = "", f"call raised: {e}"
                else:
                    parsed = self._parse_llm_response(text)
                    if not parsed.get('_parse_failed'):
                        if attempt == 2:
                            logger.info(f"Ensemble vote {label}: recovered on retry")
                        return text, parsed, label, ""
                    reason = (f"unparseable reply ({len(text or '')} chars): "
                              f"{(text or '')[:200]!r}")
                # A retry is another full call — only when a 60s call still
                # fits before the release, same margin as the single-call path
                if attempt == 1 and self._seconds_until(event) > 90:
                    logger.warning(f"Ensemble vote {label} failed ({reason}) "
                                   f"— retrying once")
                    continue
                logger.warning(f"Ensemble vote DROPPED — {label}: {reason}")
                return text or "", {"_parse_failed": True}, label, reason

        # WALL-CLOCK DEADLINE. pool.map waited for the SLOWEST call, so a single
        # hung vendor could push the whole decision past the release: the signal
        # endpoint then answered "event has already passed" and the event was
        # silently skipped even though the other models had replied in seconds.
        # Aggregate whatever arrived in time instead of missing the trade.
        deadline = max(self._seconds_until(event) - ENSEMBLE_DEADLINE_MARGIN,
                       ENSEMBLE_MIN_WAIT_SECONDS)
        pool = ThreadPoolExecutor(max_workers=k)
        try:
            futures = [pool.submit(one_call, i) for i in range(k)]
            wait(futures, timeout=deadline)
            results = []
            for i, f in enumerate(futures):
                if not f.done():
                    label = panel[i] if panel else self.model
                    logger.warning(
                        f"Ensemble vote ABANDONED — {label}: still running "
                        f"after the {deadline:.0f}s deadline"
                    )
                    results.append(("", {"_parse_failed": True}, label,
                                    f"no reply within {deadline:.0f}s deadline"))
                    continue
                try:
                    results.append(f.result())
                except Exception as e:
                    label = panel[i] if panel else self.model
                    logger.warning(f"Ensemble vote DROPPED — {label}: {e}")
                    results.append(("", {"_parse_failed": True}, label, str(e)))
        except Exception as e:
            logger.error(f"Ensemble execution failed: {e}")
            return self._rule_based_decision(event, data_context)
        finally:
            # Never block on an abandoned call: cancel what has not started and
            # let a running one die on its own HTTP timeout.
            pool.shutdown(wait=False, cancel_futures=True)

        votes, raws, failures = [], [], []
        for text, parsed, vote_model, fail_reason in results:
            # Label raw replies per model in panel mode — the context dump
            # is unreadable otherwise (three vendors, one blob)
            raws.append((f"[{vote_model}]\n" if panel else "")
                        + (text or "(call failed)"))
            if parsed.get('_parse_failed'):
                failures.append({"model": vote_model,
                                 "reason": fail_reason or "unknown"})
                continue
            votes.append({
                "direction": self._normalize_direction(parsed.get('direction')),
                "confidence": self._num(parsed.get('confidence'), 0.0, 0.0, 1.0),
                "model": vote_model,
                "parsed": parsed,
            })
        raw_joined = "\n\n=== ENSEMBLE CALL BOUNDARY ===\n\n".join(raws)
        if not votes:
            logger.warning("Ensemble: no parseable vote — rule-based fallback")
            return self._rule_based_decision(event, data_context)
        # A vote that never arrived is NOT an abstention: the committee is
        # smaller than configured and every downstream reader must be able
        # to see that. `degraded` rides the decision into the dashboard, the
        # context dump and the reasoning string.
        degraded = bool(failures)
        degraded_note = ""
        if degraded:
            lost = ", ".join(f["model"] for f in failures)
            degraded_note = (f"PANEL DEGRADED {len(votes)}/{k} "
                             f"(no vote from {lost}). ")
            logger.warning(f"Ensemble panel degraded to {len(votes)}/{k} "
                           f"valid votes — missing: {lost}")

        dirs = [v["direction"] for v in votes]
        counts = {d: dirs.count(d) for d in sorted(set(dirs))}
        meta = {"k": k, "valid": len(votes),
                "votes": [{"direction": v["direction"],
                           "confidence": round(v["confidence"], 2),
                           "model": v["model"]}
                          for v in votes]}
        if panel:
            meta["panel"] = list(panel)
            meta["anchor"] = panel[0]
        if degraded:
            meta["degraded"] = True
            meta["failures"] = failures

        def _median(vals):
            vals = sorted(vals)
            mid = len(vals) // 2
            return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

        def _median_pips(vals):
            """Median honoring the 0 = "not set, EA fallback" SENTINEL: 0 is
            not a magnitude, and averaging it with real proposals would
            invent a tighter stop than ANY voter wanted (e.g. [0, 40] ->
            20 -> clamped 25 on a news release). Zeros win on ties —
            conservative, the EA has its own fallback logic."""
            real = [v for v in vals if v > 0]
            if len(vals) - len(real) >= len(real):
                return 0.0
            return _median(real)

        def build(direction, confidence, votes_for, reasoning):
            """TradingDecision from the agreeing voters' medians (SL/TP via
            sentinel-aware median), re-clamped to the same contract ranges
            as the single-call path before reaching the EA."""
            src = votes_for or votes
            sl = _median_pips([self._num(v["parsed"].get('stop_loss_pips'), 0)
                               for v in src])
            tp = _median_pips([self._num(v["parsed"].get('take_profit_pips'), 0)
                               for v in src])
            lim = self._limits_for(data_context.get('suggested_pair'))
            return TradingDecision(
                event=event.event_name,
                currency=event.currency,
                pair=data_context['suggested_pair'],
                direction=direction,
                confidence=round(min(max(confidence, 0.0), 1.0), 2),
                model=("panel:" + ",".join(panel) if panel
                       else f"{self.model} x{k}"),
                prompt_version=ENTRY_PROMPT_VERSION,
                lot_percent=int(_median([self._num(v["parsed"].get('lot_percent'),
                                                   70, 0, lim["lot_max"]) for v in src])),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=int(_median([self._num(v["parsed"].get('exit_minutes'),
                                                          10, lim["exit"][0], lim["exit"][1])
                                                for v in src])),
                stop_loss_percent=_median([self._num(v["parsed"].get('stop_loss_percent'),
                                                     40, 0, 100) for v in src]),
                stop_loss_pips=sl if sl <= 0 else min(max(sl, lim["sl"][0]), lim["sl"][1]),
                take_profit_pips=tp if tp <= 0 else min(max(tp, lim["tp"][0]), lim["tp"][1]),
                reasoning=reasoning,
                data_summary=data_context,
                timestamp=utcnow(),
                forced=FORCE_DECISION,
                raw_response=raw_joined,
                ensemble=meta,
            )

        def representative_reasoning(votes_for):
            """The reasoning of the median-confidence agreeing voter — one
            honest sample instead of a mashup of K essays. `or`-fallback:
            a JSON null reasoning KEY EXISTS, so .get's default alone would
            return None and crash the string concat below."""
            ranked = sorted(votes_for, key=lambda v: v["confidence"])
            text = ranked[len(ranked) // 2]["parsed"].get('reasoning')
            return str(text) if text else 'LLM decision'

        # Aggregation must NEVER kill the event's decision — any surprise in
        # the vote data degrades to the rule engine, mirroring the
        # single-call path's outer try
        try:
            if FORCE_DECISION:
                buysell = [v for v in votes if v["direction"] in ('BUY', 'SELL')]
                n_buy = sum(1 for v in buysell if v["direction"] == 'BUY')
                n_sell = len(buysell) - n_buy
                if buysell and n_buy != n_sell:
                    top = 'BUY' if n_buy > n_sell else 'SELL'
                    votes_for = [v for v in buysell if v["direction"] == top]
                    agreement = len(votes_for) / len(votes)
                    confidence = (sum(v["confidence"] for v in votes_for)
                                  / len(votes_for)) * agreement
                    # QUORUM: one surviving vote is not a "majority" and its
                    # agreement is not 1.00 — that label made a panel of one
                    # look like a panel of three. Force mode may never SKIP
                    # (documented invariant), so the trade still goes out; it
                    # is just described as the single opinion it really is.
                    if len(votes) < ENSEMBLE_MIN_QUORUM:
                        what = (f"NO QUORUM {len(votes)}/{k} — {top} is one "
                                f"model's opinion, not a panel decision")
                        logger.warning(f"Ensemble force-mode below quorum: "
                                       f"{len(votes)}/{k} valid -> {top}")
                    else:
                        what = (f"votes {counts} -> majority {top}, "
                                f"agreement {agreement:.2f}")
                        logger.info(f"Ensemble force-mode majority: "
                                    f"{counts} -> {top}")
                    reasoning = (degraded_note
                                 + f"ENSEMBLE (force mode) {what}. "
                                 + representative_reasoning(votes_for))
                    return build(top, confidence, votes_for, reasoning)
                # All-SKIP or a dead BUY/SELL tie: same resolver as the
                # single-call force path (rule scores, not alphabet)
                direction, note = self._forced_direction(data_context)
                what = ("tie" if buysell
                        else f"all {len(votes)} votes SKIP")
                reasoning = (degraded_note
                             + f"ENSEMBLE (force mode): {what} ({counts}); {note}")
                return build(direction, min(v["confidence"] for v in votes),
                             votes, reasoning)

            if panel:
                # Heterogeneous committee (F4c): the ANCHOR's call gates the
                # trade. Confirmation = >=1 other model votes the same
                # direction; an OPPOSITE BUY/SELL vote vetoes; SKIP votes
                # abstain. Confidence and reasoning come from the anchor —
                # confidence scales are not comparable across vendors, so
                # they are never mixed or averaged.
                anchor = next((v for v in votes if v["model"] == panel[0]), None)
                if anchor and anchor["direction"] in ('BUY', 'SELL'):
                    d = anchor["direction"]
                    others = [v for v in votes if v is not anchor]
                    confirms = [v for v in others if v["direction"] == d]
                    opposed = [v for v in others
                               if v["direction"] in ('BUY', 'SELL')
                               and v["direction"] != d]
                    if confirms and not opposed:
                        votes_for = [anchor] + confirms
                        anchor_text = anchor["parsed"].get('reasoning')
                        # degraded_note matters most HERE: a model that never
                        # answered cannot veto, so "no veto" is only as strong
                        # as the number of models that actually voted.
                        reasoning = (degraded_note
                                     + f"ENSEMBLE panel {len(votes_for)}/{k} {d} "
                                     f"(anchor + {len(confirms)} confirm, no veto). "
                                     + (str(anchor_text) if anchor_text
                                        else 'LLM decision'))
                        logger.info(f"Ensemble panel: {d} — anchor confirmed "
                                    f"by {[v['model'] for v in confirms]}")
                        return build(d, anchor["confidence"], votes_for, reasoning)
                    if opposed:
                        why = (f"veto — {', '.join(v['model'] for v in opposed)} "
                               f"voted against anchor {d}")
                    else:
                        why = f"anchor {d} unconfirmed by any other model"
                elif anchor:
                    why = "anchor voted SKIP"
                else:
                    why = f"anchor {panel[0]} call failed"
                logger.info(f"Ensemble panel SKIP: {why} ({counts})")
                return build('SKIP',
                             anchor["confidence"] if anchor
                             else _median([v["confidence"] for v in votes]),
                             votes,
                             degraded_note + f"ENSEMBLE SKIP: {why} ({counts}).")

            unanimous = (len(votes) >= ENSEMBLE_MIN_QUORUM and len(counts) == 1
                         and dirs[0] in ('BUY', 'SELL'))
            if unanimous:
                confidence = _median([v["confidence"] for v in votes])
                reasoning = (degraded_note
                             + f"ENSEMBLE {len(votes)}/{k} unanimous {dirs[0]} "
                             f"(agreement gate passed). "
                             + representative_reasoning(votes))
                logger.info(f"Ensemble unanimous: {len(votes)}/{k} {dirs[0]}")
                return build(dirs[0], confidence, votes, reasoning)

            # Split, SKIP votes, or a single surviving vote — the agreement
            # gate failed; the disagreement itself is the signal
            if len(votes) == 1:
                why = f"only 1/{k} calls returned a valid vote — no consensus possible"
            elif len(counts) == 1:
                why = f"all {len(votes)} votes SKIP"
            else:
                why = f"votes split {counts} — no unanimity"
            logger.info(f"Ensemble SKIP: {why}")
            return build('SKIP', _median([v["confidence"] for v in votes]),
                         votes, degraded_note + f"ENSEMBLE SKIP: {why}.")
        except Exception as e:
            logger.error(f"Ensemble aggregation error: {e}")
            return self._rule_based_decision(event, data_context)

    def _limits_for(self, pair: Optional[str]) -> Dict:
        """Numeric contract ranges for the instrument the decision is for.

        Forex pairs (no instrument profile) get the class defaults — the exact
        literals the clamps used before profiles existed. A non-forex CFD
        (XAUUSD, GER40, ...) gets the ranges declared in instrument_profiles so
        a gold stop of "$8" (80 pips at 1 pip = $0.10) is not squeezed into
        the forex 25-80 window.
        """
        prof = profile_for(pair)
        if prof is None:
            return {"lot_max": self.LOT_MAX, "exit": self.EXIT_RANGE,
                    "sl": self.SL_RANGE, "tp": self.TP_RANGE,
                    "units": "pips"}
        return {"lot_max": int(prof.lot_max), "exit": tuple(prof.exit_range),
                "sl": tuple(prof.sl_range), "tp": tuple(prof.tp_range),
                "units": prof.units_label}

    @staticmethod
    def _num(value, default, lo=None, hi=None):
        """Coerce an LLM-returned numeric field to float and clamp it to the
        documented schema range. Non-numeric junk (None, "1-2", "fast") falls
        back to the default so a malformed model reply can't reach the EA."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _chat(self, prompt: str, model: str = None) -> str:
        """Send one prompt to the configured LLM provider and return the raw
        text. `model` overrides the engine's default for THIS call only —
        used by the F4c mixed panel, which routes each vote to a different
        OpenRouter id through the same client."""
        # The instrument of the decision in flight (set by _llm_decision) picks
        # the schema ranges; None -> forex text. Kept as instance state so the
        # many `_chat(prompt)` call sites and test doubles keep their signature.
        system_prompt = self._system_prompt(getattr(self, "_active_pair", None))
        use_model = model or self.model
        if self.provider == "openrouter":
            # OpenRouter uses OpenAI-compatible API
            response = self.client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                temperature=0.3,
                timeout=60.0,  # 60 second timeout
                extra_headers=openrouter_headers("entry",
                                                 "SkyTower-AI Trading"),
                extra_body=reasoning_body(
                    LLM_CONFIG.get("reasoning_effort")),
            )
            logger.info(f"OpenRouter response received from {use_model}")
            return response.choices[0].message.content

        elif self.provider == "anthropic":
            response = self.client.messages.create(
                model=use_model,
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                timeout=60.0  # 60 second timeout
            )
            return response.content[0].text

        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                temperature=0.3,
                timeout=60.0  # 60 second timeout
            )
            return response.choices[0].message.content

        raise RuntimeError(f"No LLM client for provider '{self.provider}'")

    @staticmethod
    def _currency_bias_to_direction(bias: str, pair: str, currency: str) -> str:
        """
        Map an event-currency bias (BULLISH/BEARISH) to a BUY/SELL of the pair,
        respecting the quoting. The event currency is NOT always the base:
        DEFAULT_PAIRS maps CAD -> USD/CAD, where CAD is the QUOTE — there a
        bearish CAD means BUY USDCAD, not SELL.
        """
        currency_is_base = normalize_pair(pair).startswith(currency.upper())
        if bias == "BULLISH":
            return "BUY" if currency_is_base else "SELL"
        return "SELL" if currency_is_base else "BUY"

    @staticmethod
    def _normalize_direction(raw) -> str:
        """Whitelist LLM direction output: anything but BUY/SELL becomes SKIP."""
        direction = str(raw or "").strip().upper()
        if direction in ("BUY", "SELL"):
            return direction
        if direction != "SKIP":
            logger.warning(f"LLM returned unexpected direction '{raw}' — treating as SKIP")
        return "SKIP"

    @staticmethod
    def _seconds_until(event) -> float:
        """Seconds until the event (UTC), or a large number when unknown."""
        try:
            event_time = event.datetime_utc
            if event_time.tzinfo is not None:
                event_time = event_time.replace(tzinfo=None)
            return (event_time - utcnow()).total_seconds()
        except (AttributeError, TypeError):
            return float('inf')

    def _score_direction(self, data_context: Dict) -> tuple:
        """
        Single scoring table shared by the rule-based engine and the forced
        tie-break: forecast +2, COT +3, contrarian sentiment +2.
        Returns (bullish, bearish, reasons, confidence_boost).
        """
        bullish, bearish = 0, 0
        reasons = []
        confidence_boost = 0.0

        forecast_cmp = data_context.get('forecast_info', {}).get('forecast_vs_previous', 'UNKNOWN')
        if forecast_cmp == "IMPROVEMENT":
            bullish += 2
            reasons.append("Forecast better than previous")
        elif forecast_cmp == "DETERIORATION":
            bearish += 2
            reasons.append("Forecast worse than previous")

        cot = data_context.get('cot_analysis') or {}
        if isinstance(cot, dict):
            if cot.get('signal') == "BULLISH":
                bullish += 3
                confidence_boost += cot.get('confidence', 0) * 0.2
                reasons.append(f"COT: Institutions bullish ({cot.get('reasoning', '')})")
            elif cot.get('signal') == "BEARISH":
                bearish += 3
                confidence_boost += cot.get('confidence', 0) * 0.2
                reasons.append(f"COT: Institutions bearish ({cot.get('reasoning', '')})")

        sentiment = data_context.get('sentiment_analysis') or {}
        if isinstance(sentiment, dict):
            if sentiment.get('signal') == "BULLISH":  # already contrarian
                bullish += 2
                confidence_boost += sentiment.get('confidence', 0) * 0.15
                reasons.append("Retail heavily short (contrarian bullish)")
            elif sentiment.get('signal') == "BEARISH":
                bearish += 2
                confidence_boost += sentiment.get('confidence', 0) * 0.15
                reasons.append("Retail heavily long (contrarian bearish)")

        return bullish, bearish, reasons, confidence_boost

    def _forced_direction(self, data_context: Dict, pair: str = None) -> tuple:
        """
        Pick BUY/SELL from the shared rule scores when SKIP is not allowed
        (FORCE_DECISION test mode). Scores describe the EVENT CURRENCY; the
        pair direction is derived via _currency_bias_to_direction so a
        quote-side pair (CAD -> USDCAD) does not invert the trade.
        Tie-break: forecast comparison, then bullish.
        """
        bullish, bearish, _, _ = self._score_direction(data_context)
        forecast_cmp = data_context.get('forecast_info', {}).get('forecast_vs_previous', 'UNKNOWN')
        currency = data_context.get('event', {}).get('currency', '')
        pair = pair or data_context.get('suggested_pair', '')

        if bullish > bearish:
            bias, note = "BULLISH", f"direction forced from rule fallback (bullish {bullish} vs bearish {bearish})"
        elif bearish > bullish:
            bias, note = "BEARISH", f"direction forced from rule fallback (bearish {bearish} vs bullish {bullish})"
        elif forecast_cmp == "DETERIORATION":
            bias, note = "BEARISH", "forced tie-break via forecast deterioration"
        else:
            bias, note = "BULLISH", "forced tie-break (no directional signal)"

        direction = self._currency_bias_to_direction(bias, pair, currency)
        return direction, f"{note}; {currency} {bias.lower()} -> {direction} {normalize_pair(pair)}"

    def _parse_llm_response(self, response: str) -> Dict:
        """Parse JSON from LLM response. Salvage order: whole text as JSON,
        then the outermost {...} slice (survives braces INSIDE the reasoning
        string, which the flat regex below cannot), then the legacy regex."""
        response = response or ""
        for candidate in (response.strip(),
                          response[response.find('{'):response.rfind('}') + 1]
                          if '{' in response and '}' in response else ''):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
        try:
            import re
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception:
            pass

        # Default response if parsing fails (_parse_failed lets callers
        # distinguish a broken response from a genuine SKIP)
        return {
            "direction": "SKIP",
            "confidence": 0.0,
            "reasoning": "Could not parse LLM response",
            "_parse_failed": True
        }

    def _rule_based_decision(self, event: EconomicEvent, data_context: Dict) -> TradingDecision:
        """
        Rule-based decision making when LLM is not available
        Uses the SkyTower-FX strategy rules
        """
        currency = event.currency.upper()
        pair = data_context['suggested_pair']

        bullish_score, bearish_score, reasons, confidence_boost = self._score_direction(data_context)
        confidence = 0.5 + confidence_boost

        # Determine direction: scores describe the EVENT CURRENCY; map the
        # bias onto the pair (base vs quote) instead of assuming bullish=BUY
        if bullish_score > bearish_score + 2:
            direction = self._currency_bias_to_direction("BULLISH", pair, currency)
            reasons.append(f"{currency} bullish -> {direction} {normalize_pair(pair)}")
        elif bearish_score > bullish_score + 2:
            direction = self._currency_bias_to_direction("BEARISH", pair, currency)
            reasons.append(f"{currency} bearish -> {direction} {normalize_pair(pair)}")
        elif FORCE_DECISION:
            # Test mode: no margin requirement — pick the stronger side
            direction, note = self._forced_direction(data_context, pair)
            reasons.append(note)
            if bullish_score == 0 and bearish_score == 0:
                # Evidence-free coin flip: report it honestly instead of
                # shipping the 0.5 baseline as if it meant something
                confidence = 0.3
        else:
            direction = "SKIP"
            confidence = 0.3

        # Adjust lot based on confidence
        lot_percent = 80 if confidence > 0.7 else 70 if confidence > 0.5 else 60

        return TradingDecision(
            event=event.event_name,
            currency=currency,
            pair=pair,
            direction=direction,
            confidence=min(confidence, 1.0),
            model="rule-based",
            prompt_version="",
            lot_percent=lot_percent,
            entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
            exit_minutes_after=10 if confidence > 0.6 else 5,
            stop_loss_percent=40,
            reasoning="; ".join(reasons) if reasons else "No strong signals",
            data_summary=data_context,
            timestamp=utcnow(),
            forced=FORCE_DECISION
        )

    def get_next_trade_recommendation(self) -> Optional[TradingDecision]:
        """
        Get recommendation for the next tradeable event

        Returns:
            TradingDecision for next event or None
        """
        # Find next high-impact event
        event = self.calendar.get_next_tradeable_event(
            # None = calendar_fetcher reads cfg.HIGH_IMPACT_EVENTS at call
            # time, so panel edits to the tier lists take effect without a
            # restart (a module-level import here would pin the old list).
            event_keywords=None,
            currencies=["NZD", "CAD", "AUD", "USD", "GBP"]
        )

        if not event:
            logger.info("No upcoming tradeable events found")
            return None

        logger.info(f"Analyzing upcoming event: {event.event_name} ({event.currency})")
        return self.analyze_event(event, co_released=self._co_released_for(event))

    def _co_released_for(self, event) -> List[Dict]:
        """Other prints of this event's release minute, read from the calendar
        cache (never a fetch — this runs inside request handlers). Best-effort:
        the co-release block is context, never a precondition for deciding."""
        try:
            return co_release_brief(event, self.calendar.peek_cached_events())
        except Exception as e:
            logger.debug(f"Co-release lookup failed: {e}")
            return []
