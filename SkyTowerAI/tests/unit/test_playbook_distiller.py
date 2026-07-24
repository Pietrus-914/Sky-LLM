"""
Unit tests for F5c: playbook distillation proposals with the operator
approve/reject gate.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from playbook_distiller import (PlaybookProposals, parse_proposal_reply,
                                find_playbook_key, generate_proposal,
                                MAX_FIELD_CHARS)


def good_reply(**over):
    data = {"pattern": "Fades the pre-drift in 62% (n=41).",
            "typical_behavior": "5-min median 17 pips (n=58).",
            "notes": "Hiking regime doubles the move (n=15).",
            "rationale": "Adds measured fade rate."}
    data.update(over)
    return json.dumps(data)


class TestParseReply:
    def test_valid(self):
        out = parse_proposal_reply(good_reply())
        assert out["pattern"].startswith("Fades")
        assert out["rationale"] == "Adds measured fade rate."

    def test_chatty_reply_salvaged(self):
        out = parse_proposal_reply("Sure! Here is the entry:\n" + good_reply()
                                   + "\nHope this helps.")
        assert out is not None

    def test_no_entry_fields_rejected(self):
        assert parse_proposal_reply(json.dumps({"rationale": "meh"})) is None
        assert parse_proposal_reply("not json at all") is None
        assert parse_proposal_reply(json.dumps(["list"])) is None

    def test_long_fields_capped(self):
        out = parse_proposal_reply(good_reply(pattern="x" * 2000))
        assert len(out["pattern"]) == MAX_FIELD_CHARS

    def test_non_string_fields_dropped(self):
        out = parse_proposal_reply(json.dumps(
            {"pattern": {"nested": 1}, "typical_behavior": "ok"}))
        assert "pattern" not in out and out["typical_behavior"] == "ok"


class TestFindPlaybookKey:
    def test_normalized_match_wins(self):
        books = {"CPI m/m (USD)": {}, "CURRENCY:USD": {}}
        assert find_playbook_key(books, "CPI m/m") == "CPI m/m (USD)"

    def test_fallback_to_event_name(self):
        assert find_playbook_key({"NFP": {}}, "CPI m/m") == "CPI m/m"

    def test_currency_keys_ignored(self):
        assert find_playbook_key({"CURRENCY:USD": {}}, "cpi m/m") == "cpi m/m"


class TestGenerateProposal:
    def test_shape(self):
        p = generate_proposal(lambda s, u: good_reply(), "CPI m/m", "usd",
                              {"n_releases": 63}, {"pattern": "old"}, "CPI m/m")
        assert p["status"] == "pending"
        assert p["currency"] == "USD"
        assert p["playbook_key"] == "CPI m/m"
        assert p["current"] == {"pattern": "old"}
        assert "rationale" not in p["proposed"]
        assert p["rationale"]

    def test_chat_failure_and_garbage(self):
        def boom(s, u):
            raise RuntimeError("api")
        assert generate_proposal(boom, "E", "USD", None, None, "E") is None
        assert generate_proposal(lambda s, u: "garbage", "E", "USD",
                                 None, None, "E") is None
        assert generate_proposal(None, "E", "USD", None, None, "E") is None


class TestProposalStore:
    def _proposal(self, **over):
        p = generate_proposal(lambda s, u: good_reply(), "CPI m/m", "USD",
                              None, None, "CPI m/m")
        p.update(over)
        return p

    def test_add_list_filter(self, tmp_path):
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        store.add(self._proposal(id="b", status="rejected"))
        assert [p["id"] for p in store.list(status="pending")] == ["a"]
        assert len(store.list()) == 2

    def test_approve_applies_to_playbooks(self, tmp_path):
        books = tmp_path / "event_playbooks.json"
        books.write_text(json.dumps({"CPI m/m": {"pattern": "old"},
                                     "NFP": {"pattern": "keep"}}),
                         encoding="utf-8")
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        result = store.decide("a", "approve", str(books))
        assert result["ok"]
        data = json.loads(books.read_text(encoding="utf-8"))
        assert data["CPI m/m"]["pattern"].startswith("Fades")
        assert data["NFP"] == {"pattern": "keep"}       # untouched
        assert store.list(status="approved")[0]["id"] == "a"

    def test_approve_creates_playbooks_file(self, tmp_path):
        books = tmp_path / "knowledge" / "event_playbooks.json"
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        assert store.decide("a", "approve", str(books))["ok"]
        assert json.loads(books.read_text(encoding="utf-8"))["CPI m/m"]

    def test_reject_does_not_touch_playbooks(self, tmp_path):
        books = tmp_path / "event_playbooks.json"
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        assert store.decide("a", "reject", str(books))["ok"]
        assert not books.exists()
        assert store.list(status="rejected")

    def test_double_decide_and_unknown_id(self, tmp_path):
        books = tmp_path / "event_playbooks.json"
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        store.decide("a", "reject", str(books))
        assert not store.decide("a", "approve", str(books))["ok"]
        assert not store.decide("nope", "approve", str(books))["ok"]
        assert not store.decide("a", "explode", str(books))["ok"]

    def test_failed_apply_keeps_pending(self, tmp_path):
        # Corrupt playbooks file -> apply raises -> proposal stays pending
        books = tmp_path / "event_playbooks.json"
        books.write_text("[1, 2, 3]", encoding="utf-8")   # not an object
        store = PlaybookProposals(str(tmp_path / "props.jsonl"))
        store.add(self._proposal(id="a"))
        result = store.decide("a", "approve", str(books))
        assert not result["ok"]
        assert store.list(status="pending")[0]["id"] == "a"


class TestEndpoints:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import server
        from server import app
        app.config['TESTING'] = True
        import playbook_distiller as pd

        orig = (server._playbook_proposals, server.decision_engine,
                server.calendar)
        server._playbook_proposals = PlaybookProposals(
            str(tmp_path / "props.jsonl"))
        server.decision_engine = SimpleNamespace(
            _load_learned_stats=lambda: {"events": {
                "USD|cpi m/m": learned_entry()}},
            _load_playbooks=lambda: {})
        if server.calendar is None:
            server.calendar = MagicMock()
        books = tmp_path / "event_playbooks.json"
        monkeypatch.setattr("config.EVENT_PLAYBOOKS_FILE", str(books))
        monkeypatch.setattr("llm_util.make_chat_fn",
                            lambda **kw: (lambda s, u: good_reply()))
        with app.test_client() as c:
            yield c, books
        (server._playbook_proposals, server.decision_engine,
         server.calendar) = orig

    def test_distill_list_approve_flow(self, client):
        c, books = client
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "CPI m/m", "currency": "usd"})
        assert resp.get_json()["status"] == "ok"
        pid = resp.get_json()["proposal"]["id"]

        listing = c.get('/api/playbooks/proposals?status=pending').get_json()
        assert [p["id"] for p in listing["proposals"]] == [pid]

        decide = c.post('/api/playbooks/proposals/decide',
                        json={"id": pid, "action": "approve"})
        assert decide.get_json()["status"] == "ok"
        assert json.loads(books.read_text(encoding="utf-8"))["CPI m/m"]

    def test_distill_validates_input(self, client):
        c, _ = client
        assert c.post('/api/playbooks/distill',
                      json={"event_name": "", "currency": "USD"}).status_code == 400
        assert c.post('/api/playbooks/distill',
                      json={"event_name": "CPI", "currency": "DOLLAR"}).status_code == 400

    def test_distill_no_sample_guides_instead_of_drafting(self, client):
        # An event with no measured stats must NOT draft blind — it returns a
        # 404 that names the currency's events we DO have data for. Regression:
        # "CPI" (no m/m suffix) missed for every currency and drafted blind.
        c, _ = client
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "CPI", "currency": "USD"})
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["status"] == "error"
        assert "CPI m/m" in body["message"]          # points at the real name
        assert "CPI m/m" in body.get("available", [])

    def test_distill_bundle_alias_fallback_still_drafts(self, client):
        # The 404 block sits right AFTER the alias lookup — if they get
        # reordered, every bundle-member event (Core CPI m/m etc.) 404s while
        # the card tells the operator to type exact names. Alias hit must draft.
        c, _ = client
        import server
        server.decision_engine._load_learned_stats = lambda: {
            "events": {"USD|cpi m/m": learned_entry()},
            "bundle_alias": {"USD|core cpi m/m": {"to": "USD|cpi m/m", "n": 40}}}
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "Core CPI m/m", "currency": "USD"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["proposal"]["event_name"] == "Core CPI m/m"

    def test_distill_broken_stats_is_503_not_brak(self, client):
        # _load_learned_stats swallows a broken file into {} — that must read
        # as "stats unavailable" (503), never as a confident "z danymi: brak"
        c, _ = client
        import server
        server.decision_engine._load_learned_stats = lambda: {}
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "CPI m/m", "currency": "USD"})
        assert resp.status_code == 503
        assert "learned_stats.json" in resp.get_json()["message"]

    def test_distill_404_ranks_match_and_tradeable_first(self, client):
        # Guidance ranking: typed-substring match > tradeable whitelist > n.
        # Regression: a raw n-sort put junk (Crude Oil Inventories n=286)
        # ahead of every tradeable USD event and cut CPI from the top-12.
        c, _ = client
        import server
        server.decision_engine._load_learned_stats = lambda: {"events": {
            "USD|crude oil inventories": learned_entry(
                name="Crude Oil Inventories", n=286),
            "USD|cpi m/m": learned_entry(),                       # n=63
            "USD|retail sales m/m": learned_entry(
                name="Retail Sales m/m", n=62),
        }}
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "CPI", "currency": "USD"})
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["available"] == ["CPI m/m", "Retail Sales m/m",
                                     "Crude Oil Inventories"]
        # Other currency with data but not THIS one -> explicit "brak"
        resp2 = c.post('/api/playbooks/distill',
                       json={"event_name": "CPI", "currency": "NZD"})
        assert resp2.status_code == 404
        body2 = resp2.get_json()
        assert body2["available"] == [] and "brak" in body2["message"]

    def test_distill_404_survives_malformed_entries(self, client):
        # One bad regen row (non-dict entry, string n_releases) must be
        # skipped from the guidance, not detonate the endpoint into a 500
        c, _ = client
        import server
        server.decision_engine._load_learned_stats = lambda: {"events": {
            "USD|cpi m/m": learned_entry(),
            "USD|broken": "not-a-dict",
            "USD|weird n": {"event_name": "Weird N", "currency": "USD",
                            "n_releases": "abc"},
        }}
        resp = c.post('/api/playbooks/distill',
                      json={"event_name": "CPI", "currency": "USD"})
        assert resp.status_code == 404
        body = resp.get_json()
        assert "CPI m/m" in body["available"]
        assert "not-a-dict" not in json.dumps(body)


def learned_entry(name="CPI m/m", currency="USD", n=63):
    return {"event_name": name, "currency": currency, "n_releases": n,
            "pairs": {"USDCAD": {"n": n}}}


class TestBatchCandidates:
    from playbook_distiller import select_batch_candidates as _sel

    def _run(self, events, proposals=None, playbooks=None,
             wanted=("CPI", "Non-Farm"), **kw):
        from playbook_distiller import select_batch_candidates
        return select_batch_candidates(events, playbooks or {},
                                       proposals or [], list(wanted),
                                       "2026-07-18T12:00:00", **kw)

    def test_thin_and_untradeable_filtered(self):
        events = {
            "USD|cpi m/m": learned_entry(n=63),
            "USD|crude oil inventories": learned_entry(
                name="Crude Oil Inventories", n=286),
            "NZD|cpi q/q": learned_entry(name="CPI q/q", currency="NZD", n=5),
        }
        cands, skipped = self._run(events)
        assert [c["event_name"] for c in cands] == ["CPI m/m"]
        assert skipped["not_tradeable"] == 1 and skipped["thin"] == 1

    def test_pending_and_cooldown_skips(self):
        events = {"USD|cpi m/m": learned_entry(),
                  "USD|non-farm employment change": learned_entry(
                      name="Non-Farm Employment Change")}
        proposals = [
            {"currency": "USD", "event_name": "CPI m/m", "status": "pending",
             "created_at": "2026-06-01T00:00:00Z"},
            {"currency": "USD", "event_name": "Non-Farm Employment Change",
             "status": "rejected", "created_at": "2026-07-10T00:00:00Z"},
        ]
        cands, skipped = self._run(events, proposals=proposals)
        assert cands == []
        assert skipped["pending"] == 1 and skipped["cooldown"] == 1

    def test_old_decision_does_not_block(self):
        events = {"USD|cpi m/m": learned_entry()}
        proposals = [{"currency": "USD", "event_name": "CPI m/m",
                      "status": "approved",
                      "created_at": "2026-06-01T00:00:00Z"}]  # > 14 dni
        cands, _ = self._run(events, proposals=proposals)
        assert len(cands) == 1

    def test_cap_and_biggest_samples_first(self):
        events = {f"USD|cpi {i}": learned_entry(name=f"CPI {i}", n=10 + i)
                  for i in range(5)}
        cands, skipped = self._run(events, max_batch=2)
        assert len(cands) == 2
        assert cands[0]["learned"]["n_releases"] == 14   # największe n najpierw
        assert skipped["capped"] == 3

    def test_playbook_key_resolved(self):
        events = {"USD|cpi m/m": learned_entry()}
        cands, _ = self._run(events, playbooks={"CPI m/m (USD)": {}})
        assert cands[0]["playbook_key"] == "CPI m/m (USD)"

    def test_round_robin_order_and_currency_tiebreak(self):
        # Locks the interleave semantics: currency order = biggest top-event n
        # first (tie -> alphabetical), one event per currency per round. Only
        # exact-list equality catches a regression to file-order iteration —
        # membership checks survive any 2-currency scramble.
        events = {
            "USD|cpi m/m": learned_entry(n=60),
            "NZD|cpi q/q": learned_entry(name="CPI q/q", currency="NZD", n=60),
            "USD|core cpi m/m": learned_entry(name="Core CPI m/m", n=55),
            "CAD|cpi m/m": learned_entry(name="CPI m/m", currency="CAD", n=40),
        }
        cands, skipped = self._run(events, max_batch=3, wanted=("CPI",))
        assert [(c["currency"], c["event_name"]) for c in cands] == \
            [("NZD", "CPI q/q"), ("USD", "CPI m/m"), ("CAD", "CPI m/m")]
        assert skipped["capped"] == 1                # USD Core CPI queued

    def test_skipped_counters_conserve_all_inputs(self):
        # Every input event must land in exactly one bucket: selected or one
        # skipped counter. The dashboard renders "capped" as an operator
        # promise ("w kolejce X — klik ponownie") — the accounting can't drift.
        events = {
            "USD|cpi m/m": learned_entry(),                            # selected
            "USD|crude oil inventories": learned_entry(
                name="Crude Oil Inventories", n=200),                  # not_tradeable
            "NZD|cpi q/q": learned_entry(name="CPI q/q",
                                         currency="NZD", n=5),         # thin
            "CAD|cpi m/m": learned_entry(name="CPI m/m",
                                         currency="CAD", n=30),        # pending
            "GBP|cpi y/y": learned_entry(name="CPI y/y",
                                         currency="GBP", n=25),        # selected
            "AUD|cpi q/q": learned_entry(name="CPI q/q",
                                         currency="AUD", n=20),        # capped
        }
        proposals = [{"currency": "CAD", "event_name": "CPI m/m",
                      "status": "pending",
                      "created_at": "2026-07-01T00:00:00Z"}]
        cands, skipped = self._run(events, proposals=proposals,
                                   max_batch=2, wanted=("CPI",))
        assert len(cands) + sum(skipped.values()) == len(events)
        assert skipped == {"thin": 1, "not_tradeable": 1, "pending": 1,
                           "cooldown": 0, "capped": 1}

    def test_currency_fair_quarterly_not_starved(self):
        # 6 USD monthly events (deep samples) + 1 NZD quarterly (thinner but
        # eligible). A global n_releases sort + cap would fill the batch with
        # USD and push NZD past the cap — the "USD only" bug. Round-robin must
        # surface NZD on the FIRST click.
        events = {f"USD|cpi {i}": learned_entry(name=f"CPI {i}", n=60 - i)
                  for i in range(6)}
        events["NZD|cpi q/q"] = learned_entry(name="CPI q/q",
                                              currency="NZD", n=20)
        cands, skipped = self._run(events, max_batch=3, wanted=("CPI",))
        assert "NZD" in {c["currency"] for c in cands}
        assert skipped["capped"] == 4        # 7 eligible - 3 taken


class TestBatchEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import server
        from server import app
        app.config['TESTING'] = True
        orig = (server._playbook_proposals, server.decision_engine,
                server.calendar)
        server._playbook_proposals = PlaybookProposals(
            str(tmp_path / "props.jsonl"))
        server.decision_engine = SimpleNamespace(
            _load_learned_stats=lambda: {"events": {
                "USD|cpi m/m": learned_entry(),
                "CAD|employment change": learned_entry(
                    name="Employment Change", currency="CAD", n=24),
            }},
            _load_playbooks=lambda: {})
        if server.calendar is None:
            server.calendar = MagicMock()
        monkeypatch.setattr("llm_util.make_chat_fn",
                            lambda **kw: (lambda s, u: good_reply()))
        with app.test_client() as c:
            yield c
        (server._playbook_proposals, server.decision_engine,
         server.calendar) = orig

    def test_batch_generates_then_dedupes(self, client):
        resp = client.post('/api/playbooks/distill-batch', json={})
        body = resp.get_json()
        assert body["status"] == "ok"
        # Exact list: locks round-robin order through the HTTP layer (USD's
        # top n=63 leads CAD n=24) AND the "CUR Event" string format the
        # dashboard toast parses with split(' ')[0]. The old `sorted(...) ==
        # [...] or len == 2` assertion had a dead left side (casing) and
        # passed on ANY two strings.
        assert body["generated"] == ["USD CPI m/m", "CAD Employment Change"]
        # drugi klik: wszystko wisi jako pending -> nic nowego
        resp2 = client.post('/api/playbooks/distill-batch', json={})
        body2 = resp2.get_json()
        assert body2["generated"] == []
        assert body2["skipped"]["pending"] == 2

    def test_batch_empty_stats_is_503(self, client):
        # Broken/absent learned_stats.json must NOT render as a green
        # "Drafted 0, all counters 0" success toast
        import server
        server.decision_engine._load_learned_stats = lambda: {}
        resp = client.post('/api/playbooks/distill-batch', json={})
        assert resp.status_code == 503
        assert "learned_stats.json" in resp.get_json()["message"]
