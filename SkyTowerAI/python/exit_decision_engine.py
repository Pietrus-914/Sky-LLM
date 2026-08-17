"""
SkyTower-AI Exit Decision Engine
Uses LLM to make strategic position management decisions.
Operates on USD values for instrument-independent analysis.
"""
import json
from timeutil import utcnow
from typing import Optional
from loguru import logger
import os

from config import LLM_CONFIG, OPENROUTER_API_KEY, POSITION_MANAGEMENT_CONFIG
from llm_util import openrouter_headers, reasoning_body
from position_manager import OpenPosition, PositionCommand
from trading_units import forex_pip_size
from instrument_profiles import profile_value


EXIT_SYSTEM_PROMPT = """You are an expert forex position manager for the SkyTower-FX news trading strategy.

You are managing an OPEN position after a high-impact economic news release. Your job is to decide what to do RIGHT NOW based on the position data provided.

All profit/loss values are in USD (account currency) - this works identically regardless of which currency pair is traded.

POSITION MANAGEMENT PRINCIPLES:
1. Let winners run, cut losers quickly
2. After profit > $30, consider moving SL to break-even (entry_price + small buffer for BUY, - buffer for SELL)
3. Partial close (50%) when approaching strong resistance/support zones or after significant profit
4. Trail SL to lock in profits: as profit grows, SL should protect a meaningful portion
5. News impact typically peaks within 5-10 minutes after release, then momentum fades
6. If TOTAL trade profit (floating + realized) declined >40% from its peak, consider closing to protect remaining gains. After a partial close the FLOATING P/L drops by the closed fraction by construction - that is NOT lost momentum; always judge the decline on the TOTAL.
7. Max hold is 30 minutes - after that market returns to normal spread/volatility
8. Use zone data: if price is approaching a liquidity pool, FVG, or order block - that's a natural take-profit area
9. Zone bias: positive = bullish pressure (good for BUY), negative = bearish pressure (good for SELL)
10. Cutting losses: NEVER cut on elapsed time alone. A news spike routinely puts a fresh position underwater by the spread alone, and the broker stop loss already caps the worst case. Cut when BOTH are true: the loss is MATERIAL (roughly a quarter of the risk budget or worse - see RISK BUDGET) AND the RECENT TRAJECTORY shows no recovery from the max drawdown. A small drawdown at minute 5 on a delayed reaction is noise, not a failed thesis; closing it converts an open option into a certain loss.

DECISION FRAMEWORK (phases describe the OPPORTUNITY, they are not close triggers on their own - principle 10 governs losing positions in every phase):
- First minutes (0-3 min): Be patient, let the news reaction develop. Only act on extreme moves.
- Peak phase (3-10 min): This is where most profit is made. Trail SL to protect gains.
- Fade phase (10-20 min): Momentum fading. Bank or protect PROFIT here. A position that is merely underwater without a material loss is not a reason to close - the stop loss already caps it.
- Late phase (20-30 min): Wind the trade down; max hold ends it anyway. Still do not realize a small loss just because time passed.

AVAILABLE ACTIONS:
- HOLD: Do nothing, let position run. Use when position is developing favorably.
- MODIFY_SL: Change stop loss. Provide sl_price (instrument price). Use to lock in profits.
- MODIFY_TP: Move the broker take-profit. Provide tp_price (instrument price). Use to
  pull the target closer when momentum fades. Removing the TP is not possible, only
  moving it - when the target itself is wrong, prefer PARTIAL_CLOSE or CLOSE.
- PARTIAL_CLOSE: Close portion of position. Provide close_percent (25-75). Use at key levels.
- CLOSE: Full position close. Use when target reached, momentum lost, or protecting gains.

IMPORTANT: Respond with ONLY a JSON object, no markdown or explanation outside JSON.
Write the "reasoning" field FIRST (think it through there), then commit the action.
Keep reasoning under 40 words - the response must NEVER be cut off before "action":
{
    "reasoning": "Brief explanation (max 40 words)",
    "sl_price": 0.0,
    "tp_price": 0.0,
    "close_percent": 0,
    "action": "HOLD"
}"""


