"""
Event -> instrument routing (multi-instrument PR3).

Contract under test:
* routing OFF (default) => decision pair / market context / prompt are
  byte-identical to the DEFAULT_PAIRS flow;
* routing ON for a currency => the FIRST routed symbol whose EA chart has
  pushed FRESH market data claims the decision (exact root match, no
  base-currency fallback); stale or absent routed charts fall through to
  today's flow;
* the LLM prompt for a routed non-forex instrument carries an INSTRUMENT
  block (units, ranges, direction semantics) — and NO such block for forex;
* /api/signal serves the routed decision to the EA polling that symbol
  (suffix-tolerant) and refuses it to the forex chart;
* /api/config/routing GET/POST validates and persists.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import config as cfg
import server
from timeutil import utcnow, utc_epoch
import llm_decision_engine as lde
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_bars(step_min, count, now_epoch, base=1.36, tick=0.0002):
    step = step_min * 60
    start = (now_epoch // 60) * 60 - (count - 1) * step
    bars, price = [], base
    for i in range(count):
        o, c = price, price + tick
        bars.append({"time": start + i * step, "open": round(o, 5),
                     "high": round(c + tick / 2, 5), "low": round(o - tick / 2, 5),
                     "close": round(c, 5)})
        price = c
    return bars


def push(client, pair, base=1.36, tick=0.0002, age_seconds=0, pip_size=None):
    now_epoch = utc_epoch(utcnow()) - age_seconds
    payload = {
        "pair": pair,
        "ohlc_multi": {"M1": make_bars(1, 60, now_epoch, base, tick),
                       "M5": make_bars(5, 30, now_epoch, base, tick),
                       "M15": make_bars(15, 20, now_epoch, base, tick),
                       "H1": make_bars(60, 20, now_epoch, base, tick)},
        "spread_points": 25, "spread_pips": 2.5,
    }
    if pip_size is not None:
        payload["pip_size"] = pip_size          # EA >= 17.08 echoes its effective pip
    resp = client.post('/api/market-data', json=payload)
    assert resp.get_json()["status"] == "ok"
    if age_seconds:
        with server.market_data_lock:
            key = [k for k in server.market_data_reports
                   if k.upper().startswith(pair.split('.')[0].upper()[:6])][0]
            server.market_data_reports[key]["updated_at"] = (
                utcnow() - timedelta(seconds=age_seconds)).isoformat()


def usd_event(seconds=90):
    return SimpleNamespace(event_name="CPI m/m", currency="USD",
                           datetime_utc=utcnow() + timedelta(seconds=seconds),
                           impact="HIGH", forecast="0.3%", previous="0.2%",
                           source="forexfactory")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Isolated server globals + routing OFF by default."""
    monkeypatch.setattr(server, "ensure_services", lambda: None)
    monkeypatch.setattr(server, "market_data_reports", {})
    monkeypatch.setattr(server, "zone_reports", {})
    monkeypatch.setattr(server, "registered_pairs", {})
    monkeypatch.setattr(server, "executed_trades", set())
    monkeypatch.setattr(server, "_last_served_signal", None)
    monkeypatch.setattr(server, "_signal_served_log_key", None)
    monkeypatch.setattr(server, "next_decision", None)
    monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {})
    saved = []
    monkeypatch.setattr(cfg, "save_runtime_overrides", lambda d: saved.append(d))
    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        yield SimpleNamespace(client=client, saved=saved, tmp=tmp_path)


def make_engine(tmp_path):
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trades.jsonl"))
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


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------

