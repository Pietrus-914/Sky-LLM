"""
SkyTower-AI Exit Decision Engine
Uses LLM to make strategic position management decisions.
Operates on USD values for instrument-independent analysis.
"""
import json
from datetime import datetime
from timeutil import utcnow
from typing import Optional
from loguru import logger
import os

from config import LLM_CONFIG, OPENROUTER_API_KEY, POSITION_MANAGEMENT_CONFIG
from position_manager import OpenPosition, PositionCommand


EXIT_SYSTEM_PROMPT = """You are an expert forex position manager for the SkyTower-FX news trading strategy.

You are managing an OPEN position after a high-impact economic news release. Your job is to decide what to do RIGHT NOW based on the position data provided.

All profit/loss values are in USD (account currency) - this works identically regardless of which currency pair is traded.

POSITION MANAGEMENT PRINCIPLES:
1. Let winners run, cut losers quickly
2. After profit > $30, consider moving SL to break-even (entry_price + small buffer for BUY, - buffer for SELL)
3. Partial close (50%) when approaching strong resistance/support zones or after significant profit
4. Trail SL to lock in profits: as profit grows, SL should protect a meaningful portion
5. News impact typically peaks within 5-10 minutes after release, then momentum fades
6. If profit declined >40% from its peak, consider closing to protect remaining gains
7. Max hold is 30 minutes - after that market returns to normal spread/volatility
8. Use zone data: if price is approaching a liquidity pool, FVG, or order block - that's a natural take-profit area
9. Zone bias: positive = bullish pressure (good for BUY), negative = bearish pressure (good for SELL)
10. If position is losing and no signs of recovery after 5+ minutes, cut the loss

DECISION FRAMEWORK:
- First minutes (0-3 min): Be patient, let the news reaction develop. Only act on extreme moves.
- Peak phase (3-10 min): This is where most profit is made. Trail SL to protect gains.
- Fade phase (10-20 min): Momentum fading. If not already in significant profit, consider closing.
- Late phase (20-30 min): Close unless there's a very strong reason to hold.

AVAILABLE ACTIONS:
- HOLD: Do nothing, let position run. Use when position is developing favorably.
- MODIFY_SL: Change stop loss. Provide sl_price (instrument price). Use to lock in profits.
- PARTIAL_CLOSE: Close portion of position. Provide close_percent (25-75). Use at key levels.
- CLOSE: Full position close. Use when target reached, momentum lost, or protecting gains.

IMPORTANT: Respond with ONLY a JSON object, no markdown or explanation outside JSON.
Write the "reasoning" field FIRST (think it through there), then commit the action.
Keep reasoning under 40 words - the response must NEVER be cut off before "action":
{
    "reasoning": "Brief explanation (max 40 words)",
    "sl_price": 0.0,
    "close_percent": 0,
    "action": "HOLD"
}"""


