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

# Import our data modules
from calendar_fetcher import CalendarAggregator, EconomicEvent
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from event_reaction_history import EventReactionHistory, normalize_event_name
from decision_history import DecisionHistory
from market_context import normalize_pair
from config import (LLM_CONFIG, TRADING_CONFIG, DEFAULT_PAIRS,
                    HIGH_IMPACT_EVENTS, OPENROUTER_API_KEY, FORCE_DECISION,
                    ENSEMBLE_K)


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
   where the stops likely sit relative to nearest support/resistance.
d. Volatility fit: are your stop_loss_pips/take_profit_pips consistent with ATR and
   the current spread (a stop inside 1x spread+ATR noise will be swept)? Entry happens
   seconds BEFORE the release, so also budget the stop for an adverse stop-run wick
   in the release seconds — the EVENT PLAYBOOK gives measured wick sizes for many
   events; if two estimates could apply, budget the LARGER. A stop tighter than
   wick+spread dies at the print even when the direction is right.
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
    "stop_loss_pips": 25 to 80 (wider for JPY pairs: 40-80),
    "take_profit_pips": 30 to 120 (1.5x to 2x of SL),
    "exit_minutes": 5 to 15,
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

    def _system_prompt(self) -> str:
        """Build the system prompt for the current mode (normal vs FORCE_DECISION)."""
        if FORCE_DECISION:
            return (self.SYSTEM_PROMPT
                    .replace("%DIRECTION_VALUES%", '"BUY" or "SELL"')
                    .replace("%SKIP_POLICY%", self.SKIP_POLICY_FORCED))
        return (self.SYSTEM_PROMPT
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
                            LEARNED_STATS_FILE)
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
                    base_url="https://openrouter.ai/api/v1"
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

    def analyze_event(self, event: EconomicEvent, market_context: Dict = None) -> TradingDecision:
        """
        Analyze an upcoming economic event and make a trading decision

        Args:
            event: The economic event to analyze
            market_context: Optional dict from market_context.build_market_context()
                (trend, ATR, S/R zones from EA-pushed OHLC). None when no EA
                has pushed data for this event yet.

        Returns:
            TradingDecision object
        """
        # Gather all data
        data_context = self._gather_data(event, market_context)

        # Make decision (LLM or rule-based)
        if self.client:
            logger.info(f"Using LLM ({self.provider}) for decision on {event.event_name}")
            decision = self._llm_decision(event, data_context)
        else:
            logger.info(f"Using rule-based decision for {event.event_name}")
            decision = self._rule_based_decision(event, data_context)

        return decision

    def _gather_data(self, event: EconomicEvent, market_context: Dict = None) -> Dict:
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
            "forecast_vs_previous": self._compare_values(event.forecast, event.previous)
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
            reaction_summary = self.reaction_history.summarize(event.event_name, currency)
        except Exception as e:
            logger.debug(f"Reaction history lookup failed: {e}")
        if reaction_summary:
            source_status["reaction_history"] = "ok"
        else:
            try:
                reaction_summary = self.reaction_history.summarize_currency_fallback(currency)
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
        suggested = (market_context or {}).get('pair') or pair
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

        return {
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
            "_source_status": source_status,
        }

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
        (base or quote), newest first. FORCE_DECISION test-mode trades are
        excluded — the prompt tells the model to calibrate against these
        outcomes, and demo coin-flips must not pose as real experience."""
        currency = (currency or '').upper()
        out = []
        for rec in reversed(self._load_recent_trades()):
            if rec.get('forced'):
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
            pair_label, block = max(usable.items(), key=lambda kv: kv[1]['n'])
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

    def _calibration_line(self) -> Optional[str]:
        """Measured calibration of past directional calls vs recorded event
        paths (calibration.py). Needs the live recorder's records via
        paths_provider — absent (tests / cold start), there is no line."""
        if self._paths_provider is None:
            return None
        paths = self._paths_provider()
        if not paths:
            return None
        from calibration import build_summary, prompt_line
        decisions = self.decision_log.get_recent(300)
        return prompt_line(build_summary(decisions, paths))

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

    def _compare_values(self, forecast: str, previous: str) -> str:
        """Compare forecast to previous value"""
        if not forecast or not previous:
            return "UNKNOWN"

        # Shared unit-aware parser (handles 210K vs 1.2M correctly)
        from event_reaction_history import parse_numeric
        forecast_num = parse_numeric(forecast)
        previous_num = parse_numeric(previous)
        if forecast_num is None or previous_num is None:
            return "UNKNOWN"

        if forecast_num > previous_num:
            return "IMPROVEMENT"
        elif forecast_num < previous_num:
            return "DETERIORATION"
        return "UNCHANGED"

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

        prompt = f"""Analyze this upcoming economic event and make a trading decision:

EVENT DETAILS:
{json.dumps(data_context['event'], indent=2)}

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
{learned_block}{playbook_block}
YOUR TRACK RECORD ({data_context['event']['currency']} events):
{data_context.get('track_record') or "No prior decisions for this currency yet"}

RECENT TRADE OUTCOMES ({data_context['event']['currency']}, realized P/L):
{data_context.get('trade_outcomes') or "No completed trades for this currency yet"}

SUGGESTED PAIR: {data_context['suggested_pair']}
{calibration_block}{recap_block}
Work through the ANALYSIS CHECKLIST against this data, then provide your trading
decision in JSON format."""
        return prompt

    def _llm_decision(self, event: EconomicEvent, data_context: Dict) -> TradingDecision:
        """Use LLM to make trading decision"""
        prompt = self._entry_prompt(data_context)

        # Self-consistency ensemble (F4): K parallel votes, unanimity gates
        # the trade. Single-call classic path below stays byte-identical.
        if ENSEMBLE_K >= 2:
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
            # output schema (exit 5-15, lot <=85, SL 25-80, TP 30-120) — the
            # playbook texts quote sub-5-minute historical exits and the model
            # must not be able to push an out-of-contract value to the EA.
            # SL/TP of 0 mean "not set" and are passed through for EA fallback.
            sl_pips = self._num(decision_data.get('stop_loss_pips'), 0)
            tp_pips = self._num(decision_data.get('take_profit_pips'), 0)
            return TradingDecision(
                event=event.event_name,
                currency=event.currency,
                pair=data_context['suggested_pair'],
                direction=direction,
                confidence=self._num(decision_data.get('confidence'), 0.0, 0.0, 1.0),
                lot_percent=int(self._num(decision_data.get('lot_percent'), 70, 0, 85)),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=int(self._num(decision_data.get('exit_minutes'), 10, 5, 15)),
                stop_loss_percent=self._num(decision_data.get('stop_loss_percent'), 40, 0, 100),
                stop_loss_pips=sl_pips if sl_pips <= 0 else min(max(sl_pips, 25.0), 80.0),
                take_profit_pips=tp_pips if tp_pips <= 0 else min(max(tp_pips, 30.0), 120.0),
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
        from concurrent.futures import ThreadPoolExecutor
        k = ENSEMBLE_K
        logger.info(f"Ensemble entry: {k} parallel LLM calls "
                    f"({self.provider}/{self.model})...")

        def one_call(i):
            try:
                text = self._chat(prompt)
                return text, self._parse_llm_response(text)
            except Exception as e:
                logger.warning(f"Ensemble call {i + 1}/{k} failed: {e}")
                return "", {"_parse_failed": True}

        try:
            with ThreadPoolExecutor(max_workers=k) as pool:
                results = list(pool.map(one_call, range(k)))
        except Exception as e:
            logger.error(f"Ensemble execution failed: {e}")
            return self._rule_based_decision(event, data_context)

        votes, raws = [], []
        for text, parsed in results:
            raws.append(text or "(call failed)")
            if parsed.get('_parse_failed'):
                continue
            votes.append({
                "direction": self._normalize_direction(parsed.get('direction')),
                "confidence": self._num(parsed.get('confidence'), 0.0, 0.0, 1.0),
                "parsed": parsed,
            })
        raw_joined = "\n\n=== ENSEMBLE CALL BOUNDARY ===\n\n".join(raws)
        if not votes:
            logger.warning("Ensemble: no parseable vote — rule-based fallback")
            return self._rule_based_decision(event, data_context)

        dirs = [v["direction"] for v in votes]
        counts = {d: dirs.count(d) for d in sorted(set(dirs))}
        meta = {"k": k, "valid": len(votes),
                "votes": [{"direction": v["direction"],
                           "confidence": round(v["confidence"], 2)}
                          for v in votes]}

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
            return TradingDecision(
                event=event.event_name,
                currency=event.currency,
                pair=data_context['suggested_pair'],
                direction=direction,
                confidence=round(min(max(confidence, 0.0), 1.0), 2),
                lot_percent=int(_median([self._num(v["parsed"].get('lot_percent'),
                                                   70, 0, 85) for v in src])),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=int(_median([self._num(v["parsed"].get('exit_minutes'),
                                                          10, 5, 15) for v in src])),
                stop_loss_percent=_median([self._num(v["parsed"].get('stop_loss_percent'),
                                                     40, 0, 100) for v in src]),
                stop_loss_pips=sl if sl <= 0 else min(max(sl, 25.0), 80.0),
                take_profit_pips=tp if tp <= 0 else min(max(tp, 30.0), 120.0),
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
                    reasoning = (f"ENSEMBLE (force mode) votes {counts} -> "
                                 f"majority {top}, agreement {agreement:.2f}. "
                                 + representative_reasoning(votes_for))
                    logger.info(f"Ensemble force-mode majority: {counts} -> {top}")
                    return build(top, confidence, votes_for, reasoning)
                # All-SKIP or a dead BUY/SELL tie: same resolver as the
                # single-call force path (rule scores, not alphabet)
                direction, note = self._forced_direction(data_context)
                what = ("tie" if buysell
                        else f"all {len(votes)} votes SKIP")
                reasoning = (f"ENSEMBLE (force mode): {what} ({counts}); {note}")
                return build(direction, min(v["confidence"] for v in votes),
                             votes, reasoning)

            unanimous = (len(votes) >= 2 and len(counts) == 1
                         and dirs[0] in ('BUY', 'SELL'))
            if unanimous:
                confidence = _median([v["confidence"] for v in votes])
                reasoning = (f"ENSEMBLE {len(votes)}/{k} unanimous {dirs[0]} "
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
                         votes, f"ENSEMBLE SKIP: {why}.")
        except Exception as e:
            logger.error(f"Ensemble aggregation error: {e}")
            return self._rule_based_decision(event, data_context)

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

    def _chat(self, prompt: str) -> str:
        """Send one prompt to the configured LLM provider and return the raw text."""
        system_prompt = self._system_prompt()
        if self.provider == "openrouter":
            # OpenRouter uses OpenAI-compatible API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                temperature=0.3,
                timeout=60.0,  # 60 second timeout
                extra_headers={
                    "HTTP-Referer": "https://skytower-ai.local",
                    "X-Title": "SkyTower-AI Trading"
                }
            )
            logger.info(f"OpenRouter response received from {self.model}")
            return response.choices[0].message.content

        elif self.provider == "anthropic":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=LLM_CONFIG.get("max_tokens", 1500),
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                timeout=60.0  # 60 second timeout
            )
            return response.content[0].text

        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
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
            event_keywords=HIGH_IMPACT_EVENTS,
            currencies=["NZD", "CAD", "AUD", "USD", "GBP"]
        )

        if not event:
            logger.info("No upcoming tradeable events found")
            return None

        logger.info(f"Analyzing upcoming event: {event.event_name} ({event.currency})")
        return self.analyze_event(event)

    def get_best_pair_recommendation(
        self,
        event_info: Dict,
        pairs_data: Dict[str, Dict]
    ) -> Optional[TradingDecision]:
        """
        Analyze multiple pairs and select the best one for trading an event.
        Used in multi-instance mode where multiple EA's register their pairs.

        Args:
            event_info: Event details (event_name, currency, forecast, previous, event_time)
            pairs_data: Dict of pair -> {zones, current_price, spread_points, ohlc}

        Returns:
            TradingDecision for the best pair, or None if SKIP recommended
        """
        if not pairs_data:
            logger.warning("No pairs data provided for analysis")
            return None

        currency = event_info.get('currency', '')

        # Get COT and sentiment for the event currency
        cot_data = self.cot_analyzer.analyze_currency(currency)
        sentiment_data = self.sentiment.get_currency_sentiment(currency)

        # Get event time - use provided time or fallback to now
        event_time = event_info.get('event_time', datetime.now().isoformat())
        if isinstance(event_time, datetime):
            event_time = event_time.isoformat()

        # Build data context
        data_context = {
            "event": {
                "name": event_info.get('event_name', 'Unknown'),
                "currency": currency,
                "datetime": event_time,
                "impact": "HIGH",
                "forecast": event_info.get('forecast', ''),
                "previous": event_info.get('previous', ''),
            },
            "cot_analysis": cot_data,
            "sentiment_analysis": sentiment_data,
            "forecast_info": {
                "current_forecast": event_info.get('forecast', ''),
                "previous_value": event_info.get('previous', ''),
                "forecast_vs_previous": self._compare_values(
                    event_info.get('forecast', ''),
                    event_info.get('previous', '')
                )
            },
        }

        # Use LLM for multi-pair analysis
        if self.client:
            return self._llm_multi_pair_decision(data_context, pairs_data)
        else:
            return self._rule_based_multi_pair_decision(data_context, pairs_data)

    def _llm_multi_pair_decision(
        self,
        data_context: Dict,
        pairs_data: Dict[str, Dict]
    ) -> Optional[TradingDecision]:
        """Use LLM to select the best pair from multiple candidates"""

        # Build pairs summary for prompt
        pairs_summary = []
        for pair, data in pairs_data.items():
            zones = data.get('zones', {})
            pairs_summary.append({
                "pair": pair,
                "current_price": data.get('current_price', 0),
                "spread_points": data.get('spread_points', 0),
                "direction_bias": zones.get('direction_bias', 'neutral'),
                "bias_strength": zones.get('bias_strength', 0),
                "resistance_zones_count": len(zones.get('resistance_zones', [])),
                "support_zones_count": len(zones.get('support_zones', [])),
                "fvg_zones_count": len(zones.get('fvg_zones', [])),
            })

        prompt = f"""Analyze this economic event and SELECT THE BEST CURRENCY PAIR to trade:

EVENT DETAILS:
{json.dumps(data_context['event'], indent=2)}

COT (INSTITUTIONAL) ANALYSIS:
{json.dumps(data_context['cot_analysis'], indent=2)}

RETAIL SENTIMENT (USE AS CONTRARIAN):
{json.dumps(data_context['sentiment_analysis'], indent=2)}

FORECAST COMPARISON:
{json.dumps(data_context['forecast_info'], indent=2)}

AVAILABLE PAIRS WITH TECHNICAL DATA:
{json.dumps(pairs_summary, indent=2)}

PAIR SELECTION CRITERIA:
1. Lower spread = better execution
2. Strong zone alignment (direction_bias matching your intended trade direction)
3. Higher bias_strength = clearer technical setup
4. More zones in direction = better support/resistance levels

Your task:
1. First determine BUY, SELL, or SKIP based on fundamental analysis
2. If trading, SELECT ONE PAIR with the best technical setup for that direction
3. A pair with bullish bias is better for BUY, bearish bias for SELL
4. Set SL/TP based on volatility: JPY pairs need wider stops (40-80 pips), others 25-50 pips

Respond with JSON:
{{
    "selected_pair": "GBPUSD" or null if SKIP,
    "direction": "BUY" or "SELL" or "SKIP",
    "confidence": 0.0 to 1.0,
    "lot_percent": 60 to 85,
    "exit_minutes": 5 to 15,
    "stop_loss_pips": 25 to 80,
    "take_profit_pips": 30 to 120,
    "reasoning": "Why this pair was selected over others"
}}"""

        try:
            if not self.client:
                return self._rule_based_multi_pair_decision(data_context, pairs_data)

            response_text = self._chat(prompt)

            # Parse response
            decision_data = self._parse_llm_response(response_text)
            selected_pair = decision_data.get('selected_pair')
            direction = self._normalize_direction(decision_data.get('direction'))

            # Handle various forms of null/empty response
            if not selected_pair or selected_pair == 'null' or selected_pair == 'None' \
               or direction == 'SKIP':
                if FORCE_DECISION:
                    logger.warning("LLM recommends SKIP for all pairs — force mode, using rule-based fallback")
                    return self._rule_based_multi_pair_decision(data_context, pairs_data)
                logger.info("LLM recommends SKIP for all pairs")
                return None

            logger.info(f"LLM selected {selected_pair}: {decision_data.get('direction')} "
                       f"with {decision_data.get('confidence', 0):.0%} confidence")

            return TradingDecision(
                event=data_context['event']['name'],
                currency=data_context['event']['currency'],
                pair=selected_pair,
                direction=direction,
                confidence=decision_data.get('confidence', 0.0),
                lot_percent=decision_data.get('lot_percent', 70),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=decision_data.get('exit_minutes', 10),
                stop_loss_percent=decision_data.get('stop_loss_percent', 40),
                stop_loss_pips=decision_data.get('stop_loss_pips', 0),
                take_profit_pips=decision_data.get('take_profit_pips', 0),
                reasoning=decision_data.get('reasoning', 'Multi-pair LLM decision'),
                data_summary=data_context,
                timestamp=utcnow(),
                forced=FORCE_DECISION
            )

        except Exception as e:
            logger.error(f"LLM multi-pair decision error: {e}")
            return self._rule_based_multi_pair_decision(data_context, pairs_data)

    def _rule_based_multi_pair_decision(
        self,
        data_context: Dict,
        pairs_data: Dict[str, Dict]
    ) -> Optional[TradingDecision]:
        """Rule-based pair selection when LLM is unavailable"""

        # First, determine the EVENT-CURRENCY bias from fundamentals.
        # The BUY/SELL direction depends on each candidate pair's quoting
        # (bullish CAD = SELL USDCAD but BUY CADJPY), so bias and direction
        # are mapped per pair via _currency_bias_to_direction.
        bullish_score, bearish_score, reasons, _ = self._score_direction(data_context)
        currency = data_context.get('event', {}).get('currency', '')

        if bullish_score > bearish_score + 1:
            currency_bias = "BULLISH"
        elif bearish_score > bullish_score + 1:
            currency_bias = "BEARISH"
        elif FORCE_DECISION:
            if bullish_score != bearish_score:
                currency_bias = "BULLISH" if bullish_score > bearish_score else "BEARISH"
            else:
                forecast_cmp = data_context.get('forecast_info', {}).get('forecast_vs_previous', 'UNKNOWN')
                currency_bias = "BEARISH" if forecast_cmp == "DETERIORATION" else "BULLISH"
            reasons.append(f"forced bias {currency_bias} (no score margin)")
        else:
            logger.info("Rule-based: No clear direction, recommending SKIP")
            return None

        # Score each pair for its own mapped direction
        pair_scores = {}
        pair_details = {}  # For logging
        pair_directions = {}

        for pair, data in pairs_data.items():
            direction = self._currency_bias_to_direction(currency_bias, pair, currency)
            pair_directions[pair] = direction
            score = 100  # Base score
            zones = data.get('zones', {})
            details = [f"dir={direction}"]

            # Convert spread points to pips (JPY pairs have different multiplier)
            spread_points = data.get('spread_points', 0)
            if 'JPY' in pair.upper():
                # JPY pairs: 1 pip = 10 points (3 decimal places)
                spread_pips = spread_points / 10.0
            else:
                # Standard pairs: 1 pip = 10 points (5 decimal places)
                spread_pips = spread_points / 10.0

            # Lower spread is better (penalize high spreads)
            if spread_pips > 5:
                score -= (spread_pips - 5) * 5  # Heavy penalty above 5 pips
            elif spread_pips > 3:
                score -= (spread_pips - 3) * 2  # Light penalty above 3 pips
            details.append(f"spread={spread_pips:.1f}pips")

            # Zone alignment - this is critical for direction confirmation
            bias = zones.get('direction_bias', 'neutral')
            bias_strength = zones.get('bias_strength', 0)

            if direction == "BUY":
                if bias == "bullish":
                    # Zone confirms BUY direction
                    score += 30 + (bias_strength * 40)  # Up to +70 points
                    details.append(f"zone=BULLISH({bias_strength:.2f}) +{30 + bias_strength * 40:.0f}")
                elif bias == "bearish":
                    # Zone contradicts - reduce score significantly
                    score -= 30 + (bias_strength * 20)
                    details.append(f"zone=BEARISH({bias_strength:.2f}) -{30 + bias_strength * 20:.0f}")
                else:
                    details.append("zone=neutral")
            elif direction == "SELL":
                if bias == "bearish":
                    # Zone confirms SELL direction
                    score += 30 + (bias_strength * 40)  # Up to +70 points
                    details.append(f"zone=BEARISH({bias_strength:.2f}) +{30 + bias_strength * 40:.0f}")
                elif bias == "bullish":
                    # Zone contradicts - reduce score significantly
                    score -= 30 + (bias_strength * 20)
                    details.append(f"zone=BULLISH({bias_strength:.2f}) -{30 + bias_strength * 20:.0f}")
                else:
                    details.append("zone=neutral")

            pair_scores[pair] = score
            pair_details[pair] = f"{score:.0f} [{', '.join(details)}]"

        # Log all pair scores for transparency
        logger.info(f"Pair scores for {currency} {currency_bias}:")
        for pair, detail in sorted(pair_details.items(), key=lambda x: pair_scores[x[0]], reverse=True):
            logger.info(f"  {pair}: {detail}")

        # Select best pair; the traded direction is that pair's mapping
        best_pair = max(pair_scores, key=pair_scores.get)
        best_score = pair_scores[best_pair]
        direction = pair_directions[best_pair]

        logger.info(f"Rule-based selected {best_pair} (score: {best_score})")

        confidence = 0.6 + (best_score - 50) / 200  # Normalize to 0.5-0.8 range
        confidence = max(0.5, min(0.85, confidence))

        return TradingDecision(
            event=data_context['event']['name'],
            currency=data_context['event']['currency'],
            pair=best_pair,
            direction=direction,
            confidence=confidence,
            lot_percent=70 if confidence > 0.6 else 60,
            entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
            exit_minutes_after=10,
            stop_loss_percent=40,
            reasoning=f"Selected {best_pair}: {'; '.join(reasons)}",
            data_summary=data_context,
            timestamp=utcnow(),
            forced=FORCE_DECISION
        )


# =============================================================================
# TESTING
# =============================================================================
if __name__ == "__main__":
    import sys
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    print("=" * 70)
    print("SkyTower-AI Decision Engine Test")
    print("=" * 70)

    # Initialize engine (will use rule-based if no API key)
    engine = LLMDecisionEngine()

    # Get next trade recommendation
    print("\nSearching for next tradeable event...")
    decision = engine.get_next_trade_recommendation()

    if decision:
        print("\n" + "=" * 70)
        print("TRADING DECISION")
        print("=" * 70)
        print(f"Event: {decision.event}")
        print(f"Currency: {decision.currency}")
        print(f"Pair: {decision.pair}")
        print(f"Direction: {decision.direction}")
        print(f"Confidence: {decision.confidence:.2%}")
        print(f"Lot %: {decision.lot_percent}%")
        print(f"Entry: {decision.entry_seconds_before}s before")
        print(f"Exit: {decision.exit_minutes_after} min after")
        print(f"Stop Loss: {decision.stop_loss_percent}%")
        print(f"\nReasoning: {decision.reasoning}")

        print("\n" + "-" * 70)
        print("DATA SUMMARY:")
        print(json.dumps(decision.data_summary, indent=2, default=str)[:1000])
    else:
        print("No tradeable events found in the near future.")

    print("\n" + "=" * 70)
    print("Test complete!")
