"""
Instrument profiles (multi-instrument PR1): non-forex CFDs (XAUUSD, GER40,
US500) declare their own pip unit and risk envelope; every forex pair keeps
today's behaviour byte-for-byte. These tests pin BOTH halves:

* identity pins — forex pip math, LLM clamp literals, guardrail values,
  calibration spreads and path-recorder pair matching are unchanged for
  every configured forex pair;
* profile behaviour — the same call sites resolve the profile for a
  non-forex symbol (with and without broker suffix).
"""
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import instrument_profiles as ip
from instrument_profiles import (InstrumentProfile, profile_for, profile_value,
                                 register, is_forex_root, normalize_root)
from trading_units import forex_pip_size, pips_to_price, price_to_pips
from config import CURRENCY_PAIRS, TYPICAL_NEWS_SPREADS
import llm_decision_engine as lde
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory
from position_manager import PositionManager
from exit_decision_engine import ExitDecisionEngine, PositionCommand
from event_path_recorder import EventPathRecorder
import calibration


FOREX_PAIRS = sorted({p for pairs in CURRENCY_PAIRS.values() for p in pairs}
                     | {"EURUSD", "USDJPY", "GBPJPY", "EURJPY", "AUDNZD",
                        "USDCHF", "EURGBP", "NZDJPY", "CADJPY"})


# ---------------------------------------------------------------------------
# Registry / resolution
# ---------------------------------------------------------------------------

class TestRegistry:
    @pytest.mark.parametrize("sym", ["XAUUSD", "xauusd", "XAUUSD.pro", "XAUUSD_SB",
                                     "GOLD", "XAU/USD"])
    def test_gold_resolves_with_any_suffix_or_alias(self, sym):
        prof = profile_for(sym)
        assert prof is not None and prof.name == "XAUUSD"
        assert prof.pip_size == 0.10

    @pytest.mark.parametrize("sym", ["GER40", "GER40.cash", "DE40", "ger40-mini"])
    def test_dax_resolves(self, sym):
        prof = profile_for(sym)
        assert prof is not None and prof.name == "GER40" and prof.pip_size == 1.0

    def test_us500_resolves(self):
        assert profile_for("US500").name == "US500"
        assert profile_for("US500.cash").name == "US500"

    @pytest.mark.parametrize("pair", FOREX_PAIRS + ["EURUSD.pro", "USDCAD_SB", "usd/cad", "usdjpy.r"])
    def test_every_forex_pair_has_no_profile(self, pair):
        assert profile_for(pair) is None
        assert is_forex_root(pair)

    def test_unknown_and_empty(self):
        assert profile_for("") is None
        assert profile_for(None) is None
        assert profile_for("BTCUSD") is None

    def test_register_refuses_forex_pair(self):
        with pytest.raises(ValueError):
            register(InstrumentProfile(
                name="EURUSD", asset_class="metal", pip_size=1.0, units_label="x",
                price_digits=2, quote_currency="USD", base_tag="EUR",
                sl_range=(1, 2), tp_range=(1, 2)))
        assert profile_for("EURUSD") is None

    def test_profile_value_contract(self):
        # forex -> default; profile with field set -> value; field None -> default
        assert profile_value("NZDUSD", "max_hold_minutes", 30) == 30
        assert profile_value("GER40", "max_hold_minutes", 30) == 90
        assert profile_value("XAUUSD", "max_hold_minutes", 30) == 30      # None in profile
        assert profile_value("XAUUSD", "emergency_spread_pips", 15) == 40.0
        assert profile_value("XAUUSD", "no_such_field", "d") == "d"

    def test_normalize_root(self):
        assert normalize_root("xauusd.pro") == "XAUUSD"
        assert normalize_root("GER40.cash") == "GER40"
        assert normalize_root("usd/cad") == "USDCAD"

    def test_profiles_are_frozen(self):
        with pytest.raises(Exception):
            profile_for("XAUUSD").pip_size = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# trading_units hook
# ---------------------------------------------------------------------------