class TestRoutingConfig:
    def test_parse_env_string(self):
        assert cfg.parse_instrument_routing("USD:XAUUSD;NZD:NZDUSD,XAUUSD") == {
            "USD": ["XAUUSD"], "NZD": ["NZDUSD"]}          # XAUUSD does not carry NZD -> dropped
        assert cfg.parse_instrument_routing("") == {}
        assert cfg.parse_instrument_routing("junk;;USD") == {}
        assert cfg.parse_instrument_routing("usd: xau/usd , xauusd") == {"USD": ["XAUUSD"]}
        assert cfg.parse_instrument_routing("USDX:XAUUSD") == {}
        # non-strict paths (env/file) DROP unprofiled non-forex and non-leg symbols
        assert cfg.parse_instrument_routing("USD:NAS100,XAUUSD") == {"USD": ["XAUUSD"]}
        assert cfg.parse_instrument_routing("GBP:XAUUSD") == {}

    def test_normalize_strict_raises(self):
        with pytest.raises(ValueError, match="no instrument profile"):
            cfg.normalize_instrument_routing({"USD": ["NAS100"]}, strict=True)
        with pytest.raises(ValueError, match="does not carry"):
            cfg.normalize_instrument_routing("GBP:XAUUSD", strict=True)
        with pytest.raises(ValueError, match="bad currency key"):
            cfg.normalize_instrument_routing({"US": ["XAUUSD"]}, strict=True)
        assert cfg.normalize_instrument_routing({"usd": "xau/usd, USDCAD"}, strict=True) == {
            "USD": ["XAUUSD", "USDCAD"]}
        assert cfg.normalize_instrument_routing(None, strict=True) == {}

    def test_routing_candidates_reads_live_table(self, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        assert cfg.routing_candidates("usd") == ["XAUUSD"]
        assert cfg.routing_candidates("NZD") == []

    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("SKYTOWER_INSTRUMENT_ROUTING", raising=False)
        assert cfg.parse_instrument_routing(os.getenv("SKYTOWER_INSTRUMENT_ROUTING", "")) == {}


# ---------------------------------------------------------------------------
# server: routed market entry + market context
# ---------------------------------------------------------------------------

class TestRoutedMarketContext:
    def test_routing_off_is_default_pairs_flow(self, world):
        push(world.client, "USDCAD.pro")
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)
        ctx = server._build_market_context_for_event(usd_event())
        assert ctx is not None and ctx["pair"] == "USDCAD"

    def test_routed_symbol_with_fresh_data_wins(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "USDCAD.pro")
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.10)   # configured chart
        ctx = server._build_market_context_for_event(usd_event())
        assert ctx["pair"] == "XAUUSD"
        # forex NZD event untouched by a USD-only routing table
        nzd = SimpleNamespace(event_name="Official Cash Rate", currency="NZD",
                              datetime_utc=utcnow() + timedelta(seconds=90),
                              impact="HIGH", forecast="", previous="", source="forexfactory")
        push(world.client, "NZDUSD")
        assert server._build_market_context_for_event(nzd)["pair"] == "NZDUSD"

    def test_routed_symbol_without_data_falls_back(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "USDCAD.pro")
        ctx = server._build_market_context_for_event(usd_event())
        assert ctx["pair"] == "USDCAD"

    def test_stale_routed_data_falls_back(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "USDCAD.pro")
        push(world.client, "XAUUSD", base=2400.0, tick=0.2,
             age_seconds=server.MARKET_DATA_MAX_AGE_SECONDS + 60)
        ctx = server._build_market_context_for_event(usd_event())
        assert ctx["pair"] == "USDCAD"

    def test_routing_order_and_no_base_currency_fallback(self, world, monkeypatch):
        # US500 first but not pushed; XAUUSD second and fresh -> XAUUSD.
        # 'USDJPY' pushed must never satisfy the routed 'US500' by prefix.
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["US500", "XAUUSD"]})
        push(world.client, "USDJPY", base=150.0, tick=0.02)
        push(world.client, "XAUUSD", base=2400.0, tick=0.2, pip_size=0.10)
        assert server._pick_routed_market_entry("USD")[0] == "XAUUSD"
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["US500"]})
        assert server._pick_routed_market_entry("USD") is None


# ---------------------------------------------------------------------------
# engine prompt: INSTRUMENT block only for non-forex
# ---------------------------------------------------------------------------