def _stop_is_below_market(pos: OpenPosition, sl_price: float,
                          margin: float) -> bool:
    """True when sl_price is on the LOSS side of the current price, i.e. a
    stop the broker can actually accept (below the bid for a BUY, above the
    ask for a SELL), with a small margin so a stop is not placed right on top
    of the market. Named for the BUY case; the SELL branch is mirrored.

    A stop on the wrong side is not merely rejected: the server marks the
    command applied on delivery, so the position would be recorded as
    break-even-protected while its real stop never moved.
    """
    if not pos.current_price or pos.current_price <= 0:
        return False
    if pos.direction == "BUY":
        return sl_price < pos.current_price - margin
    return sl_price > pos.current_price + margin


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
                    base_url="https://openrouter.ai/api/v1",
                    # One transport retry, not the SDK default of two: exits are
                    # consulted every ~30s, so a call that keeps retrying past
                    # the next check just delays position management.
                    max_retries=1,
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

        # RISK BUDGET: without it the model cannot tell a -$20 spread-noise
        # drawdown (priced into the stop) from a -$90 near-budget failure, so
        # the cut-loss principle degenerated into "negative at 5 min -> CLOSE".
        budget = pos.max_loss_usd or POSITION_MANAGEMENT_CONFIG.get(
            "max_loss_usd", 100.0)
        total_pnl = pos.profit_usd + pos.realized_usd
        used_pct = (abs(total_pnl) / budget * 100.0) if budget > 0 else 0.0
        budget_line = (
            f"- Risk budget for this trade: ${budget:.2f} "
            f"(forced close at -${budget:.2f})\n"
            f"- Current TOTAL P/L uses {used_pct:.0f}% of that budget"
            + (" (LOSS)" if total_pnl < 0 else " (in profit)")
        )

        # RECENT TRAJECTORY: "signs of recovery" was unmeasurable from a single
        # snapshot — the model only saw one P/L number and its own prior HOLDs.
        trajectory = self._format_trajectory(pos)

        prompt = f"""CURRENT POSITION STATUS:
- Symbol: {pos.symbol}
- Direction: {pos.direction}
- Entry price: {pos.entry_price}
- Current price: {pos.current_price}
- Lots: {pos.remaining_lots} (original: {pos.lots})
- Stop Loss: {pos.sl}
- Take Profit: {pos.tp if pos.tp else "none (no broker TP on this position)"}
- Profit/Loss (floating, remaining lots only): ${pos.profit_usd:.2f}
- Realized by partial closes: ${pos.realized_usd:.2f}
- TOTAL trade P/L: ${pos.profit_usd + pos.realized_usd:.2f}
- Peak TOTAL profit: ${pos.max_profit_usd:.2f}
- Max drawdown: ${pos.max_drawdown_usd:.2f}
- Time open: {minutes_open:.1f} minutes
- Tick value: ${pos.tick_value:.2f} per tick
- Current spread: {pos.spread_pips:.1f} pips
- Account balance: ${pos.account_balance:.2f}

RISK BUDGET:
{budget_line}

RECENT TRAJECTORY (oldest to newest, from EA reports):
{trajectory}

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

    @staticmethod
    def _format_trajectory(pos: OpenPosition) -> str:
        """Render the recorded price/P-L samples so "no signs of recovery" is
        an observation instead of a guess. Samples are appended by
        PositionManager on every EA report and capped there."""
        samples = getattr(pos, "pnl_samples", None) or []
        if not samples:
            return "No samples yet (first report of this position)."
        lines = []
        for s in samples[-10:]:
            lines.append(
                f"  T+{s.get('minutes', 0):.1f}min  price {s.get('price', 0)}  "
                f"TOTAL P/L ${s.get('pnl', 0):.2f}"
            )
        newest = samples[-1].get('pnl', 0.0)
        worst = min(s.get('pnl', 0.0) for s in samples)
        if newest > worst:
            lines.append(f"  -> recovering: ${newest - worst:.2f} back from the "
                         f"worst sample (${worst:.2f})")
        elif newest < worst + 1e-9:
            lines.append("  -> at or near its worst sample: no recovery yet")
        return "\n".join(lines)

    def _llm_decision(self, pos: OpenPosition) -> PositionCommand:
        """Use LLM to make exit decision."""
        prompt = self._build_prompt(pos)
        # Non-forex instruments may carry their own exit narrative in
        # instrument_profiles; forex keeps EXIT_SYSTEM_PROMPT verbatim.
        system_prompt = profile_value(pos.symbol, "exit_system_prompt",
                                      EXIT_SYSTEM_PROMPT)

        logger.info(f"ExitEngine: Calling LLM for {pos.symbol} (P/L: ${pos.profit_usd:.2f})")

        if self.provider == "openrouter":
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                # Same reason as the entry budget (config.LLM_CONFIG): with a
                # mandatory-reasoning model, thinking tokens can eat the
                # completion budget before the JSON appears. The exit prompt
                # is small and its reasoning is capped at 40 words, which is
                # why this call kept working while the entry vote did not —
                # but the headroom costs nothing when unused.
                max_tokens=POSITION_MANAGEMENT_CONFIG.get(
                    "exit_max_tokens", 2000),
                temperature=0.2,
                timeout=30.0,
                extra_headers=openrouter_headers("exit",
                                                 "SkyTower-AI Exit Manager"),
                extra_body=reasoning_body(
                    POSITION_MANAGEMENT_CONFIG.get("exit_reasoning_effort")),
            )
            response_text = response.choices[0].message.content

        elif self.provider == "anthropic":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=900,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0
            )
            response_text = response.content[0].text

        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=900,
                temperature=0.2,
                timeout=30.0
            )
            response_text = response.choices[0].message.content
        else:
            return self._rule_based_decision(pos)

        return self._parse_llm_response(response_text, pos)

    def _parse_llm_response(self, text: str,
                            pos: Optional[OpenPosition] = None) -> PositionCommand:
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

            # `or 0`: a JSON null KEY EXISTS, so .get's default alone returns
            # None and float(None) raised TypeError — which the except below
            # did not catch, silently demoting a VALID model verdict (usually
            # HOLD with "tp_price": null) to the rule-based fallback.
            cmd = PositionCommand(
                action=action,
                sl_price=float(data.get("sl_price") or 0),
                tp_price=float(data.get("tp_price") or 0),
                close_percent=float(data.get("close_percent") or 0),
                reason=f"AI: {data.get('reasoning', 'No reasoning')}",
            )
            if cmd.action == "MODIFY_TP":
                cmd = self._validate_modify_tp(cmd, pos)
            return cmd
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.error(f"ExitEngine: Failed to parse LLM response: {e}")
            logger.error(f"ExitEngine: Raw response: {text[:300]}")
            return PositionCommand(action="HOLD", reason="AI: Parse error, holding")

    @staticmethod
    def _validate_modify_tp(cmd: PositionCommand,
                            pos: Optional[OpenPosition]) -> PositionCommand:
        """tp_price is the only model-produced number that reaches the broker
        unclamped, and a wrong-units value (pips instead of an instrument
        price, e.g. 15 on NZDUSD ~0.59) can land on the VALID far side of the
        market — the broker accepts it and the real target is silently gone.
        Demote anything that is not a plausible profit-side price to HOLD."""
        if pos is None:
            return cmd
        price = pos.current_price
        if not price or price <= 0:
            return PositionCommand(
                action="HOLD",
                reason="AI: MODIFY_TP dropped (no current price to validate against)")
        pip_size = forex_pip_size(pos.symbol)
        margin = pip_size  # do not park a TP right on top of the market
        on_profit_side = (cmd.tp_price > price + margin
                          if pos.direction == "BUY"
                          else 0 < cmd.tp_price < price - margin)
        # Units sanity: a genuine TP lives within a few hundred pips of the
        # market; 500 pips is far beyond any clamp/stat this system produces.
        band_pips = profile_value(pos.symbol, "tp_sanity_band_pips", 500.0)
        within_band = abs(cmd.tp_price - price) <= band_pips * pip_size
        if on_profit_side and within_band:
            return cmd
        logger.warning(
            f"ExitEngine: MODIFY_TP {cmd.tp_price} rejected "
            f"({pos.direction} @ {price}) — wrong side or implausible units; "
            f"holding instead")
        return PositionCommand(
            action="HOLD",
            reason=f"AI: MODIFY_TP {cmd.tp_price} invalid vs market {price} — held")

    def _rule_based_decision(self, pos: OpenPosition) -> PositionCommand:
        """
        Rule-based fallback for exit decisions.
        Used when LLM is unavailable or fails.
        """
        minutes_open = (utcnow() - pos.open_time).total_seconds() / 60

        # WHOLE-TRADE P/L: floating on the remaining lots PLUS what partial
        # closes already realized. profit_usd alone covers only the open leg,
        # so after a losing partial close every threshold below read a trade
        # that was doing far worse than it looked — the guardrails and the LLM
        # prompt have always used the total, this fallback had drifted.
        # Identical to profit_usd before any partial close (realized_usd = 0).
        total_pnl = pos.profit_usd + pos.realized_usd

        # Rule 1: Move SL to break-even after $30+ profit.
        # `pos.profit_usd > 0` is NOT redundant with the total: realized profit
        # from an earlier partial can satisfy the total while the REMAINING leg
        # is underwater, and an entry+buffer stop is only a valid stop while
        # price is on the profitable side of entry. Without it a break-even
        # order lands on the wrong side of the market, the broker rejects it,
        # and the server still marks the position break-even-protected.
        if total_pnl > 30.0 and not pos.sl_moved_to_be and pos.profit_usd > 0:
            pip_size = forex_pip_size(pos.symbol)
            # 1 pip buffer on forex; a non-forex profile may widen it
            buffer = pip_size * profile_value(pos.symbol, "rule_be_buffer_pips", 1.0)

            if pos.direction == "BUY":
                new_sl = pos.entry_price + buffer
                if new_sl > pos.sl and _stop_is_below_market(pos, new_sl, buffer):
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=new_sl,
                        reason=f"Rule: Move SL to BE after ${total_pnl:.0f} profit",
                    )
            else:
                new_sl = pos.entry_price - buffer
                if ((new_sl < pos.sl or pos.sl == 0)
                        and _stop_is_below_market(pos, new_sl, buffer)):
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=new_sl,
                        reason=f"Rule: Move SL to BE after ${total_pnl:.0f} profit",
                    )

        # Rule 2: Partial close after $60+ profit (if not done)
        if total_pnl > 60.0 and not pos.partial_closed:
            return PositionCommand(
                action="PARTIAL_CLOSE",
                close_percent=50,
                reason=f"Rule: Partial close 50% at ${total_pnl:.0f} profit",
            )

        # Rule 3: Trail SL to protect profits
        if total_pnl > 40.0 and pos.sl_moved_to_be:
            pip_size = forex_pip_size(pos.symbol)
            # 10 pips on forex; a non-forex profile may declare its own trail
            trail_distance = pip_size * profile_value(pos.symbol, "rule_trail_pips", 10.0)

            if pos.direction == "BUY":
                potential_sl = pos.current_price - trail_distance
                if (potential_sl > pos.sl
                        and _stop_is_below_market(pos, potential_sl, pip_size)):
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=potential_sl,
                        reason=f"Rule: Trailing SL (profit ${total_pnl:.0f})",
                    )
            else:
                potential_sl = pos.current_price + trail_distance
                if ((potential_sl < pos.sl or pos.sl == 0)
                        and _stop_is_below_market(pos, potential_sl, pip_size)):
                    return PositionCommand(
                        action="MODIFY_SL",
                        sl_price=potential_sl,
                        reason=f"Rule: Trailing SL (profit ${total_pnl:.0f})",
                    )

        # Rule 4: Close if losing after 10 minutes with no recovery
        if minutes_open > 10 and total_pnl < -20.0:
            return PositionCommand(
                action="CLOSE",
                reason=f"Rule: Losing ${total_pnl:.0f} after {minutes_open:.0f}min, cutting loss",
            )

        # Rule 5: Close in late phase if profit is small
        if minutes_open > 20 and abs(total_pnl) < 15.0:
            return PositionCommand(
                action="CLOSE",
                reason=f"Rule: Late phase ({minutes_open:.0f}min), minimal P/L ${total_pnl:.0f}",
            )

        return PositionCommand(action="HOLD", reason="Rule: No action needed")