class ExitDecisionEngine:
    """
    AI-powered exit decision engine.
    Uses LLM for strategic decisions, with rule-based fallback.
    """

    def __init__(self, api_key: str = None, provider: str = None):
        self.provider = provider or LLM_CONFIG.get("provider", "openrouter")
        self.api_key = (api_key or OPENROUTER_API_KEY
                        or os.getenv("OPENROUTER_API_KEY")
                        or os.getenv("ANTHROPIC_API_KEY")
                        or os.getenv("OPENAI_API_KEY"))
        self.client = None
        self.model = None
        self._init_llm_client()

    def _init_llm_client(self):
        """Initialize LLM client (same pattern as LLMDecisionEngine)."""
        exit_model = POSITION_MANAGEMENT_CONFIG.get("exit_llm_model")

        if self.provider == "openrouter":
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url="https://openrouter.ai/api/v1"
                )
                self.model = exit_model or LLM_CONFIG.get("model", "anthropic/claude-sonnet-4")
                logger.info(f"ExitEngine: Initialized OpenRouter with model: {self.model}")
            except ImportError:
                logger.warning("ExitEngine: OpenAI package not installed, using rule-based")
                self.client = None
        elif self.provider == "anthropic":
            try:
                from anthropic import Anthropic
                self.client = Anthropic(api_key=self.api_key)
                self.model = exit_model or "claude-sonnet-4-20250514"
                logger.info(f"ExitEngine: Initialized Anthropic with model: {self.model}")
            except ImportError:
                self.client = None
        elif self.provider == "openai":
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
                self.model = exit_model or "gpt-4o"
                logger.info(f"ExitEngine: Initialized OpenAI with model: {self.model}")
            except ImportError:
                self.client = None
        else:
            self.client = None
            logger.info("ExitEngine: Using rule-based exit decisions (no LLM)")

    def decide(self, position: OpenPosition) -> Optional[PositionCommand]:
        """
        Make an exit decision for the given position.
        Returns PositionCommand or None if error.
        """
        if self.client:
            try:
                return self._llm_decision(position)
            except Exception as e:
                logger.error(f"ExitEngine LLM error: {e}, falling back to rule-based")
                return self._rule_based_decision(position)
        else:
            return self._rule_based_decision(position)

    def _build_prompt(self, pos: OpenPosition) -> str:
        """Build the LLM prompt with position context."""
        minutes_open = (utcnow() - pos.open_time).total_seconds() / 60

        # Recent AI decisions (last 5)
        recent_decisions = pos.ai_decisions[-5:] if pos.ai_decisions else []

        prompt = f"""CURRENT POSITION STATUS:
- Symbol: {pos.symbol}
- Direction: {pos.direction}
- Entry price: {pos.entry_price}
- Current price: {pos.current_price}
- Lots: {pos.remaining_lots} (original: {pos.lots})
- Stop Loss: {pos.sl}
- Profit/Loss: ${pos.profit_usd:.2f}
- Peak profit: ${pos.max_profit_usd:.2f}
- Max drawdown: ${pos.max_drawdown_usd:.2f}
- Time open: {minutes_open:.1f} minutes
- Tick value: ${pos.tick_value:.2f} per tick
- Current spread: {pos.spread_pips:.1f} pips
- Account balance: ${pos.account_balance:.2f}

MARKET CONTEXT:
- Zone bias: {pos.zone_bias:.2f} (positive=bullish, negative=bearish)
- Nearest resistance: {pos.nearest_resistance}
- Nearest support: {pos.nearest_support}

ENTRY CONTEXT:
- Event: {pos.event_name}
- Entry reasoning: {pos.entry_reasoning}
- Partial close done: {pos.partial_closed}
- SL moved to break-even: {pos.sl_moved_to_be}

RECENT AI DECISIONS:
{json.dumps(recent_decisions, indent=2) if recent_decisions else "None yet (first check)"}

Based on this data, what should we do with this position RIGHT NOW?
Respond with JSON only."""

        return prompt

    def _llm_decision(self, pos: OpenPosition) -> PositionCommand:
        """Use LLM to make exit decision."""
        prompt = self._build_prompt(pos)

        logger.info(f"ExitEngine: Calling LLM for {pos.symbol} (P/L: ${pos.profit_usd:.2f})")

        if self.provider == "openrouter":
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": EXIT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=900,
                temperature=0.2,
                timeout=30.0,
                extra_headers={
                    "HTTP-Referer": "https://skytower-ai.local",
                    "X-Title": "SkyTower-AI Exit Manager"
                }
            )
            response_text = response.choices[0].message.content

        elif self.provider == "anthropic":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=900,
                system=EXIT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0
            )
            response_text = response.content[0].text

        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": EXIT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=900,
                temperature=0.2,
                timeout=30.0
            )
            response_text = response.choices[0].message.content
        else:
            return self._rule_based_decision(pos)

        return self._parse_llm_response(response_text)

    def _parse_llm_response(self, text: str) -> PositionCommand:
        """Parse LLM response into a PositionCommand."""
        try:
            # Extract JSON from response (handle markdown code blocks)
            clean = text.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                json_lines = [l for l in lines if not l.startswith("```")]
                clean = "\n".join(json_lines)

            try:
                data = json.loads(clean)
            except json.JSONDecodeError:
                # Salvage: outermost {...} slice (model wrapped the JSON in
                # prose, or trailing junk after the object)
                if '{' in clean and '}' in clean:
                    data = json.loads(clean[clean.find('{'):clean.rfind('}') + 1])
                else:
                    raise

            action = data.get("action", "HOLD").upper()
            if action not in ("HOLD", "MODIFY_SL", "MODIFY_TP", "PARTIAL_CLOSE", "CLOSE"):
                action = "HOLD"

            return PositionCommand(
                action=action,
                sl_price=float(data.get("sl_price", 0)),
                tp_price=float(data.get("tp_price", 0)),
                close_percent=float(data.get("close_percent", 0)),
                reason=f"AI: {data.get('reasoning', 'No reasoning')}",
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"ExitEngine: Failed to parse LLM response: {e}")
            logger.error(f"ExitEngine: Raw response: {text[:300]}")
            return PositionCommand(action="HOLD", reason="AI: Parse error, holding")

    def _rule_based_decision(self, pos: OpenPosition) -> PositionCommand:
        """
        Rule-based fallback for exit decisions.
        Used when LLM is unavailable or fails.
        """
        minutes_open = (utcnow() - pos.open_time).total_seconds() / 60

        # Rule 1: Move SL to break-even after $30+ profit
        if pos.profit_usd > 30.0 and not pos.sl_moved_to_be:
            pip_size = 0.01 if "JPY" in pos.symbol else 0.0001
            buffer = pip_size * 10  # 1 pip buffer

            if pos.direction == "BUY":
                new_sl = pos.entry_price + buffer
                if new_sl > pos.sl:
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=new_sl,
                        reason=f"Rule: Move SL to BE after ${pos.profit_usd:.0f} profit",
                    )
            else:
                new_sl = pos.entry_price - buffer
                if new_sl < pos.sl or pos.sl == 0:
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=new_sl,
                        reason=f"Rule: Move SL to BE after ${pos.profit_usd:.0f} profit",
                    )

        # Rule 2: Partial close after $60+ profit (if not done)
        if pos.profit_usd > 60.0 and not pos.partial_closed:
            return PositionCommand(
                action="PARTIAL_CLOSE",
                close_percent=50,
                reason=f"Rule: Partial close 50% at ${pos.profit_usd:.0f} profit",
            )

        # Rule 3: Trail SL to protect profits
        if pos.profit_usd > 40.0 and pos.sl_moved_to_be:
            pip_size = 0.01 if "JPY" in pos.symbol else 0.0001
            trail_distance = pip_size * 100  # 10 pips

            if pos.direction == "BUY":
                potential_sl = pos.current_price - trail_distance
                if potential_sl > pos.sl:
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=potential_sl,
                        reason=f"Rule: Trailing SL (profit ${pos.profit_usd:.0f})",
                    )
            else:
                potential_sl = pos.current_price + trail_distance
                if potential_sl < pos.sl or pos.sl == 0:
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=potential_sl,
                        reason=f"Rule: Trailing SL (profit ${pos.profit_usd:.0f})",
                    )

        # Rule 4: Close if losing after 10 minutes with no recovery
        if minutes_open > 10 and pos.profit_usd < -20.0:
            return PositionCommand(
                action="CLOSE",
                reason=f"Rule: Losing ${pos.profit_usd:.0f} after {minutes_open:.0f}min, cutting loss",
            )

        # Rule 5: Close in late phase if profit is small
        if minutes_open > 20 and abs(pos.profit_usd) < 15.0:
            return PositionCommand(
                action="CLOSE",
                reason=f"Rule: Late phase ({minutes_open:.0f}min), minimal P/L ${pos.profit_usd:.0f}",
            )

        return PositionCommand(action="HOLD", reason="Rule: No action needed")
