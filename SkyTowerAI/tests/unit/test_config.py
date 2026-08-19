"""
Unit tests for configuration module
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from config import (
    TRADING_CONFIG,
    TIER1_EVENTS,
    TIER2_EVENTS,
    HIGH_IMPACT_EVENTS,
    CURRENCY_PAIRS,
    DEFAULT_PAIRS,
    TYPICAL_NEWS_SPREADS,
    SPREAD_LOT_REDUCTION,
)


class TestTradingConfig:
    """Tests for trading configuration"""

    def test_risk_percent_in_range(self):
        """Risk percent should be between 0 and 100"""
        assert 0 < TRADING_CONFIG["max_risk_percent"] <= 100

    def test_lot_percent_in_range(self):
        """Lot percent should be between 0 and 100"""
        assert 0 < TRADING_CONFIG["default_lot_percent"] <= 100

    def test_entry_timing_positive(self):
        """Entry timing should be positive"""
        assert TRADING_CONFIG["entry_seconds_before"] > 0

    def test_exit_timing_positive(self):
        """Exit timing should be positive"""
        assert TRADING_CONFIG["exit_minutes_after"] > 0

    def test_max_spread_positive(self):
        """Max spread should be positive"""
        assert TRADING_CONFIG["max_spread_pips"] > 0


class TestEventConfig:
    """Tests for event configuration"""

    def test_tier1_events_not_empty(self):
        """Tier 1 events should have items"""
        assert len(TIER1_EVENTS) > 0

    def test_tier2_events_not_empty(self):
        """Tier 2 events should have items"""
        assert len(TIER2_EVENTS) > 0

    def test_high_impact_combines_tiers(self):
        """HIGH_IMPACT_EVENTS should combine both tiers"""
        for event in TIER1_EVENTS:
            assert event in HIGH_IMPACT_EVENTS
        for event in TIER2_EVENTS:
            assert event in HIGH_IMPACT_EVENTS

    def test_interest_rate_in_tier1(self):
        """Interest Rate Decision should be Tier 1"""
        assert "Interest Rate Decision" in TIER1_EVENTS

    def test_nfp_in_tier1(self):
        """NFP should be Tier 1"""
        nfp_variants = ["NFP", "Non-Farm Payrolls", "Nonfarm Payrolls"]
        assert any(nfp in TIER1_EVENTS for nfp in nfp_variants)

    def test_cpi_in_tier1(self):
        """CPI should be Tier 1"""
        cpi_variants = ["CPI", "Consumer Price Index"]
        assert any(cpi in TIER1_EVENTS for cpi in cpi_variants)


class TestCurrencyConfig:
    """Tests for currency pair configuration"""

    def test_primary_currencies_defined(self):
        """Primary currencies should be defined"""
        primary = ["NZD", "CAD", "AUD", "USD", "GBP"]
        for currency in primary:
            assert currency in CURRENCY_PAIRS

    def test_each_currency_has_pairs(self):
        """Each currency should have at least one pair"""
        for currency, pairs in CURRENCY_PAIRS.items():
            assert len(pairs) > 0, f"{currency} has no pairs"

    def test_default_pairs_exist(self):
        """Default pairs should be defined for primary currencies"""
        primary = ["NZD", "CAD", "AUD", "USD", "GBP"]
        for currency in primary:
            assert currency in DEFAULT_PAIRS
            assert DEFAULT_PAIRS[currency] in CURRENCY_PAIRS[currency]


class TestSpreadConfig:
    """Tests for spread configuration"""

    def test_spread_data_has_required_fields(self):
        """Each spread entry should have required fields"""
        required = ["normal", "news", "max_acceptable"]
        for pair, data in TYPICAL_NEWS_SPREADS.items():
            for field in required:
                assert field in data, f"{pair} missing {field}"

    def test_spread_values_logical(self):
        """Spread values should be logical (normal < news < max)"""
        for pair, data in TYPICAL_NEWS_SPREADS.items():
            assert data["normal"] < data["news"], f"{pair}: normal >= news"
            assert data["news"] < data["max_acceptable"], f"{pair}: news >= max"

    def test_spread_reduction_thresholds_ascending(self):
        """Spread reduction thresholds should be ascending"""
        levels = ["low", "medium", "high", "extreme"]
        thresholds = [SPREAD_LOT_REDUCTION[l]["threshold"] for l in levels]
        assert thresholds == sorted(thresholds)

    def test_spread_reduction_multipliers_descending(self):
        """Spread reduction multipliers should be descending"""
        levels = ["low", "medium", "high", "extreme"]
        multipliers = [SPREAD_LOT_REDUCTION[l]["multiplier"] for l in levels]
        assert multipliers == sorted(multipliers, reverse=True)

    def test_extreme_spread_stops_trading(self):
        """Extreme spread should have 0 multiplier"""
        assert SPREAD_LOT_REDUCTION["extreme"]["multiplier"] == 0.0


class TestSpreadLotReduction:
    """Test spread to lot multiplier logic"""

    def test_low_spread_full_lot(self):
        """Spread < 3 pips should give 100% lot"""
        threshold = SPREAD_LOT_REDUCTION["low"]["threshold"]
        multiplier = SPREAD_LOT_REDUCTION["low"]["multiplier"]
        assert threshold == 3
        assert multiplier == 1.0

    def test_medium_spread_reduced(self):
        """Spread 3-6 pips should give 80% lot"""
        threshold = SPREAD_LOT_REDUCTION["medium"]["threshold"]
        multiplier = SPREAD_LOT_REDUCTION["medium"]["multiplier"]
        assert threshold == 6
        assert multiplier == 0.8

    def test_high_spread_more_reduced(self):
        """Spread 6-10 pips should give 60% lot"""
        threshold = SPREAD_LOT_REDUCTION["high"]["threshold"]
        multiplier = SPREAD_LOT_REDUCTION["high"]["multiplier"]
        assert threshold == 10
        assert multiplier == 0.6


class TestModelEnvOverrides:
    """Entry/exit models are env-overridable per machine (no code edits)."""

    def _fresh_config(self, env, tmp_path=None):
        """Import config.py in a clean subprocess.

        SKYTOWER_OVERRIDES_FILE points at a path that does not exist, so the
        run sees default < env and nothing else. Without it the subprocess
        loads the OPERATOR's logs/runtime_overrides.json, whose panel-set
        entry_model outranks both the code default and the env var under
        test — both assertions below then failed on every run, and the env
        pass-through they exist to protect was never actually exercised.
        """
        import subprocess, sys, os, json, tempfile
        code = (
            "import json, config; "
            "print(json.dumps({'entry': config.LLM_CONFIG['model'], "
            "'exit': config.POSITION_MANAGEMENT_CONFIG['exit_llm_model']}))")
        pristine = os.path.join(str(tmp_path) if tmp_path else tempfile.mkdtemp(),
                                "no_runtime_overrides.json")
        full_env = {**os.environ, "SKYTOWER_OVERRIDES_FILE": pristine, **env}
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
            cwd=os.path.join(os.path.dirname(__file__), '..', '..', 'python'),
            env=full_env)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_defaults_are_current_models(self):
        cfg = self._fresh_config({"SKYTOWER_ENTRY_MODEL": "",
                                  "SKYTOWER_EXIT_MODEL": ""})
        assert cfg["entry"] == "anthropic/claude-opus-4.8"
        assert cfg["exit"] == "anthropic/claude-sonnet-5"

    def test_env_overrides_win(self):
        # Sentinel slugs: a value that could legitimately sit in the
        # operator's .env would make this pass even if the pass-through broke
        cfg = self._fresh_config({
            "SKYTOWER_ENTRY_MODEL": "test/sentinel-entry-model",
            "SKYTOWER_EXIT_MODEL": "test/sentinel-exit-model"})
        assert cfg["entry"] == "test/sentinel-entry-model"
        assert cfg["exit"] == "test/sentinel-exit-model"
