"""
Automatic monetary-policy regime tracking (hiking / cutting / hold).

Why: the same event often reacts OPPOSITE ways in a hiking vs cutting cycle,
so every recorded event path is tagged with the currency's regime. Hand-editing
config.CURRENCY_REGIMES after every central-bank meeting does not scale — and
the system already SEES every rate decision: the calendar feed delivers
actual vs previous for "Interest Rate Decision"-class events, the path
recorder stores them (with the market's reaction), and the backfill fills the
released value minutes later.

Two layers:
1. DETERMINISTIC (facts from the calendar): actual > previous = hike ->
   "hiking"; actual < previous = cut -> "cutting"; the direction expires into
   "hold" after HOLDS_TO_NEUTRAL consecutive no-change decisions or when the
   last move is older than STALE_MOVE_DAYS. A rate CHANGE is never ambiguous,
   so no LLM is consulted for it.
2. LLM ADJUDICATION (only for the ambiguous case): a no-change decision while
   the cycle direction is still alive — is it a pause within the cycle or the
   end of it? The model gets the numbers, the recent decision history and the
   MEASURED market reaction (a hawkish hold moves the currency up — a signal
   this system uniquely has on its own broker's prices). The LLM may only
   choose between the deterministic answer and "hold" — it can never invent
   the opposite direction. Any error falls back to the deterministic answer.

State: logs/currency_regimes.json (machine-owned, gitignored; rebuilt from
the config.CURRENCY_REGIMES seed on a fresh deploy). Manual override via the
panel (/api/regimes) wins until the bank's NEXT observed decision.

Coverage note: only currencies whose pairs have mounted EA charts get path
records (rate decisions of EUR/JPY/CHF won't auto-update unless such a pair
is monitored) — those stay at their seed/manual value.
"""
import json
import os
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional

from loguru import logger

from timeutil import utcnow, to_naive_utc
from event_reaction_history import parse_numeric, normalize_event_name

REGIMES = ("hiking", "cutting", "hold")

# Substrings (on the normalized event name) that identify a rate decision
RATE_DECISION_MARKERS = (
    "interest rate", "cash rate", "funds rate", "bank rate",
    "overnight rate", "refinancing rate", "policy rate", "base rate",
)

# A cycle direction older than this (no move since) decays to "hold"
STALE_MOVE_DAYS = 270
# ... and this many consecutive no-change decisions also mean "hold"
HOLDS_TO_NEUTRAL = 2
# Keep this many past decisions per currency in state
DECISIONS_KEPT = 12


def is_rate_decision(event_name: str) -> bool:
    name = normalize_event_name(event_name)
    # "MPC Official Bank Rate Votes" carries vote counts ("0-0-9"), not a
    # rate — parse_numeric would read it as 0 and fake a cut. Caught by the
    # historical replay when GBP's regime came out corrupted.
    if "votes" in name:
        return False
    return any(marker in name for marker in RATE_DECISION_MARKERS)