class TestPipSizeHook:
    @pytest.mark.parametrize("pair", FOREX_PAIRS + ["EURUSD.pro", "USDCAD_SB", "usd/cad"])
    def test_forex_pip_unchanged(self, pair):
        expected = 0.01 if pair.upper().replace("/", "").replace(".PRO", "")[3:6] == "JPY" else 0.0001
        assert forex_pip_size(pair) == expected

    def test_gold_and_index_pips(self):
        assert forex_pip_size("XAUUSD") == 0.10
        assert forex_pip_size("XAUUSD.pro") == 0.10
        assert forex_pip_size("GER40.cash") == 1.0
        assert forex_pip_size("US500") == 1.0

    def test_pips_to_price_roundtrip_gold(self):
        assert pips_to_price(80, "XAUUSD") == pytest.approx(8.0)      # $8 stop = 80 pips
        assert price_to_pips(0.6, "XAUUSD") == pytest.approx(6.0)     # $0.60 spread = 6 pips
        assert pips_to_price(30, "NZDUSD") == pytest.approx(0.0030)   # unchanged


# ---------------------------------------------------------------------------
# LLM engine clamps
# ---------------------------------------------------------------------------

def make_engine(tmp_path):
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trades.jsonl"),
    )
    engine.playbooks_file = str(tmp_path / "playbooks.json")
    engine.learned_stats_file = str(tmp_path / "learned_stats.json")
    engine.client = object()
    engine.model = "test-model"
    engine.cot_analyzer = Mock()
    engine.cot_analyzer.analyze_currency.return_value = {"signal": "BULLISH"}
    engine.sentiment = Mock()
    engine.sentiment.get_currency_sentiment.return_value = {"signal": "SHORT", "pairs_analyzed": 1}
    engine.reaction_history = Mock()
    engine.reaction_history.summarize.return_value = None
    engine.reaction_history.summarize_currency_fallback.return_value = None
    engine.reaction_history.get_matching.return_value = []
    return engine


def make_event():
    return SimpleNamespace(
        event_name="CPI m/m", currency="USD",
        datetime_utc=datetime.utcnow() + timedelta(minutes=30),
        impact="HIGH", forecast="0.3%", previous="0.2%")


def reply(sl, tp, exit_m=10, lot=70):
    return json.dumps({"reasoning": "r", "direction": "BUY", "confidence": 0.7,
                       "lot_percent": lot, "exit_minutes": exit_m,
                       "stop_loss_pips": sl, "take_profit_pips": tp,
                       "stop_loss_percent": 40})


class TestEngineClamps:
    def test_class_attribute_pins_equal_pre_profile_literals(self):
        assert LLMDecisionEngine.LOT_MAX == 85
        assert LLMDecisionEngine.EXIT_RANGE == (5, 15)
        assert LLMDecisionEngine.SL_RANGE == (25.0, 80.0)
        assert LLMDecisionEngine.TP_RANGE == (8.0, 120.0)

    def test_limits_for_forex_and_gold(self, tmp_path):
        engine = make_engine(tmp_path)
        fx = engine._limits_for("USD/CAD")
        assert (fx["sl"], fx["tp"], fx["exit"], fx["lot_max"]) == ((25.0, 80.0), (8.0, 120.0), (5, 15), 85)
        gold = engine._limits_for("XAUUSD")
        assert gold["sl"] == (50.0, 250.0) and gold["tp"] == (30.0, 400.0)
        assert engine._limits_for(None) == fx

    def test_single_call_clamps_forex_unchanged(self, tmp_path, monkeypatch):
        engine = make_engine(tmp_path)
        monkeypatch.setattr(lde, "ENSEMBLE_K", 1)
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", [])
        monkeypatch.setattr(lde, "FORCE_DECISION", False)
        engine._chat = lambda prompt: reply(sl=120, tp=300, exit_m=45, lot=99)
        ctx = engine._gather_data(make_event(), None)
        d = engine._llm_decision(make_event(), ctx)
        assert d.stop_loss_pips == 80.0 and d.take_profit_pips == 120.0
        assert d.exit_minutes_after == 15 and d.lot_percent == 85

    def test_single_call_clamps_gold_use_profile(self, tmp_path, monkeypatch):
        engine = make_engine(tmp_path)
        monkeypatch.setattr(lde, "ENSEMBLE_K", 1)
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", [])
        monkeypatch.setattr(lde, "FORCE_DECISION", False)
        engine._chat = lambda prompt: reply(sl=120, tp=300, exit_m=12)
        ctx = engine._gather_data(make_event(), {"pair": "XAUUSD"})
        assert ctx["suggested_pair"] == "XAUUSD"
        d = engine._llm_decision(make_event(), ctx)
        assert d.pair == "XAUUSD"
        assert d.stop_loss_pips == 120.0 and d.take_profit_pips == 300.0   # $12 / $30 kept
        assert d.exit_minutes_after == 12

    def test_ensemble_clamps_gold_use_profile(self, tmp_path, monkeypatch):
        engine = make_engine(tmp_path)
        monkeypatch.setattr(lde, "ENSEMBLE_K", 3)
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", [])
        monkeypatch.setattr(lde, "FORCE_DECISION", False)
        engine._chat = lambda prompt: reply(sl=150, tp=20)
        ctx = engine._gather_data(make_event(), {"pair": "XAUUSD.pro"})
        d = engine._llm_decision(make_event(), ctx)
        assert d.stop_loss_pips == 150.0          # forex would clamp to 80
        assert d.take_profit_pips == 30.0         # gold TP floor $3 (forex floor 8 would keep 20)


