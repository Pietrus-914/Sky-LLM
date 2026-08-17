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


def push(client, pair, base=1.36, tick=0.0002, age_seconds=0):
    now_epoch = utc_epoch(utcnow()) - age_seconds
    resp = client.post('/api/market-data', json={
        "pair": pair,
        "ohlc_multi": {"M1": make_bars(1, 60, now_epoch, base, tick),
                       "M5": make_bars(5, 30, now_epoch, base, tick),
                       "M15": make_bars(15, 20, now_epoch, base, tick),
                       "H1": make_bars(60, 20, now_epoch, base, tick)},
        "spread_points": 25, "spread_pips": 2.5,
    })
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
        assert cfg.parse_instrument_routing("USD:XAUUSD;GBP:XAUUSD,GBPUSD") == {
            "USD": ["XAUUSD"], "GBP": ["XAUUSD", "GBPUSD"]}
        assert cfg.parse_instrument_routing("") == {}
        assert cfg.parse_instrument_routing("junk;;USD") == {}
        assert cfg.parse_instrument_routing("usd: xau/usd , xauusd") == {"USD": ["XAUUSD"]}
        assert cfg.parse_instrument_routing("USDX:XAUUSD") == {}

    def test_routing_candidates_reads_live_table(self, monkeypatch):
        monkeypatch.setattr(cfg, "INSTRUMENT_ROUTING", {"USD": ["XAUUSD"]})
        assert cfg.routing_candidates("usd") == ["XAUUSD"]
        assert cfg.routing_candidates("NZD") == []
        assert cfg.routing_candidates("USD", {}) == []

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
        push(world.client, "XAUUSD.pro", base=2400.0, tick=0.2)
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
        push(world.client, "XAUUSD", base=2400.0, tick=0.2)
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
