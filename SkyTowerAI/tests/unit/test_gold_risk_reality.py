"""
Gold (XAUUSD) audit fixes, 23.08.2026 — the server-side half.

Covers:
* the EA's ``risk_usd`` / ``margin_capped`` echo -> OpenPosition, the
  effective-risk rule, the profit-protection floor armed on the REAL risk
  (a $1 000 panel budget on a margin-capped 0.2-lot gold position was a
  guardrail that could never arm);
* exit prompt: INSTRUMENT block + "risk at the broker stop" wording for
  gold, forex prompt unchanged; planned-exit horizon line;
* rule-based exit fallback thresholds scale with the effective risk and are
  byte-identical at the historical $100 budget;
* routing alias canonicalization (USD:GOLD -> XAUUSD);
* per-instrument event policy (gold adds Core PCE / PPI, drops Home Sales)
  applied only to routed currencies;
* zone-config substitution for profiled instruments;
* clamp-warning for out-of-unit LLM numbers on gold only.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import config as cfg
import llm_decision_engine as lde
from calendar_fetcher import CalendarAggregator
from exit_decision_engine import ExitDecisionEngine
from instrument_profiles import (canonical_symbol, event_policy_for,
                                 zone_config_for, profile_for)
from position_manager import OpenPosition, PositionManager
from timeutil import utcnow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def gold_open(**over):
    data = {
        "ticket": 777, "symbol": "XAUUSD_raw", "direction": "SELL",
        "entry_price": 2400.00, "lots": 0.20, "sl": 2408.00, "tp": 0.0,
        "tick_value": 1.0, "account_balance": 1000.0,
        "event_name": "CPI m/m", "max_loss_usd": 1000.0,
        "pip_size": 0.10,
    }
    data.update(over)
    return data


def gold_report(**over):
    data = {
        "ticket": 777, "current_price": 2398.00, "remaining_lots": 0.20,
        "sl": 2408.00, "tp": 0.0, "profit_usd": 40.0, "tick_value": 1.0,
        "account_balance": 1000.0, "spread_pips": 8.0, "pip_size": 0.10,
        "zone_bias": 0.0, "nearest_resistance": 0.0, "nearest_support": 0.0,
        "max_loss_usd": 1000.0,
    }
    data.update(over)
    return data


def make_pos(**over):
    now = utcnow()
    d = dict(ticket=1, symbol="NZDUSD", direction="BUY", entry_price=0.62,
             current_price=0.621, lots=0.5, remaining_lots=0.5, sl=0.617, tp=0.0,
             profit_usd=0.0, max_profit_usd=0.0, max_drawdown_usd=0.0,
             tick_value=10.0, account_balance=5000.0, open_time=now,
             last_update=now, event_name="CPI m/m", entry_reasoning="r",
             max_loss_usd=100.0)
    d.update(over)
    return OpenPosition(**d)


# ---------------------------------------------------------------------------
# effective risk + profit protection
# ---------------------------------------------------------------------------

class TestEffectiveRisk:
    def test_unknown_risk_falls_back_to_budget(self):
        assert make_pos(max_loss_usd=1000.0).effective_risk_usd() == 1000.0
        assert make_pos(max_loss_usd=0.0).effective_risk_usd(100.0) == 100.0

    def test_risk_below_budget_wins_only_when_structurally_capped(self):
        # gold (profiled): the echo wins below the budget
        assert make_pos(symbol="XAUUSD", max_loss_usd=1000.0, risk_usd=160.0).effective_risk_usd() == 160.0
        assert make_pos(symbol="XAUUSD", max_loss_usd=100.0, risk_usd=250.0).effective_risk_usd() == 100.0
        # forex sized at 70% of the budget via lot_percent: budget stays (unchanged behaviour)
        assert make_pos(max_loss_usd=1000.0, risk_usd=700.0).effective_risk_usd() == 1000.0
        # forex whose lot the broker margin actually capped: real risk
        assert make_pos(max_loss_usd=1000.0, risk_usd=700.0, margin_capped=True).effective_risk_usd() == 700.0

    def test_report_parses_risk_echo_first_value_wins(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open(risk_usd=160.0, margin_capped=True))
        assert pm.position.risk_usd == 160.0 and pm.position.margin_capped is True
        # a later echo (EA re-adopted the trade, stop already at break-even)
        # must not overwrite the risk at the INITIAL stop
        pm.update_position(gold_report(risk_usd=5.0, margin_capped=False))
        assert pm.position.risk_usd == 160.0 and pm.position.margin_capped is True
        # unknown at open, learned from the first positive report
        pm2 = PositionManager(exit_engine=None)
        pm2.on_position_opened(gold_open())
        pm2.update_position(gold_report(risk_usd=165.0, margin_capped=True))
        assert pm2.position.risk_usd == 165.0 and pm2.position.margin_capped is True

    def test_old_ea_without_echo_keeps_today_behaviour(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open())
        assert pm.position.risk_usd == 0.0
        assert pm.position.effective_risk_usd() == 1000.0

    def test_profit_protection_arms_on_real_risk_for_margin_capped_gold(self):
        """$1 000 panel budget, 0.2 lot gold with $160 at the stop: the old
        floor (30% of $1 000 = $300) was unreachable; the floor is now 30% of
        $160 = $48, so a $60 peak arms and a confirmed 50%+ give-back closes."""
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open(risk_usd=160.0, margin_capped=True))
        pm.position.open_time = utcnow() - timedelta(minutes=5)
        pm.update_position(gold_report(profit_usd=60.0))
        first = pm.update_position(gold_report(profit_usd=20.0))
        assert first["command"]["action"] == "HOLD"          # debounce
        result = pm.update_position(gold_report(profit_usd=18.0))
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_profit_protection_floor_unchanged_without_echo(self):
        """Same trade, EA without the echo: floor stays 30% of $1 000 and a
        $60 peak must NOT arm (today's behaviour, byte for byte)."""
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open())
        pm.position.open_time = utcnow() - timedelta(minutes=5)
        pm.update_position(gold_report(profit_usd=60.0))
        pm.update_position(gold_report(profit_usd=20.0))
        result = pm.update_position(gold_report(profit_usd=18.0))
        assert result["command"]["action"] == "HOLD"

    def test_max_loss_guardrail_still_uses_panel_budget(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open(risk_usd=160.0))
        result = pm.update_position(gold_report(profit_usd=-170.0))
        assert result["command"]["action"] == "HOLD"     # -$170 > -$1 000 panel cap
        result = pm.update_position(gold_report(profit_usd=-1001.0))
        assert result["command"]["action"] == "CLOSE"

    def test_planned_exit_minutes_stored(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(gold_open(), planned_exit_minutes=12)
        assert pm.position.planned_exit_minutes == 12
        assert pm.position.to_dict()["planned_exit_minutes"] == 12


# ---------------------------------------------------------------------------
# exit prompt
# ---------------------------------------------------------------------------

class TestExitPrompt:
    def test_forex_prompt_unchanged(self):
        engine = ExitDecisionEngine(provider="rule-based")
        prompt = engine._build_prompt(make_pos(max_loss_usd=100.0))
        assert "INSTRUMENT:" not in prompt
        assert "- Risk budget for this trade: $100.00 (forced close at -$100.00)" in prompt
        assert "planned the exit" not in prompt

    def test_gold_prompt_has_instrument_block_and_real_risk(self):
        engine = ExitDecisionEngine(provider="rule-based")
        pos = make_pos(symbol="XAUUSD_raw", entry_price=2400.0, current_price=2398.0,
                       lots=0.2, remaining_lots=0.2, sl=2408.0, spread_pips=12.0,
                       max_loss_usd=1000.0, risk_usd=160.0, margin_capped=True,
                       planned_exit_minutes=10)
        prompt = engine._build_prompt(pos)
        assert "INSTRUMENT: XAUUSD (metal, quoted in USD)" in prompt
        assert "1 pip = $0.10" in prompt
        assert "current spread 12.0 pips = 1.20 price units" in prompt
        assert "Break-even buffer on this instrument is at least 0.50 price units" in prompt
        assert "Risk at the broker stop for this trade: $160.00 (lot was CAPPED by free margin)" in prompt
        assert "panel max loss $1000.00 (forced close at -$1000.00)" in prompt
        assert "planned the exit around T+10 min" in prompt
        assert "fractions of the risk at the stop ($160)" in prompt

    def test_gold_prompt_without_echo_shows_budget_line(self):
        engine = ExitDecisionEngine(provider="rule-based")
        pos = make_pos(symbol="XAUUSD", entry_price=2400.0, sl=2408.0, max_loss_usd=1000.0)
        prompt = engine._build_prompt(pos)
        assert "- Risk budget for this trade: $1000.00" in prompt
        assert "INSTRUMENT: XAUUSD" in prompt


# ---------------------------------------------------------------------------
# rule-based fallback scales with risk
# ---------------------------------------------------------------------------

class TestRuleFallbackScaling:
    def _engine(self):
        return ExitDecisionEngine(provider="rule-based")

    def test_historical_100_budget_thresholds_unchanged(self):
        e = self._engine()
        # $31 > $30 -> BE move (forex, budget 100) exactly as before
        cmd = e._rule_based_decision(make_pos(profit_usd=31.0, max_loss_usd=100.0,
                                              current_price=0.6230))
        assert cmd.action == "MODIFY_SL" and "BE" in cmd.reason
        # $29 -> nothing
        cmd = e._rule_based_decision(make_pos(profit_usd=29.0, max_loss_usd=100.0))
        assert cmd.action == "HOLD"

    def test_large_budget_does_not_react_to_spread_noise(self):
        """Documented behaviour change for the production $1 000 budget
        (forex too): the fallback thresholds are 30/60/40/-20/15% of the
        budget = $300/$600/$400/-$200/$150 instead of the $100-era flat
        dollars, which at 3 lots were ~1-2 pips of spread noise."""
        e = self._engine()
        cmd = e._rule_based_decision(make_pos(profit_usd=31.0, max_loss_usd=1000.0,
                                              current_price=0.6230))
        assert cmd.action == "HOLD"
        cmd = e._rule_based_decision(make_pos(profit_usd=301.0, max_loss_usd=1000.0,
                                              current_price=0.6230))
        assert cmd.action == "MODIFY_SL"
        pos = make_pos(profit_usd=601.0, max_loss_usd=1000.0, sl_moved_to_be=True)
        assert e._rule_based_decision(pos).action == "PARTIAL_CLOSE"
        pos = make_pos(profit_usd=-150.0, max_loss_usd=1000.0,
                       open_time=utcnow() - timedelta(minutes=11))
        assert e._rule_based_decision(pos).action == "HOLD"       # -15% after 10 min: hold
        pos.profit_usd = -201.0
        assert e._rule_based_decision(pos).action == "CLOSE"      # -20%: cut

    def test_margin_capped_gold_uses_real_risk(self):
        e = self._engine()
        pos = make_pos(symbol="XAUUSD", direction="SELL", entry_price=2400.0,
                       current_price=2396.0, sl=2408.0, lots=0.2, remaining_lots=0.2,
                       max_loss_usd=1000.0, risk_usd=160.0, profit_usd=49.0)
        cmd = e._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"                  # 49 > 30% of 160 = 48
        assert cmd.sl_price == pytest.approx(2400.0 - 0.5)   # 5-pip ($0.50) BE buffer
        pos.open_time = utcnow() - timedelta(minutes=11)
        pos.profit_usd = -33.0                            # worse than -20% of 160
        assert e._rule_based_decision(pos).action == "CLOSE"


# ---------------------------------------------------------------------------
# routing alias + event policy + zones
# ---------------------------------------------------------------------------

class TestRoutingAliasAndPolicy:
    def test_alias_canonicalized(self):
        assert canonical_symbol("GOLD") == "XAUUSD"
        assert canonical_symbol("xauusd.pro") == "XAUUSD"
        assert canonical_symbol("usd/cad") == "USDCAD"
        assert cfg.normalize_instrument_routing("USD:GOLD") == {"USD": ["XAUUSD"]}
        assert cfg.normalize_instrument_routing("USD:GOLD,XAUUSD,USDCAD") == {
            "USD": ["XAUUSD", "USDCAD"]}

    def test_event_policy_exposed(self, monkeypatch):
        extra, skip = event_policy_for("XAUUSD")
        assert "Core PCE Price Index" in extra and "PPI" in extra
        assert "New Home Sales" in skip and "Existing Home Sales" in skip
        assert event_policy_for("NZDUSD") == ((), ())
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        assert cfg.routed_event_policy("usd")[1] == skip
        assert cfg.routed_event_policy("NZD") == ((), ())
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["USDCAD", "XAUUSD"]})
        assert cfg.routed_event_policy("USD") == ((), ())   # first route is forex

    def _ev(self, name, currency="USD"):
        return SimpleNamespace(event_name=name, currency=currency, source="forexfactory",
                               datetime_utc=utcnow() + timedelta(hours=1))

    def test_tradeable_predicate_applies_policy_only_to_routed_currency(self, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        params = CalendarAggregator._tradeable_params()
        assert "USD" in params["event_policies"]
        now = utcnow()
        kw = list(cfg.HIGH_IMPACT_EVENTS)
        ok = lambda ev: CalendarAggregator._event_is_tradeable(ev, kw, now, **params)
        assert ok(self._ev("Core PCE Price Index m/m")) is True       # extra
        assert ok(self._ev("PPI m/m")) is True                        # extra
        assert ok(self._ev("New Home Sales")) is False                # skipped on gold
        assert ok(self._ev("CPI m/m")) is True                        # whitelist as before
        assert ok(self._ev("Core PCE Price Index m/m", "CAD")) is False   # CAD not routed
        assert ok(self._ev("New Home Sales", "CAD")) is True         # CAD keeps whitelist

    def test_trade_all_still_respects_skip_list(self, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", True)
        params = CalendarAggregator._tradeable_params()
        now = utcnow()
        ok = lambda ev: CalendarAggregator._event_is_tradeable(ev, [], now, **params)
        assert ok(self._ev("Existing Home Sales")) is False
        assert ok(self._ev("Factory Orders m/m")) is True

    def test_no_routing_is_byte_identical(self, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {})
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        params = CalendarAggregator._tradeable_params()
        assert params["event_policies"] == {}
        now = utcnow()
        kw = list(cfg.HIGH_IMPACT_EVENTS)
        ok = lambda ev: CalendarAggregator._event_is_tradeable(ev, kw, now, **params)
        assert ok(self._ev("New Home Sales")) is True
        assert ok(self._ev("Core PCE Price Index m/m")) is False

    def test_zone_config_substitution(self):
        base = dict(cfg.ZONE_CONFIG)
        assert zone_config_for("NZDUSD", base) is base
        gold = zone_config_for("XAUUSD_raw", base)
        assert gold is not base
        assert gold["equal_level_tolerance_pips"] == 15.0
        assert gold["min_fvg_size_pips"] == 10.0
        assert gold["lookback_bars"] == base["lookback_bars"]

    def test_gold_profile_ranges_and_inputs(self):
        prof = profile_for("XAUUSD")
        assert prof.sl_range == (60.0, 120.0)
        assert prof.ea_inputs["InpExtremeSpreadPips"] == 30
        assert prof.ea_inputs["InpMinSLPips"] == 60 and prof.ea_inputs["InpMaxSLPips"] == 120
        assert prof.ea_inputs["InpUseSpreadLotReduction"] == 0


# ---------------------------------------------------------------------------
# clamp warning
# ---------------------------------------------------------------------------

class TestClampWarning:
    def test_warns_only_for_profiled_out_of_range(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(lde.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))
        lim_gold = {"sl": (60.0, 120.0), "tp": (30.0, 400.0), "units": "pips ($0.10)"}
        lde.LLMDecisionEngine._log_clamped_units("XAUUSD", 8.0, 150.0, lim_gold)
        assert len(warnings) == 1 and "stop_loss_pips=8" in warnings[0]
        lde.LLMDecisionEngine._log_clamped_units("XAUUSD", 80.0, 0.0, lim_gold)
        assert len(warnings) == 1
        lim_fx = {"sl": (25.0, 80.0), "tp": (8.0, 120.0), "units": "pips"}
        lde.LLMDecisionEngine._log_clamped_units("NZDUSD", 150.0, 300.0, lim_fx)
        assert len(warnings) == 1
