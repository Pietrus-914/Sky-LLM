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
from position_manager import (PositionManager, OpenPosition,
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
        pos = make_position(profit_usd=5.0, realized_usd=40.0)
        cmd = engine._rule_based_decision(pos)
        assert cmd.action == "MODIFY_SL"


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
        server._analysis_failures.clear()
        server.analyzed_events.clear()
        server._analysis_failures["USD_CPI_20260730_1230"] = 2
        server._analysis_failures["USD_NFP_20260731_1230"] = 1
        server.analyzed_events["USD_CPI_20260730_1230"] = datetime.utcnow()

        server._cleanup_analysis_failures()

        assert "USD_CPI_20260730_1230" not in server._analysis_failures
        assert server._analysis_failures["USD_NFP_20260731_1230"] == 1
        server._analysis_failures.clear()
        server.analyzed_events.clear()


class TestCalibrationLineIsPerModel:
    """The line says "you have been OVERCONFIDENT by N points — state lower
    confidence". Computed across models or prompt versions, it tells the
    running configuration to correct an error it never made."""

    def _engine(self, tmp_path, paths, decisions):
        from llm_decision_engine import LLMDecisionEngine
        engine = LLMDecisionEngine(
            provider="rule-based",
            trade_history_file=str(tmp_path / "trades.jsonl"),
            paths_provider=lambda: paths)
        engine.model = "test/current-model"
        engine.decision_log = type("Log", (), {
            "get_recent": staticmethod(lambda limit=300: decisions)})()
        return engine

    def test_no_line_when_history_belongs_to_another_model(self, tmp_path):
        import llm_decision_engine as lde
        foreign = [{"model": "test/old-model",
                    "prompt_version": lde.ENTRY_PROMPT_VERSION,
                    "direction": "BUY", "confidence": 0.9}
                   for _ in range(200)]
        engine = self._engine(tmp_path, [{"event_key": "x"}], foreign)
        assert engine._calibration_line() is None

    def test_no_line_when_history_belongs_to_another_prompt_version(self, tmp_path):
        rows = [{"model": "test/current-model",
                 "prompt_version": "1999-01-01.0",
                 "direction": "BUY", "confidence": 0.9}
                for _ in range(200)]
        engine = self._engine(tmp_path, [{"event_key": "x"}], rows)
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
