"""
Panel event whitelist: DISABLED-name semantics + legacy migration (18.08.2026).

The bug: dashboard.html carried a hardcoded roster of 13 event names while
config.py's roster had grown to 19 (commit 3aa4037, 29.07.2026 — the FF feed
calls rate decisions "Federal Funds Rate" / "Official Bank Rate" /
"Overnight Rate", not "Interest Rate Decision"). Save posted the checked
names and the server stored them as `enabled_events`, i.e. an ALLOW-list, so
every roster name the panel could not display was filtered out of
HIGH_IMPACT_EVENTS on each save — FOMC, BoE and BoC became untradeable in
whitelist mode, invisibly, and the operator had no checkbox to fix it.

The fix has two halves, both pinned here:
  * persistence stores the DISABLED complement, so a roster addition is
    enabled by default and can only be turned off explicitly;
  * a legacy `enabled_events` file is migrated on load — only names the old
    panel could actually render count as a deliberate "off".
"""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import config as cfg


@pytest.fixture
def isolated_overrides(tmp_path, monkeypatch):
    """Point config at a throwaway overrides file, start from the PRISTINE
    roster, and restore the live state afterwards (the operator's real panel
    state must survive).

    The reset matters: config.py applied the operator's real
    logs/runtime_overrides.json at import, so without it a test asserting
    "the full roster is intact" only passed while the operator happened to
    have nothing disabled — disabling a single event in the panel (the very
    feature under test) turned test_junk_value_is_ignored red. Clearing
    CONFIG_NOTES likewise stops the migration assertion from passing on the
    note that the REAL file emitted at import instead of the one this test
    provoked.
    """
    path = tmp_path / "runtime_overrides.json"
    monkeypatch.setattr(cfg, "_OVERRIDES_FILE", str(path))
    backup = (list(cfg.TIER1_EVENTS), list(cfg.TIER2_EVENTS),
              list(cfg.HIGH_IMPACT_EVENTS), list(cfg.CONFIG_NOTES))
    cfg._apply_disabled_events([])      # pristine rosters, independent of the operator
    cfg.CONFIG_NOTES.clear()
    yield path
    (cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS) = (
        backup[0], backup[1], backup[2])
    cfg.CONFIG_NOTES[:] = backup[3]