class TestPromptInstrumentBlock:
    def test_forex_prompt_has_no_instrument_block(self, tmp_path):
        engine = make_engine(tmp_path)
        ctx = engine._gather_data(usd_event(), {"pair": "USDCAD"})
        assert "instrument" not in ctx
        prompt = engine._entry_prompt(ctx)
        assert "INSTRUMENT (this decision is for a NON-forex CFD" not in prompt
        assert "SUGGESTED PAIR: USDCAD\n" in prompt

    def test_gold_prompt_has_instrument_block(self, tmp_path):
        engine = make_engine(tmp_path)
        ctx = engine._gather_data(usd_event(), {"pair": "XAUUSD"})
        inst = ctx["instrument"]
        assert inst["name"] == "XAUUSD" and inst["pip_size"] == 0.10
        assert "SELL XAUUSD" in inst["direction_semantics"]
        prompt = engine._entry_prompt(ctx)
        assert "INSTRUMENT (this decision is for a NON-forex CFD" in prompt
        assert "stop_loss_pips: 50-100 (= 5-10 price units)" in prompt
        assert "1 pip = $0.10" in prompt
        # replay contract: the block is re-renderable from data_summary alone
        assert engine._entry_prompt(json.loads(json.dumps(ctx))) == prompt


# ---------------------------------------------------------------------------
# signal contract: routed decision reaches only the routed chart
# ---------------------------------------------------------------------------

class _PM:
    @staticmethod
    def can_open_trade():
        return True, "OK"


def _gold_decision():
    event_time = utcnow() + timedelta(seconds=60)
    return SimpleNamespace(
        event="CPI m/m", currency="USD", pair="XAUUSD", direction="SELL",
        confidence=0.7, lot_percent=70, entry_seconds_before=15,
        exit_minutes_after=10, stop_loss_percent=40, stop_loss_pips=90,
        take_profit_pips=150, reasoning="hot cpi", forced=False,
        decision_id="route-test",
        data_summary={"event": {"datetime": event_time.isoformat(), "currency": "USD"}})


class TestSignalContractRouted:
    def test_signal_served_to_gold_chart_only(self, world, monkeypatch):
        monkeypatch.setattr(server, "position_manager", _PM())
        monkeypatch.setattr(server, "next_decision", _gold_decision())
        fx = world.client.get("/api/signal?pair=USDCAD.pro").get_json()
        assert fx["signal"] is False
        assert "Not selected" in fx["message"] and fx["selected_pair"] == "XAUUSD"
        gold = world.client.get("/api/signal?pair=XAUUSD.pro").get_json()
        assert gold["signal"] is True
        assert gold["pair"] == "XAUUSD" and gold["direction"] == "SELL"
        assert gold["stop_loss_pips"] == 90 and gold["take_profit_pips"] == 150
        assert gold["max_loss_usd"] > 0
        assert gold["event_currency"] == "USD"


# ---------------------------------------------------------------------------
# /api/config/routing
# ---------------------------------------------------------------------------