class RegimeTracker:
    """Thread-safe regime state, fed by recorded event paths."""

    def __init__(self, state_file: str = None, seed: Dict[str, str] = None,
                 llm_enabled: bool = True):
        if state_file is None:
            state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'logs', 'currency_regimes.json')
        self._state_file = state_file
        self._lock = Lock()
        self._llm_enabled = llm_enabled
        self._llm_client = None
        # {"currencies": {CUR: {"regime", "source", "updated_at", "rate",
        #                       "rationale", "decisions": [...]}},
        #  "seen_events": [event_key, ...]}
        self._state = {"currencies": {}, "seen_events": []}
        self._load()
        self._apply_seed(seed or {})

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('currencies'), dict):
                    self._state = {
                        "currencies": data['currencies'],
                        "seen_events": list(data.get('seen_events') or []),
                    }
                    logger.info(f"Regime tracker: loaded "
                                f"{len(self._state['currencies'])} currencies")
        except Exception as e:
            logger.warning(f"Regime tracker state unreadable, starting fresh: {e}")

    def _save_locked(self):
        """Atomic write; caller holds the lock."""
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            tmp = self._state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.error(f"Regime tracker save failed: {e}")

    def _apply_seed(self, seed: Dict[str, str]):
        """Seed currencies the state doesn't know yet (fresh deploy). Never
        overwrites tracked state — observed decisions outrank the seed."""
        changed = False
        with self._lock:
            for cur, regime in seed.items():
                cur = (cur or '').upper()
                if regime in REGIMES and cur and cur not in self._state['currencies']:
                    self._state['currencies'][cur] = {
                        "regime": regime, "source": "seed",
                        "updated_at": utcnow().isoformat() + "Z",
                        "rate": None, "rationale": "config seed",
                        "decisions": [],
                    }
                    changed = True
            if changed:
                self._save_locked()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, currency: str) -> Optional[str]:
        with self._lock:
            entry = self._state['currencies'].get((currency or '').upper())
            return entry['regime'] if entry else None

    def all(self) -> Dict:
        with self._lock:
            return json.loads(json.dumps(self._state['currencies']))

    def set_manual(self, currency: str, regime: str) -> Dict:
        """Panel override. Wins until the bank's next OBSERVED decision
        (unless set AFTER that decision's release — then it outranks it).
        Only currencies already known to the state are accepted — a typo'd
        or empty currency would otherwise persist as a phantom row with no
        delete path."""
        currency = (currency or '').upper().strip()
        if regime not in REGIMES:
            raise ValueError(f"regime must be one of {REGIMES}")
        with self._lock:
            if currency not in self._state['currencies']:
                raise ValueError(f"unknown currency '{currency}'")
            entry = self._state['currencies'][currency]
            entry.update({"regime": regime, "source": "manual",
                          "updated_at": utcnow().isoformat() + "Z",
                          "rationale": "manual override (panel)"})
            self._save_locked()
            logger.info(f"Regime override: {currency} -> {regime} (manual)")
            return json.loads(json.dumps(entry))

    def scan(self, path_records: List[Dict]) -> int:
        """Feed recent event-path records (newest first, as get_recent returns
        them); processes unseen rate decisions that already have an 'actual'.
        Returns how many new decisions were observed. At most ONE LLM
        adjudication per scan pass — a backlog replay (fresh state file)
        must not stall the updater thread with back-to-back 30s calls;
        further ambiguous holds in the same pass fall back to the
        deterministic rule (acceptable: only the newest decision drives
        live tagging, and a backlog is a rare cold-start artifact)."""
        processed = 0
        llm_budget = 1
        for r in reversed(path_records or []):   # oldest first
            try:
                if r.get('test') or r.get('actual') in (None, ''):
                    continue
                if not is_rate_decision(r.get('event_name', '')):
                    continue
                key = r.get('event_key') or (f"{r.get('currency')}|"
                                             f"{(r.get('event_time') or '')[:16]}")
                with self._lock:
                    if key in self._state['seen_events']:
                        continue
                    # Reserve atomically: check-then-act under ONE lock, so a
                    # concurrent scan can't double-append the same decision,
                    # and a permanently-unparseable record is never retried
                    # on every cleanup cycle forever
                    self._state['seen_events'] = (self._state['seen_events']
                                                  [-199:] + [key])
                observed, llm_used = self._observe(key, r,
                                                   llm_allowed=llm_budget > 0)
                if observed:
                    processed += 1
                if llm_used:
                    llm_budget -= 1
            except Exception as e:
                logger.warning(f"Regime scan failed for a record: {e}")
        return processed

    # ------------------------------------------------------------------
    # Observation + classification
    # ------------------------------------------------------------------

    def _observe(self, key: str, record: Dict,
                 llm_allowed: bool = True) -> tuple:
        """Process one rate-decision record. Returns (observed, llm_used)."""
        currency = (record.get('currency') or '').upper()
        actual = parse_numeric(record.get('actual'))
        previous = parse_numeric(record.get('previous'))
        if actual is None:
            return False, False

        if previous is None:
            with self._lock:
                previous = (self._state['currencies'].get(currency) or {}).get('rate')

        if previous is None:
            action = "hold"   # first-ever observation, no reference — neutral
        elif actual > previous:
            action = "hike"
        elif actual < previous:
            action = "cut"
        else:
            action = "hold"

        decision = {
            "date": (record.get('event_time') or '')[:16],
            "event": record.get('event_name', ''),
            "action": action,
            "rate": actual,
        }

        with self._lock:
            entry = self._state['currencies'].setdefault(
                currency, {"regime": "hold", "source": "rule", "rate": None,
                           "rationale": "", "decisions": []})
            entry['decisions'] = (entry.get('decisions') or [])[-(DECISIONS_KEPT - 1):] + [decision]
            entry['rate'] = actual
            # Persist the observed decision NOW — a failure later (e.g. in
            # the LLM path) must not lose it across a restart
            self._save_locked()
            det = self._deterministic(entry['decisions'])

        regime, source, rationale = det, "rule", f"calendar: {action} to {actual}"
        llm_used = False

        # Ambiguity worth an LLM look: bank HELD while the cycle direction is
        # still alive — pause within the cycle, or end of it?
        if action == "hold" and det != "hold" and self._llm_enabled and llm_allowed:
            llm_used = True
            verdict = self._llm_adjudicate(currency, record, entry_snapshot={
                "decisions": list(entry['decisions']), "cycle": det})
            if verdict and verdict.get('regime') in (det, "hold"):
                regime = verdict['regime']
                source = "llm"
                # 'rationale' may be present-but-null in otherwise valid JSON
                rationale = str(verdict.get('rationale') or '')[:300]

        with self._lock:
            entry = self._state['currencies'][currency]
            old = entry.get('regime')
            # An operator override set AFTER this release (they saw the
            # decision and chose a stance) outranks the automatic verdict;
            # the decision itself is already persisted above
            manual_after_release = (
                entry.get('source') == 'manual'
                and str(entry.get('updated_at') or '')[:19]
                    > str(record.get('event_time') or '')[:19])
            if not manual_after_release:
                entry.update({"regime": regime, "source": source,
                              "updated_at": utcnow().isoformat() + "Z",
                              "rationale": rationale})
                self._save_locked()
        if manual_after_release:
            logger.info(f"Regime observation for {currency} kept manual "
                        f"override (set after the release) — auto verdict "
                        f"was {regime}")
        elif old != regime:
            logger.warning(f"REGIME CHANGE: {currency} {old} -> {regime} "
                           f"({source}; {rationale})")
        else:
            logger.info(f"Regime confirmed: {currency} = {regime} "
                        f"({action} @ {actual}, {source})")
        return True, llm_used

    @staticmethod
    def _deterministic(decisions: List[Dict], as_of: datetime = None) -> str:
        """Facts-only classification from the observed decision list.
        as_of: staleness reference point — utcnow() live, the EVENT time in
        historical replay (else every old cycle would look expired)."""
        last_move = None
        holds_since = 0
        for d in reversed(decisions):
            if d.get('action') in ("hike", "cut"):
                last_move = d
                break
            holds_since += 1
        if last_move is None:
            return "hold"
        if holds_since >= HOLDS_TO_NEUTRAL:
            return "hold"
        try:
            moved_at = to_naive_utc(datetime.fromisoformat(last_move['date']))
            if (as_of or utcnow()) - moved_at > timedelta(days=STALE_MOVE_DAYS):
                return "hold"
        except (ValueError, TypeError, KeyError):
            pass
        return "hiking" if last_move['action'] == "hike" else "cutting"

    # ------------------------------------------------------------------
    # LLM adjudication (holds only)
    # ------------------------------------------------------------------

    def _get_llm(self):
        if self._llm_client is not None:
            return self._llm_client
        try:
            from config import OPENROUTER_API_KEY, POSITION_MANAGEMENT_CONFIG
            api_key = OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                return None
            from openai import OpenAI
            self._llm_client = {
                "client": OpenAI(api_key=api_key,
                                 base_url="https://openrouter.ai/api/v1"),
                "model": (POSITION_MANAGEMENT_CONFIG.get("exit_llm_model")
                          or "anthropic/claude-sonnet-4"),
            }
            return self._llm_client
        except Exception as e:
            logger.debug(f"Regime LLM client unavailable: {e}")
            return None

    def _llm_adjudicate(self, currency: str, record: Dict,
                        entry_snapshot: Dict) -> Optional[Dict]:
        llm = self._get_llm()
        if llm is None:
            return None

        cycle = entry_snapshot['cycle']
        history_lines = "\n".join(
            f"- {d.get('date')}: {d.get('action').upper()} (rate {d.get('rate')})"
            for d in entry_snapshot['decisions'][-6:])
        reaction = ""
        if record.get('move_5min_pips') is not None:
            # Project invariant: NEVER leave base/quote semantics implicit —
            # +20 pips on NZDUSD after a USD event means USD WEAKENED
            pair = (record.get('pair') or '').upper()
            if len(pair) >= 6 and currency == pair[:3]:
                semantics = (f"{currency} is the BASE of {pair}: positive "
                             f"pips = {currency} STRENGTHENED")
            elif len(pair) >= 6 and currency == pair[3:6]:
                semantics = (f"{currency} is the QUOTE of {pair}: positive "
                             f"pips = {currency} WEAKENED")
            else:
                semantics = f"pair {pair} (unknown side for {currency})"
            reaction = (f"Measured market reaction on {pair}: "
                        f"{record.get('move_1min_pips')} pips/1min, "
                        f"{record.get('move_5min_pips')} pips/5min, "
                        f"{record.get('move_30min_pips')} pips/30min. "
                        f"NOTE: {semantics}.")

        prompt = f"""Central bank rate decision just released for {currency}:
event: {record.get('event_name')}
actual: {record.get('actual')} | forecast: {record.get('forecast')} | previous: {record.get('previous')}
The bank HELD the rate. Its recent decision history:
{history_lines}
The prior cycle direction was: {cycle.upper()}.
{reaction}

Question: is this hold a PAUSE WITHIN the {cycle} cycle (more moves in that
direction still expected), or the END of the cycle (neutral stance now)?
Consider the reaction: e.g. a hawkish hold usually strengthens the currency.

Respond with ONLY a JSON object:
{{"rationale": "one or two sentences, max 40 words", "regime": "{cycle}" or "hold"}}"""

        try:
            response = llm["client"].chat.completions.create(
                model=llm["model"],
                messages=[
                    {"role": "system",
                     "content": "You are a monetary-policy analyst. Answer "
                                "with ONLY the requested JSON object."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0.0,
                timeout=30.0,
            )
            text = (response.choices[0].message.content or "").strip()
            if '{' in text and '}' in text:
                text = text[text.find('{'):text.rfind('}') + 1]
            data = json.loads(text)
            if isinstance(data, dict) and data.get('regime') in REGIMES:
                logger.info(f"Regime LLM verdict for {currency}: "
                            f"{data.get('regime')} — {data.get('rationale')}")
                return data
        except Exception as e:
            logger.warning(f"Regime LLM adjudication failed ({currency}): {e}")
        return None
