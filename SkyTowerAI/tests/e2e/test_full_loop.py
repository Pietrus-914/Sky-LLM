"""
END-TO-END proof of the full trading + learning loop, in-process.

One test class walks the ENTIRE lifecycle through the real server code
(only the LLM network calls and flaky external feeds are scripted):

  EA pushes M1 candles -> fake calendar event enters the preload window ->
  the REAL updater sequence (the idle-scan half of PHASE 1 + PHASE 2-3
  mirrored from background_decision_updater; keep them in sync) analyzes it
  with the K=3 ensemble -> decision recorded + full context dumped ->
  EA polls /api/signal and gets every protocol key incl. decision_id ->
  position opened/reported/closed with the decision_id echo -> trade lands
  in trade_history with lineage -> a post-trade reflection is journaled ->
  the EA reports the event reaction (echo) -> the measured event path
  produces a calibration entry -> A SECOND analysis of the same event type
  SEES all of it in its prompt (episode with "you then chose BUY ->
  CORRECT, realized $", the reflection under quarantine) -> a playbook
  proposal is distilled, operator-approved, and hot-reloads into the next
  prompt -> the replay harness scores the recorded episode.

Everything writes to tmp_path; the REAL knowledge/ files (learned stats,
playbooks copy) are used read-only so the test also proves the shipped
knowledge loads. No network. Runs in the normal suite.
"""
import json
import os
import queue
import shutil
import sys
import time as time_mod
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import server
import llm_decision_engine as lde
import llm_util
import config
from calendar_fetcher import EconomicEvent
from decision_history import DecisionHistory
from event_path_recorder import EventPathRecorder
from event_reaction_history import EventReactionHistory
from llm_decision_engine import LLMDecisionEngine
from playbook_distiller import PlaybookProposals
from position_manager import PositionManager
from reflections import ReflectionStore
from timeutil import utcnow, utc_epoch

PAIR = "USDCAD"
EVENT_NAME = "CPI m/m"


def llm_reply(direction="BUY", confidence=0.7):
    return json.dumps({
        "reasoning": "Beat likely; pre-drift down argues fade; stats support.",
        "stop_loss_pips": 40, "take_profit_pips": 60, "exit_minutes": 10,
        "lot_percent": 75, "direction": direction, "confidence": confidence,
    })