class TestRoutingEndpoint:
    def test_get_default(self, world):
        r = world.client.get("/api/config/routing").get_json()
        assert r["status"] == "ok" and r["instrument_routing"] == {}

    def test_post_dict_persists_and_reports_live(self, world):
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)
        r = world.client.post("/api/config/routing",
                              json={"instrument_routing": {"usd": ["xau/usd", "USDCAD"]}})
        body = r.get_json()
        assert r.status_code == 200, body
        assert cfg.INSTRUMENT_ROUTING == {"USD": ["XAUUSD", "USDCAD"]}
        assert world.saved == [{"instrument_routing": {"USD": ["XAUUSD", "USDCAD"]}}]
        live = body["live"]["USD"]
        assert live[0]["symbol"] == "XAUUSD" and live[0]["fresh"] is True and live[0]["profile"] == "XAUUSD"
        assert live[1]["symbol"] == "USDCAD" and live[1]["has_data"] is False

    def test_post_string(self, world):
        r = world.client.post("/api/config/routing",
                              json={"instrument_routing": "USD:XAUUSD"})
        assert r.status_code == 200 and cfg.INSTRUMENT_ROUTING == {"USD": ["XAUUSD"]}

    def test_post_rejects_unknown_non_forex_symbol(self, world):
        r = world.client.post("/api/config/routing",
                              json={"instrument_routing": {"USD": ["BTCUSD"]}})
        assert r.status_code == 400
        assert cfg.INSTRUMENT_ROUTING == {}
        assert world.saved == []

    def test_post_clear(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        r = world.client.post("/api/config/routing", json={"instrument_routing": {}})
        assert r.status_code == 200 and cfg.INSTRUMENT_ROUTING == {}


# ---------------------------------------------------------------------------
# Units invariant enforced at runtime (EA pip_size echo)
# ---------------------------------------------------------------------------

class TestUnitMismatchGuard:
    def test_routing_skips_chart_with_wrong_pip(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "USDCAD.pro", pip_size=0.0001)
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.01)   # override forgotten
        assert server._pick_routed_market_entry("USD") is None
        ctx = server._build_market_context_for_event(usd_event())
        assert ctx["pair"] == "USDCAD"
        # correct echo -> routed
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.10)
        assert server._pick_routed_market_entry("USD")[0] == "XAUUSD"

    def test_profiled_chart_without_echo_fails_closed(self, world, monkeypatch):
        # An EA build that echoes nothing on a PROFILED chart cannot have the
        # override -> unit unknown -> never routed to, never served
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)                # no pip_size field
        assert server._pick_routed_market_entry("USD") is None
        live = world.client.get("/api/config/routing").get_json()["live"]["USD"][0]
        assert live["unit_ok"] is False
        monkeypatch.setattr(server, "position_manager", _PM())
        monkeypatch.setattr(server, "next_decision", _gold_decision())
        r = world.client.get("/api/signal?pair=XAUUSD.pro").get_json()
        assert r["signal"] is False and "does not echo pip_size" in r["message"]

    def test_forex_chart_without_echo_is_not_blocked(self, world, monkeypatch):
        # forex keeps working with old EA builds (unit not verifiable -> None)
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"NZD": ["NZDUSD"]})
        push(world.client, "NZDUSD")
        assert server._pick_routed_market_entry("NZD")[0] == "NZDUSD"
        live = world.client.get("/api/config/routing").get_json()["live"]["NZD"][0]
        assert live["unit_ok"] is None
        monkeypatch.setattr(server, "position_manager", _PM())
        d = _gold_decision(); d.pair = "NZDUSD"
        monkeypatch.setattr(server, "next_decision", d)
        assert world.client.get("/api/signal?pair=NZDUSD").get_json()["signal"] is True

    def test_report_path_logs_unit_mismatch_once(self, world, monkeypatch, caplog):
        from position_manager import PositionManager
        pm = PositionManager(exit_engine=None)
        monkeypatch.setattr(server, "position_manager", pm)
        monkeypatch.setattr(server, "_unit_mismatch_reported", set())
        opened = {"ticket": 77, "symbol": "XAUUSD", "direction": "BUY", "entry_price": 2400.0,
                  "lots": 0.1, "sl": 2390.0, "tp": 0.0, "tick_value": 1.0, "account_balance": 5000.0,
                  "event_name": "CPI m/m", "max_loss_usd": 100.0}
        pm.on_position_opened(opened)
        report = {"ticket": 77, "symbol": "XAUUSD", "current_price": 2401.0, "remaining_lots": 0.1,
                  "sl": 2390.0, "tp": 0.0, "profit_usd": 5.0, "tick_value": 1.0,
                  "account_balance": 5000.0, "spread_pips": 5.0, "pip_size": 0.01}
        r1 = world.client.post("/api/position/report", json=report).get_json()
        r2 = world.client.post("/api/position/report", json=report).get_json()
        assert "has_command" in r1 and "has_command" in r2
        assert 77 not in server._unit_mismatch_reported and "77" in server._unit_mismatch_reported

    def test_signal_withheld_on_unit_mismatch(self, world, monkeypatch):
        monkeypatch.setattr(server, "position_manager", _PM())
        monkeypatch.setattr(server, "next_decision", _gold_decision())
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.01)
        r = world.client.get("/api/signal?pair=XAUUSD.pro").get_json()
        assert r["signal"] is False and "Unit mismatch" in r["message"]
        assert server._last_served_signal is None
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.1)
        r = world.client.get("/api/signal?pair=XAUUSD.pro").get_json()
        assert r["signal"] is True

    def test_forex_signal_withheld_when_forex_chart_reports_wrong_pip(self, world, monkeypatch):
        monkeypatch.setattr(server, "position_manager", _PM())
        d = _gold_decision()
        d.pair = "USDCAD"
        monkeypatch.setattr(server, "next_decision", d)
        push(world.client, "USDCAD.pro", pip_size=0.001)          # someone set an override on FX
        r = world.client.get("/api/signal?pair=USDCAD.pro").get_json()
        assert r["signal"] is False and "Unit mismatch" in r["message"]
        push(world.client, "USDCAD.pro", pip_size=0.0001)
        assert world.client.get("/api/signal?pair=USDCAD.pro").get_json()["signal"] is True

    def test_live_status_reports_unit_ok(self, world, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2, pip_size=0.01)
        live = world.client.get("/api/config/routing").get_json()["live"]["USD"][0]
        assert live["fresh"] is True and live["unit_ok"] is False and live["reported_pip_size"] == 0.01


