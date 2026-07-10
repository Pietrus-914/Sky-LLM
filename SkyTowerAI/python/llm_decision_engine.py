"""
LLM Decision Engine for SkyTower-AI
Uses AI to analyze multiple data sources and make trading decisions
"""
import json
from datetime import datetime
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
from loguru import logger
import os

# Import our data modules
from calendar_fetcher import CalendarAggregator, EconomicEvent
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from event_reaction_history import EventReactionHistory
from market_context import normalize_pair
from config import LLM_CONFIG, TRADING_CONFIG, DEFAULT_PAIRS, HIGH_IMPACT_EVENTS, OPENROUTER_API_KEY, FORCE_DECISION


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

    def to_dict(self):
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
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

DECISION OUTPUT FORMAT:
You must respond with a JSON object containing:
{
    "direction": %DIRECTION_VALUES%,
    "confidence": 0.0 to 1.0,
    "lot_percent": 60 to 85 (percent of max lot),
    "exit_minutes": 5 to 15,
    "stop_loss_pips": 25 to 80 (wider for JPY pairs: 40-80),
    "take_profit_pips": 30 to 120 (1.5x to 2x of SL),
    "reasoning": "Brief explanation of decision"
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

    def __init__(self, api_key: str = None, provider: str = None):
        """
        Initialize the decision engine

        Args:
            api_key: API key for the LLM provider
            provider: "openrouter", "anthropic", "openai", or "rule-based"
        """
        # Auto-detect provider from config or environment
        self.provider = provider or LLM_CONFIG.get("provider", "openrouter")
        self.api_key = api_key or OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")

        # Initialize data sources
        self.calendar = CalendarAggregator()
        self.cot_analyzer = COTAnalyzer()
        self.sentiment = SentimentAggregator()
        self.reaction_history = EventReactionHistory()

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

        # Historical reactions to this event (builds up over time)
        reaction_summary = None
        try:
            reaction_summary = self.reaction_history.summarize(event.event_name, currency)
        except Exception as e:
            logger.debug(f"Reaction history lookup failed: {e}")
        source_status["reaction_history"] = "ok" if reaction_summary else "no_data"

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
            "_source_status": source_status,
        }

    def _market_context_section(self, data_context: Dict) -> str:
        """Prompt section for live market data (trend/ATR/zones), or a degraded-source note."""
        market = data_context.get('market_context')
        if not market:
            return "NOT AVAILABLE (no price data pushed by the EA for this event)"
        text = json.dumps(market, indent=2)
        age = market.get('data_age_minutes')
        if age is not None and age > 15:
            text += f"\nNOTE: this market data is {age} minutes old — treat with caution."
        return text

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

    def _llm_decision(self, event: EconomicEvent, data_context: Dict) -> TradingDecision:
        """Use LLM to make trading decision"""
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

HISTORICAL REACTIONS TO THIS EVENT:
{data_context.get('reaction_history') or "No history yet (the system is building this dataset)"}

SUGGESTED PAIR: {data_context['suggested_pair']}

Based on this data, provide your trading decision in JSON format."""

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

            return TradingDecision(
                event=event.event_name,
                currency=event.currency,
                pair=data_context['suggested_pair'],
                direction=direction,
                confidence=decision_data.get('confidence', 0.0),
                lot_percent=decision_data.get('lot_percent', 70),
                entry_seconds_before=TRADING_CONFIG['entry_seconds_before'],
                exit_minutes_after=decision_data.get('exit_minutes', 10),
                stop_loss_percent=decision_data.get('stop_loss_percent', 40),
                stop_loss_pips=decision_data.get('stop_loss_pips', 0),
                take_profit_pips=decision_data.get('take_profit_pips', 0),
                reasoning=reasoning,
                data_summary=data_context,
                timestamp=datetime.utcnow(),
                forced=FORCE_DECISION
            )

        except Exception as e:
            logger.error(f"LLM decision error: {e}")
            return self._rule_based_decision(event, data_context)

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
                max_tokens=1000,
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
                max_tokens=1000,
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
                max_tokens=1000,
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
            return (event_time - datetime.utcnow()).total_seconds()
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
        """Parse JSON from LLM response"""
        try:
            # Try to find JSON in response
            import re
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
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
            timestamp=datetime.utcnow(),
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
                timestamp=datetime.utcnow(),
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
            timestamp=datetime.utcnow(),
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