# ---------------------------------------------------------------------------
# Position manager guardrails
# ---------------------------------------------------------------------------

def _pos_data(symbol, **over):
    d = {"ticket": 1, "symbol": symbol, "direction": "BUY", "entry_price": 2400.00,
         "lots": 0.10, "sl": 2390.0, "tp": 0.0, "tick_value": 1.0,
         "account_balance": 5000.0, "event_name": "CPI m/m", "max_loss_usd": 100.0}
    d.update(over)
    return d


def _report(**over):
    d = {"ticket": 1, "current_price": 2401.0, "remaining_lots": 0.10, "sl": 2390.0,
         "tp": 0.0, "profit_usd": 5.0, "tick_value": 1.0, "account_balance": 5000.0,
         "spread_pips": 20.0, "zone_bias": 0.0}
    d.update(over)
    return d


class TestGuardrailsUseProfile:
    def test_forex_emergency_spread_unchanged(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(_pos_data("NZDUSD", entry_price=0.62, sl=0.617))
        # 20 pips >= 15 (forex default) -> first breach counted, second closes
        r1 = pm.update_position(_report(current_price=0.621, sl=0.617, spread_pips=20.0))
        r2 = pm.update_position(_report(current_price=0.621, sl=0.617, spread_pips=20.0))
        cmds = [r for r in (r1, r2) if r and r.get("has_command")]
        assert any(c["command"]["action"] == "CLOSE" and "spread" in c["command"]["reason"].lower()
                   for c in cmds)

    def test_gold_emergency_spread_uses_profile_threshold(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(_pos_data("XAUUSD"))
        # 20 pips = $2.00 spread on gold: below the profile's 40 -> no emergency close
        for _ in range(3):
            r = pm.update_position(_report(spread_pips=20.0))
            assert not (r and r.get("has_command") and r["command"]["action"] == "CLOSE"), r
        # 45 pips (>= 40) twice -> close
        pm.update_position(_report(spread_pips=45.0))
        r = pm.update_position(_report(spread_pips=45.0))
        assert r["has_command"] and r["command"]["action"] == "CLOSE"

    def test_dax_max_hold_from_profile(self, monkeypatch):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(_pos_data("GER40", entry_price=18000.0, sl=17950.0))
        pos = pm.position
        pos.open_time = pos.open_time - timedelta(minutes=45)
        # 45 min open: forex would close at 30, DAX profile allows 90
        r = pm.update_position(_report(current_price=18010.0, sl=17950.0, spread_pips=2.0))
        assert not (r and r.get("has_command") and "hold" in r["command"]["reason"].lower())
        pos.open_time = pos.open_time - timedelta(minutes=50)   # now 95 min
        r = pm.update_position(_report(current_price=18010.0, sl=17950.0, spread_pips=2.0))
        assert r["has_command"] and r["command"]["action"] == "CLOSE" and "hold" in r["command"]["reason"].lower()

    def test_forex_max_hold_unchanged(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(_pos_data("NZDUSD", entry_price=0.62, sl=0.617))
        pm.position.open_time = pm.position.open_time - timedelta(minutes=31)
        r = pm.update_position(_report(current_price=0.621, sl=0.617, spread_pips=2.0))
        assert r["has_command"] and r["command"]["action"] == "CLOSE" and "hold" in r["command"]["reason"].lower()


# ---------------------------------------------------------------------------
# Exit engine units
# ---------------------------------------------------------------------------

def _open_position(symbol, **over):
    from position_manager import OpenPosition
    now = datetime.utcnow()
    d = dict(ticket=1, symbol=symbol, direction="BUY", entry_price=2400.0, current_price=2405.0,
             lots=0.1, remaining_lots=0.1, sl=2390.0, tp=0.0, profit_usd=50.0, max_profit_usd=50.0,
             max_drawdown_usd=0.0, tick_value=1.0, account_balance=5000.0, open_time=now,
             last_update=now, event_name="CPI m/m", entry_reasoning="", spread_pips=5.0,
             zone_bias=0.0, nearest_resistance=0.0, nearest_support=0.0, ai_decisions=[],
             partial_closed=False, sl_moved_to_be=False)
    d.update(over)
    return OpenPosition(**d)


class TestExitEngineUnits:
    def test_modify_tp_band_gold_accepts_dollar_scale_target(self):
        pos = _open_position("XAUUSD")
        cmd = PositionCommand(action="MODIFY_TP", tp_price=2425.0, reason="tp")   # $20 above
        out = ExitDecisionEngine._validate_modify_tp(cmd, pos)
        assert out.action == "MODIFY_TP"     # with the forex 0.0001 pip this was demoted to HOLD

    def test_modify_tp_band_forex_unchanged(self):
        pos = _open_position("NZDUSD", entry_price=0.62, current_price=0.6215, sl=0.617)
        ok = ExitDecisionEngine._validate_modify_tp(
            PositionCommand(action="MODIFY_TP", tp_price=0.6250, reason="tp"), pos)
        assert ok.action == "MODIFY_TP"
        bad = ExitDecisionEngine._validate_modify_tp(
            PositionCommand(action="MODIFY_TP", tp_price=15.0, reason="tp"), pos)   # pips as price
        assert bad.action == "HOLD"

    def test_rule_be_buffer_gold_is_50_cents(self):
        eng = ExitDecisionEngine(provider="rule-based")
        pos = _open_position("XAUUSD", profit_usd=35.0, current_price=2408.0)
        cmd = eng._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"
        assert cmd.sl_price == pytest.approx(2400.0 + 0.5)

    def test_rule_be_buffer_forex_is_1_pip(self):
        eng = ExitDecisionEngine(provider="rule-based")
        pos = _open_position("NZDUSD", entry_price=0.62, current_price=0.6230, sl=0.617, profit_usd=35.0)
        cmd = eng._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"
        assert cmd.sl_price == pytest.approx(0.6201)


# ---------------------------------------------------------------------------
# Calibration + path recorder
# ---------------------------------------------------------------------------

class TestCalibrationAndRecorder:
    def test_news_spread_forex_unchanged(self):
        for pair, spec in TYPICAL_NEWS_SPREADS.items():
            assert calibration.news_spread_pips(pair) == float(spec["news"])
        assert calibration.news_spread_pips("XXXYYY") == calibration.DEFAULT_NEWS_SPREAD_PIPS

    def test_news_spread_gold_from_profile(self):
        assert calibration.news_spread_pips("XAUUSD.pro") == 12.0
        assert calibration.news_spread_pips("GER40") == 4.0

    def test_fresh_pairs_matches_profiles_by_quote_currency(self):
        now = datetime.utcnow()
        snap = {p: {"received_at": now.isoformat()} for p in
                ["NZDUSD", "USDCAD.pro", "XAUUSD.pro", "US500", "GER40.cash", "EURJPY"]}
        # entry_age_seconds reads 'received_at' — make the helper robust to its exact key
        import event_path_recorder as epr
        original = epr.entry_age_seconds
        epr.entry_age_seconds = lambda entry, n: 0.0
        try:
            usd = EventPathRecorder._fresh_pairs(snap, "USD", now, 180)
            eur = EventPathRecorder._fresh_pairs(snap, "EUR", now, 180)
            jpy = EventPathRecorder._fresh_pairs(snap, "JPY", now, 180)
        finally:
            epr.entry_age_seconds = original
        assert set(usd) == {"NZDUSD", "USDCAD", "XAUUSD", "US500"}
        assert set(eur) == {"GER40", "EURJPY"}
        assert set(jpy) == {"EURJPY"}