def make_bars(timeframe_minutes, count, now_epoch, base=1.3600):
    """Ascending OHLC bars ending at the current (broker==UTC) minute."""
    step = timeframe_minutes * 60
    start = (now_epoch // 60) * 60 - (count - 1) * step
    bars = []
    price = base
    for i in range(count):
        o = price
        c = price + 0.0002
        bars.append({"time": start + i * step, "open": round(o, 5),
                     "high": round(c + 0.0001, 5), "low": round(o - 0.0001, 5),
                     "close": round(c, 5)})
        price = c
    return bars


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Fresh, tmp-isolated server world wired exactly like init_services."""
    logs = tmp_path / "logs"
    logs.mkdir()

    # Real knowledge, tmp copy for the mutable playbooks
    real_knowledge = os.path.join(os.path.dirname(__file__), '..', '..',
                                  'python', 'knowledge')
    playbooks_file = str(tmp_path / "event_playbooks.json")
    shutil.copyfile(os.path.join(real_knowledge, 'event_playbooks.json'),
                    playbooks_file)

    decision_history = DecisionHistory(log_dir=str(tmp_path / "dh"))
    position_manager = PositionManager(
        exit_engine=None, history_file=str(logs / "trade_history.jsonl"))
    path_recorder = EventPathRecorder(log_dir=str(logs),
                                      regime_provider=lambda c: "hiking")

    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=decision_history,
        trade_history_file=str(logs / "trade_history.jsonl"),
        regime_provider=lambda c: "hiking",
        paths_provider=lambda: path_recorder.get_recent(2000),
    )
    engine.playbooks_file = playbooks_file
    engine.learned_stats_file = os.path.join(real_knowledge,
                                             'learned_stats.json')
    engine.reflection_store = ReflectionStore(str(logs / "reflections.jsonl"))
    engine.reaction_history = EventReactionHistory(log_dir=str(logs))
    engine.cot_analyzer = Mock()
    engine.cot_analyzer.analyze_currency.return_value = {
        "signal": "BULLISH", "confidence": 0.6}
    engine.sentiment = Mock()
    engine.sentiment.get_currency_sentiment.return_value = {
        "signal": "SHORT", "pairs_analyzed": 2}
    engine.client = object()          # LLM path; _chat is scripted per-test
    engine.model = "e2e-scripted"

    monkeypatch.setattr(lde, "FORCE_DECISION", False)
    monkeypatch.setattr(lde, "ENSEMBLE_K", 3)
    # This E2E DOES exercise reflections (with a scripted chat fn below) —
    # the suite-wide safety pin disables them, so re-enable here; also pin
    # the preload window so an operator SKYTOWER_PRELOAD_SECONDS < 120
    # cannot flake the +120s fake event out of the window
    monkeypatch.setattr(config, "REFLECTIONS_ENABLED", True)
    monkeypatch.setitem(config.TRADING_CONFIG, "preload_seconds", 150)

    saved = {name: getattr(server, name) for name in
             ("decision_engine", "decision_history", "position_manager",
              "path_recorder", "calendar", "next_decision",
              "_last_served_signal", "_playbook_proposals",
              "_reflection_chat_fn", "_reflection_chat_ready",
              "_fake_event", "_fake_event_initialized")}
    server.decision_engine = engine
    server.decision_history = decision_history
    server.position_manager = position_manager
    server.path_recorder = path_recorder
    server.calendar = Mock()
    server.calendar.get_tradeable_events.return_value = []
    server.next_decision = None
    server._last_served_signal = None
    server._playbook_proposals = PlaybookProposals(
        str(logs / "playbook_proposals.jsonl"))
    server._reflection_chat_fn = lambda s, u: (
        "Stop survived the release wick; verify spread at entry next time.")
    server._reflection_chat_ready = True
    server.analyzed_events.clear()
    server.executed_trades.clear()
    with server.market_data_lock:
        server.market_data_reports.clear()
    server.app.config['TESTING'] = True

    yield SimpleNamespace(engine=engine, decision_history=decision_history,
                          position_manager=position_manager,
                          path_recorder=path_recorder,
                          playbooks_file=playbooks_file, logs=str(logs),
                          client=server.app.test_client(), tmp=tmp_path)

    for name, value in saved.items():
        setattr(server, name, value)


def run_updater_once(world):
    """The updater's PHASE 1-3 for one iteration, mirrored from
    background_decision_updater (the loop scaffolding minus sleep)."""
    event_to_analyze = None
    with server.decision_lock:
        if server.next_decision is None:
            preload = config.TRADING_CONFIG["preload_seconds"]
            for event in server._get_next_unanalyzed_events():
                t = event.datetime_utc
                t = t.replace(tzinfo=None) if t.tzinfo else t
                until = (t - utcnow()).total_seconds()
                if until < -30:
                    continue
                if 0 < until <= preload:
                    event_to_analyze = event
                    break
                if until > preload:
                    break
    assert event_to_analyze is not None, "fake event not picked up"
    market_ctx = server._build_market_context_for_event(event_to_analyze)
    decision = server.decision_engine.analyze_event(event_to_analyze, market_ctx)
    server.decision_history.record(decision)
    with server.decision_lock:
        if decision.direction != "SKIP":
            server.next_decision = decision
        else:
            server._mark_event_analyzed(event_to_analyze)
    return decision, market_ctx


def inject_fake_event(seconds_ahead=120):
    event = EconomicEvent(
        datetime_utc=utcnow() + timedelta(seconds=seconds_ahead),
        currency="USD", event_name=EVENT_NAME, impact="HIGH",
        forecast="0.3%", previous="0.2%", source="fake-test")
    server._fake_event = event
    server._fake_event_initialized = True
    return event


class TestFullLoop:
    def test_complete_lifecycle_with_learning(self, world):
        client = world.client

        # ============ 1. EA pushes market data (with broker suffix) ======
        now_epoch = utc_epoch(utcnow())
        resp = client.post('/api/market-data', json={
            "pair": "USDCAD.pro",
            "ohlc_multi": {"M1": make_bars(1, 60, now_epoch),
                           "M5": make_bars(5, 30, now_epoch),
                           "M15": make_bars(15, 20, now_epoch),
                           "H1": make_bars(60, 20, now_epoch)},
            "spread_points": 25,
        })
        assert resp.get_json()["status"] == "ok"

        # ============ 2. Fake event enters the preload window ============
        event1 = inject_fake_event(120)

        # Scripted ensemble: 3 parallel calls, unanimous BUY, mixed conf
        replies = queue.Queue()
        for conf in (0.62, 0.71, 0.80):
            replies.put(llm_reply("BUY", conf))
        world.engine._chat = lambda prompt: replies.get_nowait()

        decision, market_ctx = run_updater_once(world)

        # Real analysis path produced an ensemble BUY with median confidence
        assert decision.direction == "BUY"
        assert decision.confidence == 0.71
        assert decision.ensemble["valid"] == 3
        assert market_ctx is not None and market_ctx.get("pair") == PAIR

        # Context dump exists and froze the full prompt inputs
        ctx_file = os.path.join(str(world.tmp / "dh"), "decision_context",
                                f"{decision.decision_id}.json")
        assert os.path.exists(ctx_file)
        dump = json.load(open(ctx_file, encoding="utf-8"))
        prompt1 = world.engine._entry_prompt(dump["data_summary"])
        # Real shipped learned_stats.json kicked in for USD CPI m/m.
        # DATA DEPENDENCY: needs events["USD|cpi m/m"] with n>=5 in
        # knowledge/learned_stats.json — if a regen ever thins it out, fix
        # the dataset (that would be a real knowledge regression), not this
        # assert.
        assert "LEARNED EVENT STATISTICS" in prompt1
        assert "KEY BASE RATES" in prompt1
        # No trades/paths yet -> no episodes/reflections on the first run
        assert "SIMILAR PAST EPISODES" not in prompt1
        assert "POST-TRADE REFLECTIONS" not in prompt1

        # ============ 3. EA polls the signal =============================
        resp = client.get(f'/api/signal?pair={PAIR}.pro')
        sig = resp.get_json()
        assert sig["signal"] is True
        # Every key the EA parses out of the signal JSON
        for key in ("decision_id", "direction", "pair", "lot_percent",
                    "max_loss_usd", "confidence", "entry_seconds_before",
                    "exit_minutes", "stop_loss_percent", "stop_loss_pips",
                    "take_profit_pips", "time_until_event", "event_time",
                    "event_name", "event_currency", "forced", "reasoning"):
            assert key in sig, f"EA protocol key missing: {key}"
        assert sig["decision_id"] == decision.decision_id
        assert sig["direction"] == "BUY" and sig["pair"] == PAIR
        assert sig["max_loss_usd"] > 0 and sig["forced"] is False

        # ============ 4. Position lifecycle with decision_id echo ========
        resp = client.post('/api/position/opened', json={
            "ticket": 777, "symbol": PAIR, "direction": "BUY",
            "entry_price": 1.3612, "lots": 0.5, "sl": 1.3572, "tp": 1.3672,
            "tick_value": 10.0, "account_balance": 5000.0,
            "event_name": EVENT_NAME, "decision_id": decision.decision_id})
        assert resp.get_json()["status"] == "ok"

        resp = client.post('/api/position/report', json={
            "ticket": 777, "current_price": 1.3630, "remaining_lots": 0.5,
            "sl": 1.3572, "tp": 1.3672, "profit_usd": 90.0,
            "tick_value": 10.0, "spread_pips": 2.5,
            "account_balance": 5090.0})
        assert "command" in resp.get_json()

        resp = client.post('/api/position/closed', json={
            "ticket": 777, "close_price": 1.3630, "profit": 90.0,
            "reason": "AI: TP region reached", "profit_source": "history",
            "decision_id": decision.decision_id})
        assert resp.get_json()["status"] == "ok"

        trade = world.position_manager.closed_trades[-1]
        assert trade["decision_id"] == decision.decision_id
        assert trade["forced"] is False and trade["profit_usd"] == 90.0

        # Reflection thread journals the trade (event currency via lineage)
        deadline = time_mod.time() + 3
        reflections = []
        while time_mod.time() < deadline and not reflections:
            reflections = world.engine.reflection_store.get_matching(
                EVENT_NAME, "USD")
            time_mod.sleep(0.05)
        assert reflections, "reflection was not journaled"
        assert reflections[0]["decision_id"] == decision.decision_id

        # ============ 5. EA reports the reaction (echo) ==================
        resp = client.post('/api/event-reaction', json={
            "pair": PAIR, "event_name": EVENT_NAME, "currency": "USD",
            "event_time": event1.datetime_utc.isoformat(),
            "decision_id": decision.decision_id,
            "price_at_event": 1.3612, "price_after_1min": 1.3621,
            "price_after_5min": 1.3630})
        assert resp.get_json()["reaction"]["decision_id"] == decision.decision_id

        # ============ 6. Measured path -> calibration + episodic memory ==
        minute_iso = event1.datetime_utc.replace(tzinfo=None).isoformat()[:19]
        world.path_recorder._append([{
            "recorded_at": utcnow().isoformat() + "Z", "source": "server_m1",
            "test": False,
            "event_key": f"USD|cpi m/m|{minute_iso[:16]}",
            "event_name": EVENT_NAME, "event_name_normalized": "cpi m/m",
            "currency": "USD", "event_time": minute_iso, "impact": "HIGH",
            "forecast": "0.3%", "previous": "0.2%", "actual": "0.4%",
            "surprise": "BEAT", "surprise_magnitude": 0.1, "non_data": False,
            "regime": "hiking", "pair": PAIR, "broker_utc_offset_min": 0,
            "data_status": "ok", "spread_points_t0": 25,
            "price_at_event": 1.3612, "move_1min_pips": 9.0,
            "move_5min_pips": 18.0, "move_15min_pips": 22.0,
            "move_30min_pips": 25.0, "first5_high_pips": 20.0,
            "first5_low_pips": -2.0, "high_30min_pips": 27.0,
            "low_30min_pips": -2.0, "pre_release_3m_pips": -4.0,
        }])

        cal = client.get('/api/calibration').get_json()["calibration"]
        assert cal["n_scored"] == 1
        assert cal["hit_rate"] == 1.0      # BUY vs +18 pips = correct

        # ============ 7. Second analysis SEES the learning ===============
        with server.decision_lock:
            server.next_decision = None
        # Opening the trade marked event1 as analyzed — that marker is what
        # stops the updater from paying for a SECOND ensemble on the same
        # release (2026-07-22 double-analysis fix). The next release is a new
        # event at a new time; the fake-event helper reuses the same name and
        # near-same minute, so simulate "new event" by clearing the marker.
        server.analyzed_events.clear()
        inject_fake_event(120)
        captured = {}

        def chat2(prompt):
            captured.setdefault("prompt", prompt)
            return llm_reply("BUY", 0.7)

        world.engine._chat = chat2
        decision2, _ = run_updater_once(world)
        prompt2 = captured["prompt"]
        assert "SIMILAR PAST EPISODES" in prompt2
        assert "you then chose BUY@71% -> CORRECT, realized $+90.00" in prompt2
        assert "POST-TRADE REFLECTIONS" in prompt2
        assert "QUARANTINED n=1" in prompt2
        assert "Stop survived the release wick" in prompt2
        assert decision2.decision_id != decision.decision_id

        # ============ 8. Playbook distillation + operator approval =======
        proposal_reply = json.dumps({
            "pattern": "E2E distilled: faded pre-drift in 62% (n=41).",
            "typical_behavior": "5-min median 17 pips (n=58).",
            "notes": "Hiking regime doubles the move (n=15).",
            "rationale": "Refreshes entry with measured rates."})
        real_make_chat = llm_util.make_chat_fn
        llm_util.make_chat_fn = lambda **kw: (lambda s, u: proposal_reply)
        orig_books = config.EVENT_PLAYBOOKS_FILE
        config.EVENT_PLAYBOOKS_FILE = world.playbooks_file
        try:
            resp = client.post('/api/playbooks/distill', json={
                "event_name": EVENT_NAME, "currency": "USD"})
            assert resp.get_json()["status"] == "ok"
            pid = resp.get_json()["proposal"]["id"]
            resp = client.post('/api/playbooks/proposals/decide', json={
                "id": pid, "action": "approve"})
            assert resp.get_json()["status"] == "ok"
        finally:
            llm_util.make_chat_fn = real_make_chat
            config.EVENT_PLAYBOOKS_FILE = orig_books

        books = json.load(open(world.playbooks_file, encoding="utf-8"))
        approved_key = next(k for k in books
                            if "cpi m/m" in k.lower() and "y/y" not in k.lower())
        assert books[approved_key]["pattern"].startswith("E2E distilled")
        # Hot-reload: the engine's next playbook lookup sees the new entry
        os.utime(world.playbooks_file)   # defeat same-second mtime caching
        section = world.engine._playbook_section(EVENT_NAME, "USD")
        assert "E2E distilled" in section

        # ============ 9. Replay harness scores the frozen episode ========
        from tools.replay_decisions import load_episodes, score_rows
        episodes, skipped = load_episodes(
            os.path.join(str(world.tmp / "dh"), "decision_context"),
            os.path.join(world.logs, "event_paths.jsonl"))
        assert len(episodes) >= 1
        baseline = score_rows(episodes)
        assert baseline["n"] >= 1 and baseline["hit_rate"] == 1.0

    def test_split_ensemble_skips_and_serves_no_signal(self, world):
        """Control run: a 2-1 ensemble split must SKIP and the EA must get
        signal:false — the agreement gate visible end-to-end."""
        client = world.client
        inject_fake_event(120)
        replies = queue.Queue()
        for d, c in (("BUY", 0.8), ("BUY", 0.9), ("SELL", 0.7)):
            replies.put(llm_reply(d, c))
        world.engine._chat = lambda prompt: replies.get_nowait()

        decision, _ = run_updater_once(world)
        assert decision.direction == "SKIP"
        assert "no unanimity" in decision.reasoning

        sig = client.get(f'/api/signal?pair={PAIR}').get_json()
        assert sig["signal"] is False