# ---------------------------------------------------------------------------
# Asset-class isolation
# ---------------------------------------------------------------------------

class TestAssetClassIsolation:
    def test_cross_pair_excludes_gold_from_forex_decision(self, world):
        push(world.client, "USDCAD.pro")
        push(world.client, "NZDUSD")
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)
        briefs = server._build_cross_pair_summaries("USD", "USDCAD")
        assert briefs and all("XAUUSD" not in b for b in briefs)
        assert any("NZDUSD" in b for b in briefs)

    def test_cross_pair_for_gold_decision_labels_forex_units(self, world):
        push(world.client, "USDCAD.pro")
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)
        briefs = server._build_cross_pair_summaries("USD", "XAUUSD")
        assert briefs and all(b.endswith("[forex pips, not XAUUSD pips]") for b in briefs)

    def test_learned_stats_fallback_never_crosses_asset_class(self, tmp_path):
        engine = make_engine(tmp_path)
        stats = {"_meta": {"schema_version": 1}, "bundle_alias": {},
                 "events": {"USD|cpi m/m": {
                     "currency": "USD", "event_name": "CPI m/m", "n_releases": 40,
                     "span": ["2023-01", "2026-07"],
                     "pairs": {"XAUUSD": {"n": 40, "abs_move_5min": {"median": 78.7, "n": 40}}}}}}
        json.dump(stats, open(engine.learned_stats_file, "w"))
        # forex decision must not be shown gold's block
        assert engine._learned_stats_section("CPI m/m", "USD", "USDCAD") is None
        # gold decision sees its own block
        out = engine._learned_stats_section("CPI m/m", "USD", "XAUUSD.pro")
        assert out is not None and "XAUUSD" in out[0]

    def test_reaction_history_filters_by_asset_class(self, tmp_path):
        from event_reaction_history import EventReactionHistory
        h = EventReactionHistory(log_dir=str(tmp_path))
        base = {"event_name": "CPI m/m", "event_name_normalized": "cpi m/m", "currency": "USD",
                "surprise": "BEAT", "test": False, "move_1min_pips": 5.0}
        h._records = [dict(base, pair="XAUUSD", move_5min_pips=-85.0, event_time="2026-08-12T12:30:00"),
                      dict(base, pair="USDCAD", move_5min_pips=12.0, event_time="2026-07-15T12:30:00")]
        fx = h.summarize("CPI m/m", "USD", pair="USDCAD")
        assert "USDCAD" in fx and "XAUUSD" not in fx
        gold = h.summarize("CPI m/m", "USD", pair="XAUUSD")
        assert "XAUUSD" in gold and "USDCAD" not in gold
        assert "USDCAD" in h.summarize("CPI m/m", "USD") and "XAUUSD" in h.summarize("CPI m/m", "USD")

    def test_episodes_never_substitute_across_asset_class(self):
        from episode_retrieval import find_similar_episodes
        paths = [{"pair": "XAUUSD", "currency": "USD", "event_name_normalized": "cpi m/m",
                  "event_time": "2026-07-15T12:30:00", "move_5min_pips": 617.3, "test": False,
                  "regime": "hold"}]
        assert find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hold") == []
        assert len(find_similar_episodes(paths, "CPI m/m", "USD", "XAUUSD.pro", "hold")) == 1


class TestRoutingEndpointValidation:
    def test_post_rejects_non_leg_currency(self, world):
        r = world.client.post("/api/config/routing", json={"instrument_routing": "GBP:XAUUSD"})
        assert r.status_code == 400 and "does not carry" in r.get_json()["message"]
        assert cfg.INSTRUMENT_ROUTING == {} and world.saved == []