def write_overrides(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


class TestDisabledSemantics:
    def test_disabled_names_are_filtered_out(self, isolated_overrides):
        write_overrides(isolated_overrides, {"disabled_events": ["CPI", "Retail Sales"]})
        cfg._apply_runtime_overrides()
        assert "CPI" not in cfg.HIGH_IMPACT_EVENTS
        assert "Retail Sales" not in cfg.HIGH_IMPACT_EVENTS
        # everything else survives
        assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS
        assert "GDP" in cfg.HIGH_IMPACT_EVENTS

    def test_roster_addition_is_enabled_by_default(self, isolated_overrides):
        """THE regression: a name absent from the saved file must trade."""
        write_overrides(isolated_overrides, {"disabled_events": ["NFP"]})
        cfg._apply_runtime_overrides()
        for name in ("Federal Funds Rate", "Official Bank Rate", "Overnight Rate",
                     "Consumer Price Index", "Gross Domestic Product"):
            assert name in cfg.HIGH_IMPACT_EVENTS, name

    def test_empty_disabled_list_enables_the_full_roster(self, isolated_overrides):
        write_overrides(isolated_overrides, {"disabled_events": []})
        cfg._apply_runtime_overrides()
        assert cfg.TIER1_EVENTS == cfg.TIER1_EVENTS_ALL
        assert cfg.TIER2_EVENTS == cfg.TIER2_EVENTS_ALL

    def test_junk_value_is_ignored(self, isolated_overrides):
        write_overrides(isolated_overrides, {"disabled_events": "CPI"})
        cfg._apply_runtime_overrides()
        assert cfg.HIGH_IMPACT_EVENTS == cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL

    def test_unknown_disabled_names_do_not_shrink_the_roster(self, isolated_overrides):
        write_overrides(isolated_overrides, {"disabled_events": ["Made Up Event"]})
        cfg._apply_runtime_overrides()
        assert cfg.HIGH_IMPACT_EVENTS == cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL


class TestLegacyMigration:
    def test_legacy_enabled_list_rescues_names_the_old_panel_never_showed(
            self, isolated_overrides):
        """Exactly the production file: the 13 names of the old dashboard."""
        write_overrides(isolated_overrides,
                        {"enabled_events": sorted(cfg.LEGACY_PANEL_EVENT_ROSTER)})
        cfg._apply_runtime_overrides()
        # Rescued (the old panel had no checkbox for them)
        for name in ("Federal Funds Rate", "Official Bank Rate", "Overnight Rate",
                     "Nonfarm Payrolls", "Consumer Price Index",
                     "Gross Domestic Product"):
            assert name in cfg.HIGH_IMPACT_EVENTS, name
        # Nothing was actually unchecked, so nothing is disabled
        assert cfg.HIGH_IMPACT_EVENTS == cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL
        assert any("legacy 'enabled_events' migrated" in n for n in cfg.CONFIG_NOTES)

    def test_legacy_deliberate_off_is_preserved(self, isolated_overrides):
        """A name the old panel DID display and the operator unchecked stays
        off — the migration must not re-enable a real decision."""
        enabled = [n for n in cfg.LEGACY_PANEL_EVENT_ROSTER if n != "Retail Sales"]
        write_overrides(isolated_overrides, {"enabled_events": enabled})
        cfg._apply_runtime_overrides()
        assert "Retail Sales" not in cfg.HIGH_IMPACT_EVENTS
        assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS

    def test_new_key_wins_over_legacy(self, isolated_overrides):
        write_overrides(isolated_overrides, {
            "disabled_events": ["GDP"],
            "enabled_events": ["CPI"],       # stale leftover
        })
        cfg._apply_runtime_overrides()
        assert "GDP" not in cfg.HIGH_IMPACT_EVENTS
        assert "Retail Sales" in cfg.HIGH_IMPACT_EVENTS   # legacy list ignored


class TestPersistenceRoundTrip:
    def test_set_enabled_events_writes_disabled_and_retires_legacy(
            self, isolated_overrides):
        write_overrides(isolated_overrides,
                        {"enabled_events": ["CPI"], "max_loss_usd": 100.0})

        cfg.set_enabled_events(["CPI", "GDP"], known_roster=cfg.ROSTER_ALL)

        stored = json.loads(isolated_overrides.read_text(encoding="utf-8"))
        assert "enabled_events" not in stored          # legacy key removed
        assert set(stored["disabled_events"]) == (
            set(cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL) - {"CPI", "GDP"})
        assert stored["max_loss_usd"] == 100.0         # unrelated keys survive
        assert cfg.HIGH_IMPACT_EVENTS == ["CPI", "GDP"]

    def test_survives_a_restart(self, isolated_overrides):
        cfg.set_enabled_events([n for n in cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL
                                if n != "New Home Sales"],
                               known_roster=cfg.ROSTER_ALL)
        # simulate a fresh import applying the file
        cfg._apply_disabled_events([])                  # wipe in-memory state
        cfg._apply_runtime_overrides()
        assert "New Home Sales" not in cfg.HIGH_IMPACT_EVENTS
        assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS

    def test_disabled_event_names_mirrors_the_effective_lists(self, isolated_overrides):
        cfg.set_enabled_events(["CPI"], known_roster=cfg.ROSTER_ALL)
        disabled = cfg.disabled_event_names()
        assert "CPI" not in disabled
        assert "GDP" in disabled
        assert set(disabled) | set(cfg.HIGH_IMPACT_EVENTS) == set(
            cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL)

    def test_known_roster_scopes_the_complement(self, isolated_overrides):
        """A stale tab (roster from before a restart) must not disable names
        it never rendered — the original bug in miniature."""
        stale_roster = list(cfg.LEGACY_PANEL_EVENT_ROSTER)

        disabled = cfg.set_enabled_events(stale_roster, known_roster=stale_roster)

        assert disabled == []
        for name in ("Federal Funds Rate", "Official Bank Rate", "Overnight Rate"):
            assert name in cfg.HIGH_IMPACT_EVENTS, name

    def test_known_roster_still_honours_a_real_uncheck(self, isolated_overrides):
        stale_roster = list(cfg.LEGACY_PANEL_EVENT_ROSTER)
        enabled = [n for n in stale_roster if n != "CPI"]

        disabled = cfg.set_enabled_events(enabled, known_roster=stale_roster)

        assert disabled == ["CPI"]
        assert "CPI" not in cfg.HIGH_IMPACT_EVENTS

    def test_known_roster_preserves_an_earlier_off_state(self, isolated_overrides):
        """A name outside the client's roster keeps whatever it already was —
        including 'disabled', so a stale Save cannot silently RE-enable it."""
        cfg.set_enabled_events([n for n in cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL
                                if n != "Overnight Rate"],
                               known_roster=cfg.ROSTER_ALL)
        stale_roster = list(cfg.LEGACY_PANEL_EVENT_ROSTER)

        cfg.set_enabled_events(stale_roster, known_roster=stale_roster)

        assert "Overnight Rate" not in cfg.HIGH_IMPACT_EVENTS
        assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS

    def test_junk_roster_falls_back_to_the_legacy_scope(self, isolated_overrides):
        """An unusable `roster` is treated as "not declared", so the scope is
        the legacy panel roster — never the full one."""
        disabled = cfg.set_enabled_events(["CPI"], known_roster="CPI")
        assert "GDP" in disabled                       # legacy name, unchecked
        assert "Gross Domestic Product" not in disabled  # never rendered by it
        assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS

    def test_roster_less_save_cannot_disable_a_modern_name(self, isolated_overrides):
        """THE permanence trap: the pre-18.08.2026 panel posts `events` with
        no `roster`. Scoping that to the full roster would let it re-disable
        the six names it never displayed AND persist them under the new key,
        where the legacy migration can no longer rescue them."""
        legacy_save = list(cfg.LEGACY_PANEL_EVENT_ROSTER)

        disabled = cfg.set_enabled_events(legacy_save)     # no roster field

        assert disabled == []
        for name in ("Federal Funds Rate", "Official Bank Rate", "Overnight Rate",
                     "Nonfarm Payrolls", "Consumer Price Index",
                     "Gross Domestic Product"):
            assert name in cfg.HIGH_IMPACT_EVENTS, name
        stored = json.loads(isolated_overrides.read_text(encoding="utf-8"))
        assert stored["disabled_events"] == []

    def test_roster_all_opts_into_the_full_complement(self, isolated_overrides):
        """Scripts that really mean "disable everything else" say so."""
        disabled = cfg.set_enabled_events(["CPI"], known_roster=cfg.ROSTER_ALL)
        assert "Gross Domestic Product" in disabled
        assert cfg.HIGH_IMPACT_EVENTS == ["CPI"]

    def test_none_value_removes_a_key_without_touching_others(self, isolated_overrides):
        write_overrides(isolated_overrides, {"a": 1, "b": 2})
        cfg.save_runtime_overrides({"a": None, "c": 3})
        stored = json.loads(isolated_overrides.read_text(encoding="utf-8"))
        assert stored == {"b": 2, "c": 3}
