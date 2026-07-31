"""
gpt_review stage C-2 (30.07.2026): position/updater/spread/calibration gaps.

Each behaviour here was verified against the code before being changed:
  * the updater retired an event for 24h on the FIRST exception, even one
    thrown by the AUDIT write after a valid BUY/SELL had been computed;
  * the exit engine's rule fallback judged every threshold on floating-only
    P/L while the guardrails and the LLM prompt use the whole trade;
  * partial-close realization ignored the exact realized_usd the EA already
    sends and estimated from the previous (stale, pre-fill) report;
  * the emergency-spread exit fired on ONE sampled tick at the same threshold
    that blocks entry, so a routine release spike liquidated at the worst quote;
  * the calibration prompt line spoke in the second person while being
    computed over other models' and other prompt versions' history.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from exit_decision_engine import ExitDecisionEngine
from position_manager import (PositionManager, OpenPosition, PositionCommand,
                              SPREAD_BREACHES_TO_CLOSE)


def make_position(**overrides):
    now = datetime.utcnow()
    defaults = dict(
        ticket=4242, symbol="USDCAD", direction="BUY",
        entry_price=1.3700, current_price=1.3690, lots=0.50,
        remaining_lots=0.50, sl=1.3660, tp=0.0,
        profit_usd=0.0, max_profit_usd=0.0, max_drawdown_usd=0.0,
        tick_value=10.0, account_balance=5000.0,
        open_time=now, last_update=now,
        event_name="CPI m/m", entry_reasoning="beat expected",
        max_loss_usd=100.0,
    )
    defaults.update(overrides)
    return OpenPosition(**defaults)


class TestFallbackUsesWholeTradePnl:
    """The fallback runs during an LLM outage — the same outage that degrades
    entries — so it must not misread a partially closed trade."""

    @pytest.fixture
    def engine(self):
        return ExitDecisionEngine(provider="rule-based")

    def test_cut_loss_counts_realized_partial(self, engine):
        """floating -$15 alone stayed above the -$20 rule, but the trade was
        really at -$65 and going nowhere."""
        pos = make_position(
            profit_usd=-15.0, realized_usd=-50.0,
            open_time=datetime.utcnow() - timedelta(minutes=15))
        cmd = engine._rule_based_decision(pos)
        assert cmd.action == "CLOSE"
        assert "-65" in cmd.reason or "$-65" in cmd.reason

    def test_unpartialed_trade_behaves_exactly_as_before(self, engine):
        """realized_usd is 0 before any partial close, so the thresholds are
        unchanged for the common case."""
        pos = make_position(
            profit_usd=-15.0,
            open_time=datetime.utcnow() - timedelta(minutes=15))
        assert engine._rule_based_decision(pos).action == "HOLD"

    def test_late_phase_close_counts_realized(self, engine):
        """A trade at -$60 total must not read as "minimal P/L" just because
        the remaining leg floats near zero."""
        pos = make_position(
            profit_usd=-5.0, realized_usd=-55.0,
            open_time=datetime.utcnow() - timedelta(minutes=25))
        cmd = engine._rule_based_decision(pos)
        # -60 total is neither "minimal" (rule 5) nor above -20 (rule 4)
        assert cmd.action == "CLOSE"
        assert "Losing" in cmd.reason

    def test_break_even_move_counts_realized_profit(self, engine):
        """Whole-trade threshold, but the OPEN leg must still be in profit and
        price above entry — otherwise an entry+1pip stop lands on the wrong
        side of the market."""
        pos = make_position(profit_usd=5.0, realized_usd=40.0,
                            current_price=1.3730)   # BUY, entry 1.3700
        cmd = engine._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"
        assert cmd.sl_price < pos.current_price

    def test_no_break_even_move_while_the_open_leg_is_underwater(self, engine):
        """Realized profit from a partial can satisfy the total while the
        remaining leg is losing — a break-even stop would then be placed above
        the market for a BUY, be rejected, and the server would still record
        the position as break-even-protected."""
        pos = make_position(profit_usd=-5.0, realized_usd=40.0,
                            current_price=1.3699)   # below entry 1.3700
        cmd = engine._rule_based_decision(pos)
        assert cmd.action != "MODIFY_SL"

    def test_no_break_even_move_when_price_sits_on_entry(self, engine):
        """Price exactly at entry leaves no room for a stop plus buffer."""
        pos = make_position(profit_usd=1.0, realized_usd=40.0,
                            current_price=1.3700)
        assert engine._rule_based_decision(pos).action != "MODIFY_SL"

    def test_sell_break_even_move_respects_the_market(self, engine):
        pos = make_position(direction="SELL", entry_price=1.3700,
                            current_price=1.3670, profit_usd=5.0,
                            realized_usd=40.0, sl=1.3740)
        cmd = engine._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"
        assert cmd.sl_price > pos.current_price


class TestEmergencySpreadDebounce:
    def _pm_with_position(self):
        pm = PositionManager(exit_engine=None)
        pm.position = make_position()
        return pm

    def test_single_spiked_report_does_not_liquidate(self):
        pm = self._pm_with_position()
        pm.position.spread_pips = 16.0
        assert pm._check_guardrails() is None
        assert pm._spread_breaches == 1

    def test_second_consecutive_breach_closes(self):
        pm = self._pm_with_position()
        pm.position.spread_pips = 16.0
        pm._check_guardrails()
        cmd = pm._check_guardrails()
        assert cmd is not None and cmd.action == "CLOSE"
        assert "spread" in cmd.reason

    def test_recovered_spread_resets_the_counter(self):
        pm = self._pm_with_position()
        pm.position.spread_pips = 16.0
        pm._check_guardrails()
        pm.position.spread_pips = 3.0
        assert pm._check_guardrails() is None
        assert pm._spread_breaches == 0
        # ...and the next spike starts counting from scratch
        pm.position.spread_pips = 16.0
        assert pm._check_guardrails() is None

    def test_catastrophic_spread_closes_immediately(self):
        """Waiting is the bigger risk once the spread is absurd."""
        pm = self._pm_with_position()
        pm.position.spread_pips = 40.0
        cmd = pm._check_guardrails()
        assert cmd is not None and cmd.action == "CLOSE"

    def test_debounce_threshold_is_low_enough_to_stay_protective(self):
        """A confirmation window of a couple of reports (5-15s each) must not
        turn into minutes of exposure."""
        assert 2 <= SPREAD_BREACHES_TO_CLOSE <= 3

    def test_reconciled_position_does_not_inherit_breaches(self):
        """The counter is manager-scoped: a position adopted after a reconnect
        must not be liquidated on its first wide tick because of breaches
        counted before it existed."""
        pm = PositionManager(exit_engine=None)
        snapshot = {
            "ticket": 4242, "position_id": "4242", "symbol": "USDCAD",
            "direction": "BUY", "entry_price": 1.3700, "lots": 1.0,
            "remaining_lots": 1.0, "max_loss_usd": 100.0, "tick_value": 10.0,
        }
        pm.on_position_opened(snapshot)
        pm._spread_breaches = SPREAD_BREACHES_TO_CLOSE - 1

        pm.on_position_opened(snapshot, recovered=True)   # reconcile branch

        assert pm._spread_breaches == 0

    def test_new_position_does_not_inherit_breaches(self):
        pm = PositionManager(exit_engine=None)
        pm.position = make_position()
        pm._spread_breaches = SPREAD_BREACHES_TO_CLOSE - 1
        pm.on_position_closed({"ticket": 4242, "profit": -5.0,
                               "close_price": 1.3690, "reason": "SL"})
        assert pm._spread_breaches == 0


class TestPartialCloseUsesBrokerRealized:
    def _pm(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened({
            "ticket": 4242, "position_id": "4242", "symbol": "USDCAD",
            "direction": "BUY", "entry_price": 1.3700, "lots": 1.0,
            "remaining_lots": 1.0, "max_loss_usd": 100.0, "tick_value": 10.0,
        })
        return pm

    def _report(self, **over):
        data = {"ticket": 4242, "position_id": "4242", "symbol": "USDCAD",
                "remaining_lots": 1.0, "profit_usd": 0.0}
        data.update(over)
        return data

    def test_broker_figure_wins_over_the_stale_estimate(self):
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-40.0))   # last floating
        # Half closed; the actual fill realized -$70, not -$20
        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-35.0,
                                       realized_usd=-70.0))
        assert pm.position.realized_usd == pytest.approx(-70.0)
        assert pm.position.partial_closed is True

    def test_estimate_still_used_when_the_ea_sends_nothing(self):
        """Older EA builds omit realized_usd; behaviour must not regress."""
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-40.0))
        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-35.0))
        assert pm.position.realized_usd == pytest.approx(-20.0)

    def test_lagging_deal_history_falls_back_then_resyncs(self):
        """MT5 can report the volume drop before the deal is in history, so a
        0.0 must not be trusted as "nothing was realized" — estimate first,
        then adopt the broker figure on a later report."""
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-40.0))
        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-35.0,
                                       realized_usd=0.0))
        assert pm.position.realized_usd == pytest.approx(-20.0)  # estimate

        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-30.0,
                                       realized_usd=-70.0))
        assert pm.position.realized_usd == pytest.approx(-70.0)  # truth

    def test_resync_ignores_an_unchanged_figure(self):
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-40.0))
        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-35.0,
                                       realized_usd=-70.0))
        pm.update_position(self._report(remaining_lots=0.5, profit_usd=-30.0,
                                       realized_usd=-70.0))
        assert pm.position.realized_usd == pytest.approx(-70.0)

    def test_guardrail_sees_the_broker_truth(self):
        """The whole point: max-loss must fire on real money, not an estimate."""
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-10.0))
        cmd = pm.update_position(self._report(
            remaining_lots=0.5, profit_usd=-20.0, realized_usd=-90.0))
        assert cmd["command"]["action"] == "CLOSE"
        assert "max loss" in cmd["command"]["reason"]


class TestTransientAnalysisFailureIsRetried:
    """One exception used to retire an event for 24h — including an exception
    from the AUDIT write that happens AFTER a valid BUY/SELL was computed. The
    decision was silently discarded while /health stayed green."""

    def test_retries_while_the_window_has_room(self):
        import server
        assert server._should_retry_analysis(attempts=1, retry_room_seconds=120)

    def test_gives_up_after_the_attempt_cap(self):
        import server
        cap = server.MAX_ANALYSIS_ATTEMPTS
        assert not server._should_retry_analysis(attempts=cap,
                                                retry_room_seconds=600)

    def test_gives_up_when_no_time_is_left(self):
        """Better a clean give-up than an analysis that lands after entry."""
        import server
        assert not server._should_retry_analysis(attempts=1,
                                                retry_room_seconds=5)

    def test_gives_up_on_an_event_already_in_the_past(self):
        import server
        assert not server._should_retry_analysis(attempts=1,
                                                retry_room_seconds=-30)

    def test_failure_counters_are_dropped_once_the_event_retires(self):
        import server
        from timeutil import utcnow
        server._analysis_failures.clear()
        server.analyzed_events.clear()
        server._analysis_failures["USD_CPI_20260730_1230"] = (2, utcnow())
        server._analysis_failures["USD_NFP_20260731_1230"] = (1, utcnow())
        server.analyzed_events["USD_CPI_20260730_1230"] = utcnow()

        server._cleanup_analysis_failures()

        assert "USD_CPI_20260730_1230" not in server._analysis_failures
        assert server._analysis_failures["USD_NFP_20260731_1230"][0] == 1
        server._analysis_failures.clear()
        server.analyzed_events.clear()

    def test_stale_counters_age_out_even_without_a_give_up(self):
        """An event whose retry was scheduled but whose window then closed is
        never marked analyzed, so "present in analyzed_events" alone would
        leak its counter forever on the 24/7 machine."""
        import server
        from datetime import timedelta
        from timeutil import utcnow
        server._analysis_failures.clear()
        server.analyzed_events.clear()
        server._analysis_failures["GBP_OLD_20260728_1100"] = (
            1, utcnow() - timedelta(hours=30))
        server._analysis_failures["GBP_NEW_20260730_1100"] = (1, utcnow())

        server._cleanup_analysis_failures()

        assert "GBP_OLD_20260728_1100" not in server._analysis_failures
        assert "GBP_NEW_20260730_1100" in server._analysis_failures
        server._analysis_failures.clear()


class TestCommandDeliveryIsConfirmed:
    """A command written into an HTTP response is not proof the EA executed
    it: the response can be lost after the server handled the request. The
    server keeps the last served command until a broker report shows its
    effect, re-sends it once if the effect is missing — but only for actions
    that are safe to repeat."""

    def _pm(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened({
            "ticket": 4242, "position_id": "4242", "symbol": "USDCAD",
            "direction": "BUY", "entry_price": 1.3700, "lots": 1.0,
            "remaining_lots": 1.0, "max_loss_usd": 100.0, "tick_value": 10.0,
        })
        return pm

    def _report(self, **over):
        data = {"ticket": 4242, "position_id": "4242", "symbol": "USDCAD",
                "remaining_lots": 1.0, "profit_usd": 0.0, "sl": 1.3660,
                "current_price": 1.3710}
        data.update(over)
        return data

    def test_unobserved_close_is_re_served_once(self):
        pm = self._pm()
        pm.pending_command = PositionCommand(action="CLOSE", reason="AI: out")

        first = pm.update_position(self._report())
        assert first["command"]["action"] == "CLOSE"

        # Still receiving reports => the close did not happen
        second = pm.update_position(self._report())
        assert second["command"]["action"] == "CLOSE"

        # After the retry it is dropped, and the model is consulted at once
        third = pm.update_position(self._report())
        assert third["command"]["action"] == "HOLD"
        assert pm._served_command is None
        assert pm.last_llm_check == 0.0

    def test_partial_close_is_never_re_served(self):
        """If it DID execute and we simply cannot see it yet, a second one
        would close another slice (two 50% legs = 75% of the position)."""
        pm = self._pm()
        pm.pending_command = PositionCommand(action="PARTIAL_CLOSE",
                                             close_percent=50)

        first = pm.update_position(self._report())
        assert first["command"]["action"] == "PARTIAL_CLOSE"

        second = pm.update_position(self._report(remaining_lots=1.0))
        assert second["command"]["action"] == "HOLD"
        assert pm._served_command is None

    def test_observed_partial_close_is_retired(self):
        pm = self._pm()
        pm.pending_command = PositionCommand(action="PARTIAL_CLOSE",
                                             close_percent=50)
        pm.update_position(self._report())

        pm.update_position(self._report(remaining_lots=0.5))
        assert pm._served_command is None
        assert pm.position.partial_closed is True

    def test_observed_modify_sl_is_retired(self):
        pm = self._pm()
        pm.pending_command = PositionCommand(action="MODIFY_SL",
                                             sl_price=1.3701)
        pm.update_position(self._report())

        pm.update_position(self._report(sl=1.3701))
        assert pm._served_command is None
        assert pm.position.sl_moved_to_be is True

    def test_unobserved_modify_sl_does_not_burn_an_extra_model_call(self):
        """A broker that keeps rejecting a stop must not bill a fresh exit
        consultation on every single report."""
        pm = self._pm()
        pm.last_llm_check = 12345.0
        pm.pending_command = PositionCommand(action="MODIFY_SL",
                                             sl_price=1.3701)
        pm.update_position(self._report())
        pm.update_position(self._report())      # re-served
        pm.update_position(self._report())      # dropped
        assert pm._served_command is None
        assert pm.last_llm_check == 12345.0


class TestManagementTrailRecordsTheEnding:
    """The "AI position management" trail is the story of the trade, and a
    guardrail is usually what ENDS it. Guardrail commands were served without
    being recorded, so the trail stopped at the last HOLD while the trade's
    own close reason said "Safety: ..." — and trade_history.jsonl fed that
    truncated story to the post-trade reflections."""

    def _pm(self):
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened({
            "ticket": 909, "position_id": "909", "symbol": "USDCAD",
            "direction": "BUY", "entry_price": 1.3700, "lots": 1.0,
            "remaining_lots": 1.0, "max_loss_usd": 100.0, "tick_value": 10.0,
        })
        return pm

    def _report(self, **over):
        data = {"ticket": 909, "position_id": "909", "symbol": "USDCAD",
                "remaining_lots": 1.0, "profit_usd": 0.0, "sl": 1.3660,
                "current_price": 1.3710, "spread_pips": 2.0}
        data.update(over)
        return data

    def test_guardrail_close_lands_in_the_trail(self):
        pm = self._pm()
        pm.update_position(self._report(profit_usd=-150.0))

        last = pm.position.ai_decisions[-1]
        assert last["action"] == "CLOSE"
        assert last["source"] == "guardrail"
        assert "max loss" in last["reasoning"].lower()

    def test_profit_protection_close_lands_in_the_trail(self):
        """The exact shape from the 31.07 screenshot: peak, fade, safety close
        — previously invisible in the trail."""
        pm = self._pm()
        pm.update_position(self._report(profit_usd=50.0))
        cmd = pm.update_position(self._report(profit_usd=12.0))

        assert cmd["command"]["action"] == "CLOSE"
        last = pm.position.ai_decisions[-1]
        assert last["source"] == "guardrail"
        assert "profit dropped" in last["reasoning"]

    def test_repeating_guardrail_is_collapsed(self):
        """Guardrails re-fire on every report while the condition holds; the
        trail must show one line per event, not one per poll."""
        pm = self._pm()
        for _ in range(4):
            pm.update_position(self._report(profit_usd=-150.0))

        closes = [d for d in pm.position.ai_decisions
                  if d.get("source") == "guardrail"]
        assert len(closes) == 1

    def test_model_decisions_are_never_collapsed(self):
        """Two identical HOLDs 30s apart are two real consultations."""
        pm = self._pm()
        same = PositionCommand(action="HOLD", reason="AI: developing")
        pm._record_management_action(same, source="ai")
        pm._record_management_action(same, source="ai")
        assert len(pm.position.ai_decisions) == 2

    def test_trail_survives_into_the_closed_trade_record(self, tmp_path):
        history = tmp_path / "trades.jsonl"
        pm = PositionManager(exit_engine=None, history_file=str(history))
        pm.on_position_opened({
            "ticket": 909, "position_id": "909", "symbol": "USDCAD",
            "direction": "BUY", "entry_price": 1.3700, "lots": 1.0,
            "remaining_lots": 1.0, "max_loss_usd": 100.0, "tick_value": 10.0,
        })
        pm.update_position(self._report(profit_usd=-150.0))
        pm.on_position_closed({"ticket": 909, "profit": -150.0,
                               "close_price": 1.3600, "reason": "Safety"})

        import json
        with open(history, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert any(d.get("source") == "guardrail" and d["action"] == "CLOSE"
                   for d in rec["ai_decisions"])


class TestCalibrationLineIsPerModel:
    """The line says "you have been OVERCONFIDENT by N points — state lower
    confidence". Computed across models or prompt versions, it tells the
    running configuration to correct an error it never made.

    Every negative case below is paired with the POSITIVE control that uses
    identical data with a matching signature — without it the assertions would
    hold for any reason at all (e.g. rows too sparse to score), and the filter
    could be deleted with the suite still green."""

    MODEL = "test/current-model"

    def _rows(self, n=60, model=None, prompt_version=None):
        """Scorable decision rows: each needs currency/event_name/pair and a
        distinct event_datetime, or build_summary cannot join them to a path
        and dedup collapses them to one."""
        import llm_decision_engine as lde
        rows = []
        for i in range(n):
            when = f"2026-{i % 12 + 1:02d}-{i // 12 + 1:02d}T13:30:00"
            rows.append({
                "timestamp": f"2026-07-15T13:28:{i % 60:02d}Z",
                "decision_id": f"d{i}",
                "event_name": "CPI m/m", "currency": "USD", "pair": "USDCAD",
                "direction": "BUY", "confidence": 0.9, "forced": False,
                "event_datetime": when,
                "model": model or self.MODEL,
                "prompt_version": (prompt_version
                                   or lde.ENTRY_PROMPT_VERSION),
            })
        return rows

    def _paths(self, n=60):
        """Measured path records in the shape calibration.index_paths expects
        (event_name_normalized + event_time are the join key, not
        event_datetime)."""
        paths = []
        for i in range(n):
            when = f"2026-{i % 12 + 1:02d}-{i // 12 + 1:02d}T13:30:00"
            paths.append({
                "test": False, "currency": "USD",
                "event_name": "CPI m/m", "event_name_normalized": "cpi m/m",
                "event_time": when, "pair": "USDCAD",
                # alternate hit/miss so the summary has a real hit rate
                "move_5min_pips": 20.0 if i % 2 else -20.0,
            })
        return paths

    def _engine(self, tmp_path, paths, decisions):
        from llm_decision_engine import LLMDecisionEngine
        engine = LLMDecisionEngine(
            provider="rule-based",
            trade_history_file=str(tmp_path / "trades.jsonl"),
            paths_provider=lambda: paths)
        engine.model = self.MODEL
        engine.decision_log = type("Log", (), {
            "get_recent": staticmethod(lambda limit=300: decisions)})()
        return engine

    def test_line_appears_for_the_running_configuration(self, tmp_path):
        """POSITIVE CONTROL — without this the negatives prove nothing."""
        engine = self._engine(tmp_path, self._paths(), self._rows())
        line = engine._calibration_line()
        assert line is not None and "CALIBRATION" in line

    def test_no_line_when_history_belongs_to_another_model(self, tmp_path):
        engine = self._engine(tmp_path, self._paths(),
                              self._rows(model="test/old-model"))
        assert engine._calibration_line() is None

    def test_no_line_when_history_belongs_to_another_prompt_version(self, tmp_path):
        engine = self._engine(tmp_path, self._paths(),
                              self._rows(prompt_version="1999-01-01.0"))
        assert engine._calibration_line() is None

    def test_signature_matches_the_single_call_stamp(self, tmp_path):
        engine = self._engine(tmp_path, [], [])
        assert engine._current_model_signature() == "test/current-model"

    def test_signature_matches_the_panel_stamp(self, tmp_path, monkeypatch):
        import llm_decision_engine as lde
        engine = self._engine(tmp_path, [], [])
        engine.provider = "openrouter"
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", ["a", "b", "c"])
        assert engine._current_model_signature() == "panel:a,b,c"

    def test_signature_matches_the_k_sample_stamp(self, tmp_path, monkeypatch):
        import llm_decision_engine as lde
        engine = self._engine(tmp_path, [], [])
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", [])
        monkeypatch.setattr(lde, "ENSEMBLE_K", 3)
        assert engine._current_model_signature() == "test/current-model x3"
