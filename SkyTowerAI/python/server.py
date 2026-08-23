"""
SkyTower-AI Flask Server
Provides REST API for MT5 Expert Advisor communication
"""
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from datetime import datetime, timedelta
from timeutil import utcnow, to_naive_utc
import json
import math
import os
import threading
from typing import Optional
import time
from loguru import logger

from config import SERVER_CONFIG, TRADING_CONFIG, ZONE_CONFIG, EXIT_CONFIG, POSITION_MANAGEMENT_CONFIG
# NOTE: HIGH_IMPACT_EVENTS is deliberately NOT imported at module level —
# the panel rebinds cfg.HIGH_IMPACT_EVENTS at runtime and a module-level
# binding here would pin the import-time list (callers pass
# event_keywords=None so calendar_fetcher reads it fresh).
from config import CURRENCY_PAIRS, DEFAULT_PAIRS
from instrument_profiles import profile_for, zone_config_for
from trading_units import forex_pip_size
from market_context import (build_market_context, normalize_pair,
                            summarize_pair_brief, entry_age_seconds)
from calendar_fetcher import CalendarAggregator
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from llm_decision_engine import LLMDecisionEngine, TradingDecision
from zone_analyzer import ZoneAnalyzer, PriceBar, analyze_from_ohlc_data
from target_calculator import TargetCalculator
from position_manager import (
    PositionConflictError,
    PositionManager,
    PositionPersistenceError,
)
from exit_decision_engine import ExitDecisionEngine
from decision_history import DecisionHistory
from event_path_recorder import EventPathRecorder
from regime_tracker import RegimeTracker
import event_cluster

app = Flask(__name__)
CORS(app)
# Largest legitimate payload is the EA's ohlc_multi push (~15 KB) — cap well
# above that so a malformed/hostile client can't allocate arbitrary memory
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

# Global instances
decision_engine = None
calendar = None
zone_analyzer = None
target_calculator = None
position_manager = None
exit_engine = None
decision_history = None
path_recorder = None
regime_tracker = None
next_decision = None
decision_lock = threading.Lock()

# Event analysis tracking (BUG-6 FIX)
# Tracks which events have been analyzed (SKIP) to avoid re-analyzing
analyzed_events = {}  # {"event_key": datetime_analyzed}

# Failed analysis attempts per event key. An exception used to retire the event
# for 24h on the FIRST failure, so one transient error (network blip, full
# disk) ended trading for that release even with most of the preload window
# left. Bounded so a permanent error still cannot loop forever.
_analysis_failures = {}  # {"event_key": (attempts, last_failure_utc)}
MAX_ANALYSIS_ATTEMPTS = 3
# Room needed before a retry is worth starting (a full panel plus serving)
MIN_ANALYSIS_RETRY_SECONDS = 60

# Multi-instance coordination
# Structure: { "event_key": { "pair": {...data...}, "pair2": {...} }, ... }
registered_pairs = {}
pair_lock = threading.Lock()
executed_trades = set()  # Tracks executed event_keys to prevent duplicates

# Per-pair market data pushed by the EA (works in single-instance mode,
# unlike registered_pairs which needs multi-instance + a known event).
# Structure: { "EURUSD": {"ohlc_multi": {"M5": [...], ...}, "current_price": ..., "updated_at": ...} }
market_data_reports = {}
market_data_lock = threading.Lock()


def init_services():
    """Initialize all services"""
    global decision_engine, calendar, zone_analyzer, target_calculator, position_manager, exit_engine, decision_history, path_recorder, regime_tracker
    logger.info("Initializing SkyTower-AI services...")

    from config import ACTIVE_POSITION_FILE, TRADE_HISTORY_FILE
    # DecisionHistory must be created first and SHARED with the engine —
    # the engine's TRACK RECORD section reads the same instance the server
    # records into (a private copy never sees in-session decisions)
    decision_history = DecisionHistory()
    # Regime tracking: fed automatically by recorded rate decisions; the
    # config map only SEEDS a fresh state (observed decisions outrank it).
    # Created BEFORE the engine — the LEARNED EVENT STATISTICS prompt section
    # selects its per-regime bucket through this provider.
    from config import CURRENCY_REGIMES
    regime_tracker = RegimeTracker(seed=CURRENCY_REGIMES)
    decision_engine = LLMDecisionEngine(decision_log=decision_history,
                                        trade_history_file=TRADE_HISTORY_FILE,
                                        regime_provider=regime_tracker.get,
                                        # Deferred: path_recorder is created a
                                        # few lines below; the lambda resolves
                                        # the module global at call time
                                        paths_provider=lambda: (
                                            path_recorder.get_recent(2000)
                                            if path_recorder else []))
    calendar = CalendarAggregator()
    zone_analyzer = ZoneAnalyzer(ZONE_CONFIG)
    target_calculator = TargetCalculator(ZONE_CONFIG)
    exit_engine = ExitDecisionEngine()
    position_manager = PositionManager(
        exit_engine=exit_engine,
        history_file=TRADE_HISTORY_FILE,
        state_file=ACTIVE_POSITION_FILE,
        # Recovers the forced marker from the decision row when a reconcile
        # report arrives without it (older EA build after a restart)
        forced_lookup=lambda did: any(
            row.get("decision_id") == did and row.get("forced")
            for row in decision_history.get_recent(200)
        ))
    # Post-event price paths for ALL monitored events (traded or not) —
    # measured server-side from EA-pushed M1, the system's learning substrate
    path_recorder = EventPathRecorder(regime_provider=regime_tracker.get)

    # The EA has no calendar, so reaction records used to carry null
    # forecast/previous and a permanent "surprise: UNKNOWN". Wire the store to
    # the server's own knowledge of the event — no network, no EA change.
    decision_engine.reaction_history.set_event_lookup(_reaction_event_lookup)

    logger.info("Services initialized successfully (with AI Position Manager + Decision History)")


def ensure_services():
    """Ensure services are initialized (lazy initialization)"""
    global decision_engine, calendar, zone_analyzer, target_calculator, position_manager, exit_engine, decision_history
    if decision_engine is None or calendar is None:
        init_services()


@app.route('/')
def dashboard():
    """Serve the web dashboard"""
    return render_template('dashboard.html')


@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Return recent log lines for dashboard"""
    import os
    lines = request.args.get('lines', 50, type=int)
    log_path = os.path.join(os.path.dirname(__file__), 'logs', 'server.log')
    try:
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
                return jsonify({"status": "ok", "lines": [l.rstrip() for l in recent]})
        return jsonify({"status": "ok", "lines": ["No log file found"]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/config/events', methods=['GET', 'POST'])
def config_events():
    """Get or update event filter configuration"""
    import config as cfg

    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            # Effective (filtered) lists — what trade selection uses now
            "tier1_events": cfg.TIER1_EVENTS,
            "tier2_events": cfg.TIER2_EVENTS,
            "all_events": cfg.HIGH_IMPACT_EVENTS,
            # Full rosters + the disabled subset: the dashboard renders its
            # checkboxes from THESE (never from a list baked into the HTML —
            # a stale client roster clipped every roster addition on Save)
            "tier1_events_all": cfg.TIER1_EVENTS_ALL,
            "tier2_events_all": cfg.TIER2_EVENTS_ALL,
            "disabled_events": cfg.disabled_event_names(),
            "currencies": list(cfg.CURRENCY_PAIRS.keys()),
            "min_impact": getattr(cfg, "MIN_IMPACT_LEVEL", "MEDIUM"),
            "trade_all_events": getattr(cfg, "TRADE_ALL_EVENTS", False),
            "non_data_markers": getattr(cfg, "NON_DATA_EVENT_MARKERS", []),
        })

    # POST: update event config at runtime
    data = request.json or {}
    # NOTE: the legacy 'tier1_events'/'tier2_events' write branch was removed
    # (18.08.2026). It had no caller (dashboard, EA and RUNBOOK only ever read
    # those fields from the GET), no validation and no persistence, and it
    # assigned the request body straight into cfg.TIER1_EVENTS — breaking the
    # invariant that the effective lists are a SUBSET of the *_ALL rosters.
    # A string body made HIGH_IMPACT_EVENTS a string, which the tradeable
    # predicate then iterated per CHARACTER (calendar_fetcher.py:660) so that
    # 'c' matched almost every release.
    # The dashboard's Save sends {events: [...checked names...]} — a flat
    # whitelist across both tiers, drawn from the FULL rosters it received
    # via GET. Everything else in the rosters becomes disabled; persisted as
    # `disabled_events` (roster additions after a Save stay enabled — the
    # old `enabled_events` key clipped them, see config.LEGACY_PANEL_EVENT_ROSTER).
    if 'events' in data and isinstance(data['events'], list) \
            and all(isinstance(e, str) for e in data['events']):
        # 'roster' = the names the client actually rendered; names outside it
        # keep their state, so a tab opened before a roster-changing restart
        # cannot disable what it never displayed. A client that sends no
        # roster is scoped to config.LEGACY_PANEL_EVENT_ROSTER (that IS the
        # pre-18.08.2026 dashboard, whose Save would otherwise silently and
        # PERMANENTLY re-disable the six names it never had a checkbox for);
        # a script that really wants the full complement sends
        # "roster": "*" (cfg.ROSTER_ALL).
        roster_arg = data.get('roster')
        if not (cfg._is_str_list(roster_arg) or roster_arg == cfg.ROSTER_ALL):
            logger.warning(
                "POST /api/config/events without a 'roster' field - this is a "
                "pre-18.08.2026 dashboard or a script. Only the legacy roster "
                "is in scope; names outside it keep their current state. Hard-"
                f"refresh the panel (Ctrl+F5), or send \"roster\": \"{cfg.ROSTER_ALL}\" "
                "to disable the complement of the FULL roster on purpose.")
        disabled = cfg.set_enabled_events(data['events'], known_roster=roster_arg)
        if disabled:
            logger.info(f"Event whitelist: {len(disabled)} roster name(s) disabled "
                        f"via dashboard: {disabled}")
        if not cfg.HIGH_IMPACT_EVENTS:
            logger.warning("Dashboard saved an EMPTY event whitelist — no "
                           "named events will trade while TRADE_ALL_EVENTS "
                           "is off")
        else:
            logger.info(f"Event whitelist set via dashboard (persisted): "
                        f"{len(cfg.HIGH_IMPACT_EVENTS)} events enabled")
    if 'min_impact' in data:
        level = str(data['min_impact']).strip().upper()
        if level in ("LOW", "MEDIUM", "HIGH"):
            cfg.MIN_IMPACT_LEVEL = level
            cfg.save_runtime_overrides({"min_impact": level})
            logger.info(f"Min impact level set to {level} via dashboard (persisted)")
    if 'trade_all_events' in data:
        flag = bool(data['trade_all_events'])
        cfg.TRADE_ALL_EVENTS = flag
        cfg.save_runtime_overrides({"trade_all_events": flag})
        logger.info(f"Trade-all-events set to {flag} via dashboard (persisted)")
    return jsonify({"status": "ok", "message": "Event config updated"})


@app.route('/api/config/risk', methods=['GET', 'POST'])
def config_risk():
    """
    Dashboard-editable risk limits (persisted across restarts via
    logs/runtime_overrides.json). PositionManager reads the live config
    dict on every check, so changes apply immediately.
    Single source of truth: max_loss_usd is also delivered to the EA in
    every /api/signal response (the EA no longer has a duplicate input).
    """
    import config as cfg

    if request.method == 'GET':
        pm = cfg.POSITION_MANAGEMENT_CONFIG
        return jsonify({
            "status": "ok",
            "max_daily_trades": pm.get("max_daily_trades"),
            "max_daily_loss_usd": pm.get("max_daily_loss_usd"),
            "max_loss_usd": pm.get("max_loss_usd"),
            "profit_protection_percent": pm.get("profit_protection_percent"),
            "profit_protection_floor_pct": pm.get("profit_protection_floor_pct"),
            "profit_protection_grace_seconds": pm.get("profit_protection_grace_seconds"),
        })

    data = request.json or {}
    updated = {}
    for key, lo, hi, cast in (("max_daily_trades", 1, 100, int),
                              ("max_daily_loss_usd", 10, 1_000_000, float),
                              ("max_loss_usd", 5, 100_000, float),
                              ("profit_protection_percent", 10, 95, float),
                              ("profit_protection_floor_pct", 5, 200, float),
                              ("profit_protection_grace_seconds", 0, 600, int)):
        if key in data:
            try:
                value = cast(data[key])
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": f"{key}: not a number"}), 400
            if not (lo <= value <= hi):
                return jsonify({"status": "error",
                                "message": f"{key}: must be between {lo} and {hi}"}), 400
            updated[key] = value

    # Cross-field check BEFORE mutating live config: a per-trade budget above
    # the daily budget is always an operator mistake (the daily limit only
    # blocks the NEXT entry, and max_loss_usd also sizes the EA's lot). Reject
    # loudly here rather than clamping silently — the panel shows the message.
    if updated:
        prospective = dict(cfg.POSITION_MANAGEMENT_CONFIG)
        prospective.update(updated)
        conflicts = cfg.risk_limit_conflicts(prospective)
        if conflicts:
            return jsonify({"status": "error",
                            "message": "; ".join(conflicts)}), 400
        cfg.POSITION_MANAGEMENT_CONFIG.update(updated)

    if updated:
        cfg.save_runtime_overrides(updated)
        logger.info(f"Risk limits updated via dashboard: {updated}")
    return jsonify({"status": "ok", "updated": updated})


@app.route('/api/config/models', methods=['GET', 'POST'])
def config_models():
    """
    Dashboard-editable AI model setup (Event Config -> AI Models card),
    persisted via logs/runtime_overrides.json (default < .env < panel).
    Applies LIVE: the engines read self.model per call and the ensemble
    reads module globals, so the NEXT decision uses the new setup without
    a restart. Validation is all-or-nothing: one bad field rejects the
    whole POST and nothing is applied.
    """
    import config as cfg
    import llm_decision_engine as lde
    ensure_services()

    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            "entry_model": cfg.LLM_CONFIG.get("model"),
            "exit_model": cfg.POSITION_MANAGEMENT_CONFIG.get("exit_llm_model"),
            "ensemble_k": cfg.ENSEMBLE_K,
            "ensemble_models": list(cfg.ENSEMBLE_MODELS),
            "panel_active": len(cfg.ENSEMBLE_MODELS) >= 2,
        })

    data = request.json or {}
    staged = {}

    def _model_id(value, field):
        s = str(value).strip()
        if not s or '/' not in s or ' ' in s:
            raise ValueError(f"{field}: expected an OpenRouter id like "
                             f"'vendor/model', got '{value}'")
        return s

    try:
        if 'entry_model' in data:
            staged['entry_model'] = _model_id(data['entry_model'], 'entry_model')
        if 'exit_model' in data:
            staged['exit_model'] = _model_id(data['exit_model'], 'exit_model')
        if 'ensemble_k' in data:
            k = int(data['ensemble_k'])
            if not 1 <= k <= 5:
                raise ValueError("ensemble_k: must be between 1 and 5")
            staged['ensemble_k'] = k
        if 'ensemble_models' in data:
            raw = data['ensemble_models']
            if isinstance(raw, str):
                raw = raw.split(',')
            if not isinstance(raw, list):
                raise ValueError("ensemble_models: expected a list or a "
                                 "comma-separated string")
            models = [str(p).strip() for p in raw if str(p).strip()]
            if models and len(models) < 2:
                raise ValueError("ensemble_models: needs >= 2 models "
                                 "(or empty to disable the panel)")
            if len(models) > 5:
                raise ValueError("ensemble_models: max 5 models")
            staged['ensemble_models'] = [_model_id(m, 'ensemble_models')
                                         for m in models]
    except (TypeError, ValueError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    if 'entry_model' in staged:
        cfg.LLM_CONFIG['model'] = staged['entry_model']
        if decision_engine is not None:
            decision_engine.model = staged['entry_model']
    if 'exit_model' in staged:
        cfg.POSITION_MANAGEMENT_CONFIG['exit_llm_model'] = staged['exit_model']
        exit_eng = getattr(position_manager, 'exit_engine', None)
        if exit_eng is not None:
            exit_eng.model = staged['exit_model']
    if 'ensemble_k' in staged:
        cfg.ENSEMBLE_K = staged['ensemble_k']
        lde.ENSEMBLE_K = staged['ensemble_k']
    if 'ensemble_models' in staged:
        cfg.ENSEMBLE_MODELS = list(staged['ensemble_models'])
        lde.ENSEMBLE_MODELS = list(staged['ensemble_models'])

    if staged:
        cfg.save_runtime_overrides(staged)
        logger.info(f"AI model config updated via dashboard: {staged}")
    return jsonify({"status": "ok", "updated": staged})


@app.route('/api/config/routing', methods=['GET', 'POST'])
def config_routing():
    """Event -> instrument routing (multi-instrument). GET returns the live
    table plus, per routed symbol, whether its EA chart has fresh data and
    whether that chart's echoed pip unit matches the profile; POST sets the
    table (dict {"USD": ["XAUUSD"]} or string "USD:XAUUSD;NZD:NZDUSD"),
    validated by config.normalize_instrument_routing(strict=True) — the same
    rule the env/file paths apply — and persisted (default < env < panel)."""
    import config as cfg

    def _live_status():
        out = {}
        with market_data_lock:
            for cur, syms in cfg.INSTRUMENT_ROUTING.items():
                rows = []
                for sym in syms:
                    state = _routed_symbol_state(sym)
                    prof = profile_for(sym)
                    rows.append({"symbol": sym,
                                 "has_data": state["entry"] is not None,
                                 "fresh": state["fresh"],
                                 "age_seconds": (int(state["age"]) if state["age"] is not None
                                                 else None),
                                 "unit_ok": state["unit_ok"],
                                 "reported_pip_size": state["reported_pip"],
                                 "profile": prof.name if prof else None})
                out[cur] = rows
        return out

    if request.method == 'GET':
        return jsonify({"status": "ok",
                        "instrument_routing": cfg.INSTRUMENT_ROUTING,
                        "live": _live_status(),
                        "default_pairs": DEFAULT_PAIRS})

    data = request.json or {}
    if 'instrument_routing' not in data:
        return jsonify({"status": "error",
                        "message": "instrument_routing missing"}), 400
    try:
        table = cfg.normalize_instrument_routing(data['instrument_routing'], strict=True)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    cfg.INSTRUMENT_ROUTING = table
    cfg.save_runtime_overrides({"instrument_routing": table})
    logger.info(f"Instrument routing set via dashboard (persisted): {table}")
    return jsonify({"status": "ok", "instrument_routing": table, "live": _live_status()})


@app.route('/api/decisions/history', methods=['GET'])
def get_decision_history():
    """Get decision audit log — all LLM decisions (BUY/SELL/SKIP)"""
    ensure_services()
    limit = request.args.get('limit', 20, type=int)
    today_only = request.args.get('today', 'false').lower() == 'true'

    if decision_history is None:
        return jsonify({"status": "ok", "count": 0, "decisions": []})

    if today_only:
        decisions = decision_history.get_today()
    else:
        decisions = decision_history.get_recent(limit=limit)

    return jsonify({
        "status": "ok",
        "count": len(decisions),
        "decisions": decisions
    })


@app.route('/api/trade-log/<decision_id>', methods=['GET'])
def get_trade_log(decision_id):
    """Position-management trail (AI exit decisions, close reason, P/L) of
    the closed trade bound to a decision_id. Fetched lazily when a Decision
    History row is expanded — recent_trades in /api/position/status is
    deliberately slimmed (no ai_decisions), this is the fat single-trade
    view. Returns trade: null when the decision never became a trade."""
    ensure_services()
    trade = position_manager.get_trade_by_decision(decision_id)
    return jsonify({"status": "ok", "trade": trade})


# This handler makes uncached upstream calls (a COT fetch alone can take 30s),
# so anything that polls it must not reach the network on every hit.
_datasource_status_cache = {"at": 0.0, "payload": None}
DATASOURCE_STATUS_TTL = 120


@app.route('/api/datasources/status', methods=['GET'])
def get_datasource_status():
    """Check which data sources are currently responding."""
    ensure_services()
    if (_datasource_status_cache["payload"] is not None
            and time.time() - _datasource_status_cache["at"] < DATASOURCE_STATUS_TTL):
        return jsonify(_datasource_status_cache["payload"])
    status = {}

    # Test COT
    try:
        cot_result = decision_engine.cot_analyzer.analyze_currency("USD")
        has_error = 'error' in cot_result if isinstance(cot_result, dict) else True
        status["cot"] = {
            "status": "error" if has_error else "ok",
            "detail": cot_result.get("error", f"signal={cot_result.get('signal', 'UNKNOWN')}") if isinstance(cot_result, dict) else "unknown",
        }
    except Exception as e:
        status["cot"] = {"status": "error", "detail": str(e)}

    # Test sentiment
    try:
        sent_result = decision_engine.sentiment.get_currency_sentiment("USD")
        pairs = sent_result.get("pairs_analyzed", 0) if isinstance(sent_result, dict) else 0
        status["sentiment"] = {
            "status": "ok" if pairs > 0 else "no_data",
            "pairs_analyzed": pairs,
            # Per-source outcome: "no_data" alone cannot distinguish a blocked
            # source from one that parsed nothing
            "sources": getattr(decision_engine.sentiment, 'last_status', {}),
        }
    except Exception as e:
        status["sentiment"] = {"status": "error", "detail": str(e)}

    # Test calendar
    try:
        events = calendar.get_upcoming_events(hours_ahead=168)
        status["calendar"] = {
            "status": "ok" if len(events) > 0 else "no_events",
            "events_found": len(events),
        }
    except Exception as e:
        status["calendar"] = {"status": "error", "detail": str(e)}

    payload = {"status": "ok", "data_sources": status}
    _datasource_status_cache.update(at=time.time(), payload=payload)
    return jsonify(payload)


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        # Identifies OUR server — START.bat checks this to tell a running
        # SkyTower apart from a foreign app squatting on the port (e.g. adb)
        "service": "SkyTower-AI",
        "timestamp": utcnow().isoformat(),
        "version": "4.1.0"
    })


@app.route('/api/next-event', methods=['GET'])
def get_next_event():
    """Get the next tradeable economic event"""
    ensure_services()
    try:
        event = calendar.get_next_tradeable_event()
        if event:
            return jsonify({
                "status": "ok",
                "event": event.to_dict()
            })
        else:
            return jsonify({
                "status": "ok",
                "event": None,
                "message": "No upcoming tradeable events"
            })
    except Exception as e:
        logger.error(f"Error getting next event: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/events', methods=['GET'])
def get_upcoming_events():
    """Get all upcoming high-impact events"""
    ensure_services()
    try:
        hours = request.args.get('hours', 168, type=int)  # Default 1 week
        currencies = request.args.get('currencies', 'NZD,CAD,AUD,USD,GBP').split(',')

        impact = request.args.get('impact', 'MEDIUM').upper()
        events = calendar.get_upcoming_events(
            currencies=currencies,
            impact_filter=impact,
            hours_ahead=hours
        )

        # Drop events that already passed — the cached list is recomputed at
        # most every 15 min, so between refreshes a just-passed event would
        # otherwise linger at the top of the table.
        now = utcnow()
        upcoming = []
        for e in events:
            dt = e.datetime_utc
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            if dt >= now:
                upcoming.append(e)

        return jsonify({
            "status": "ok",
            "count": len(upcoming),
            "events": [e.to_dict() for e in upcoming]
        })
    except Exception as e:
        logger.error(f"Error getting events: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/decision', methods=['GET'])
def get_trade_decision():
    """
    Get trading decision for the next event
    This is the main endpoint MT5 will call
    """
    global next_decision
    ensure_services()

    try:
        # NOTE: no eager analysis here — the background updater owns decision
        # making (it analyzes events inside the preload window). Analyzing on
        # demand used to pin a far-future event in next_decision and block
        # the pipeline for days.
        with decision_lock:
            if next_decision:
                return jsonify({
                    "status": "ok",
                    "decision": next_decision.to_dict()
                })
            else:
                return jsonify({
                    "status": "ok",
                    "decision": None,
                    "message": "No active decision (updater analyzes events "
                               "when they enter the preload window)"
                })
    except Exception as e:
        logger.error(f"Error getting decision: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/decision/refresh', methods=['POST'])
def refresh_decision():
    """Force refresh of trading decision"""
    global next_decision
    ensure_services()

    try:
        # Refuse to pin a decision for an event outside the decision window:
        # the updater owns in-window analysis, and a far-future pinned decision
        # would block the pipeline (and could race-arm the EA) until released
        window = TRADING_CONFIG["preload_seconds"] + 60
        # event_keywords=None -> calendar reads cfg.HIGH_IMPACT_EVENTS at
        # call time (panel tier edits apply without restart)
        upcoming = calendar.get_tradeable_events(
            event_keywords=None,
            currencies=list(CURRENCY_PAIRS.keys())
        )
        next_event_seconds = None
        for evt in upcoming:
            evt_time = evt.datetime_utc
            if evt_time.tzinfo is not None:
                evt_time = evt_time.replace(tzinfo=None)
            seconds = (evt_time - utcnow()).total_seconds()
            if seconds > 0:
                next_event_seconds = int(seconds)
                break

        if next_event_seconds is None or next_event_seconds > window:
            return jsonify({
                "status": "ok",
                "message": (f"Next event is {next_event_seconds}s away — outside the "
                            f"{window}s decision window. The background updater will "
                            f"analyze it automatically." if next_event_seconds
                            else "No upcoming tradeable events found.")
            })

        # Run the (paid, 20-60s) analysis OUTSIDE decision_lock — /api/signal
        # and /api/position/opened block on that lock, and this endpoint can
        # only fire inside the entry window. Same pattern as the updater's
        # Phase 2/3.
        recommendation = decision_engine.get_next_trade_recommendation()

        if recommendation and decision_history:
            decision_history.record(recommendation)

        with decision_lock:
            next_decision = recommendation

        if recommendation:
            return jsonify({
                "status": "ok",
                "message": "Decision refreshed",
                "decision": recommendation.to_dict()
            })
        return jsonify({
            "status": "ok",
            "message": "No trade recommendation available"
        })
    except Exception as e:
        logger.error(f"Error refreshing decision: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/cot/<currency>', methods=['GET'])
def get_cot_data(currency):
    """Get COT analysis for a specific currency"""
    try:
        cot_analyzer = COTAnalyzer()
        result = cot_analyzer.analyze_currency(currency.upper())

        return jsonify({
            "status": "ok",
            "data": result
        })
    except Exception as e:
        logger.error(f"Error getting COT data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/sentiment/<pair>', methods=['GET'])
def get_sentiment(pair):
    """Get sentiment data for a currency pair"""
    try:
        sentiment_agg = SentimentAggregator()
        result = sentiment_agg.get_sentiment(pair.upper())

        if result:
            return jsonify({
                "status": "ok",
                "data": result.to_dict()
            })
        else:
            return jsonify({
                "status": "ok",
                "data": None,
                "message": f"No sentiment data for {pair}"
            })
    except Exception as e:
        logger.error(f"Error getting sentiment: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current trading configuration"""
    return jsonify({
        "status": "ok",
        "config": TRADING_CONFIG,
        "zone_config": ZONE_CONFIG,
        "exit_config": EXIT_CONFIG
    })


@app.route('/api/zones', methods=['POST'])
def analyze_zones():
    """
    Analyze market structure zones for a symbol.

    Request body:
    {
        "symbol": "NZDUSD",
        "ohlc": [
            {"time": 1705600000, "open": 0.62, "high": 0.621, "low": 0.619, "close": 0.6205},
            ...
        ],
        "direction": "BUY"  // Optional - if provided, returns targets
    }

    Returns zone analysis with liquidity pools, FVG, order blocks, and targets.
    """
    ensure_services()
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        symbol = data.get('symbol', '')
        ohlc_data = data.get('ohlc', [])
        direction = data.get('direction')  # Optional

        if not ohlc_data or len(ohlc_data) < 10:
            return jsonify({
                "status": "error",
                "message": "Insufficient OHLC data (need at least 10 bars)"
            }), 400

        # Analyze zones
        zone_result = analyze_from_ohlc_data(ohlc_data, symbol, ZONE_CONFIG)

        response = {
            "status": "ok",
            "analysis": zone_result.to_dict()
        }

        # If direction provided, calculate targets
        if direction and direction in ["BUY", "SELL"]:
            targets = target_calculator.calculate(zone_result, direction)
            is_valid, validation_msg = target_calculator.validate_targets(targets)

            response["targets"] = targets.to_dict()
            response["targets_valid"] = is_valid
            response["validation_message"] = validation_msg

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error analyzing zones: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/zones/<symbol>', methods=['GET'])
def get_zones_simple(symbol):
    """
    Simple zone query endpoint (for testing).

    Uses simulated data - in production, MT5 should send OHLC via POST.
    """
    ensure_services()
    try:
        # For testing, generate mock data
        import random

        mock_bars = []
        base_price = 0.6200 if "NZD" in symbol.upper() else 1.3500

        for i in range(50):
            open_price = base_price + random.uniform(-0.0015, 0.0015)
            close_price = open_price + random.uniform(-0.0010, 0.0010)
            high_price = max(open_price, close_price) + random.uniform(0, 0.0008)
            low_price = min(open_price, close_price) - random.uniform(0, 0.0008)

            mock_bars.append(PriceBar(
                time=int(datetime.now().timestamp()) - (50 - i) * 60,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=random.randint(100, 1000),
            ))
            base_price = close_price

        # Analyze
        result = zone_analyzer.analyze(mock_bars, symbol)

        return jsonify({
            "status": "ok",
            "note": "Using simulated data for testing",
            "analysis": result.to_dict()
        })

    except Exception as e:
        logger.error(f"Error getting zones: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/targets', methods=['GET', 'POST'])
def calculate_targets():
    """
    Calculate trade targets.

    GET: Simple request with URL params (symbol, direction, entry_price)
         Returns default targets based on config (no zone analysis)

    POST: Full request with OHLC data for zone-based analysis
    """
    ensure_services()

    # Handle GET request (simple, no zone analysis)
    if request.method == 'GET':
        symbol = request.args.get('symbol', 'NZDUSD')
        direction = request.args.get('direction', 'BUY')
        entry_price = float(request.args.get('entry_price', 0))

        logger.info(f"GET /api/targets - {symbol} {direction} @ {entry_price}")

        if direction not in ["BUY", "SELL"]:
            return jsonify({"status": "error", "message": "Direction must be BUY or SELL"}), 400

        # Use default pip-based targets from config (instrument-aware pip:
        # the old forex literal produced sub-cent targets on XAUUSD)
        pip_size = forex_pip_size(symbol)

        # Default targets: 15 pips TP1, 25 pips TP2, 20 pips SL
        tp1_pips = EXIT_CONFIG.get('default_tp1_pips', 15)
        tp2_pips = EXIT_CONFIG.get('default_tp2_pips', 25)
        sl_pips = EXIT_CONFIG.get('default_sl_pips', 20)

        if direction == "BUY":
            tp1 = entry_price + (tp1_pips * pip_size)
            tp2 = entry_price + (tp2_pips * pip_size)
            sl = entry_price - (sl_pips * pip_size)
        else:
            tp1 = entry_price - (tp1_pips * pip_size)
            tp2 = entry_price - (tp2_pips * pip_size)
            sl = entry_price + (sl_pips * pip_size)

        return jsonify({
            "status": "ok",
            "targets": {
                "tp1": round(tp1, 5),
                "tp2": round(tp2, 5),
                "sl": round(sl, 5),
                "tp1_pips": tp1_pips,
                "tp2_pips": tp2_pips,
                "sl_pips": sl_pips,
                "risk_reward": round(tp1_pips / sl_pips, 2),
                "tp1_close_percent": 50,
                "tp2_close_percent": 100,
                "move_sl_to_be": True,
                "confidence": 0.7,
                "method": "default"
            },
            "valid": True,
            "validation_message": "Default targets applied"
        })

    # Handle POST request (full zone analysis)
    logger.info(f"POST /api/targets received - Content-Length: {request.content_length}")
    try:
        # Try to parse JSON, handle potential errors
        try:
            raw_data = request.data
            logger.info(f"Raw data length: {len(raw_data) if raw_data else 0}")

            # Manual JSON parsing - more tolerant than Flask's get_json
            if raw_data:
                raw_str = raw_data.decode('utf-8', errors='ignore')
                logger.info(f"Raw string first 200 chars: {raw_str[:200]}")
                data = json.loads(raw_str)
            else:
                data = None
        except json.JSONDecodeError as json_err:
            logger.error(f"JSON decode error: {json_err}")
            logger.error(f"Raw data preview: {raw_data[:500] if raw_data else b'empty'}")
            return jsonify({"status": "error", "message": f"Invalid JSON: {json_err}"}), 400
        except Exception as e:
            logger.error(f"Parse error: {type(e).__name__}: {e}")
            return jsonify({"status": "error", "message": f"Parse error: {e}"}), 400

        if not data:
            logger.error("No data in request")
            return jsonify({"status": "error", "message": "No data provided"}), 400

        symbol = data.get('symbol', '')
        direction = data.get('direction', '')
        entry_price = data.get('entry_price')
        ohlc_data = data.get('ohlc', [])

        if direction not in ["BUY", "SELL"]:
            return jsonify({
                "status": "error",
                "message": "Direction must be BUY or SELL"
            }), 400

        if not ohlc_data or len(ohlc_data) < 10:
            return jsonify({
                "status": "error",
                "message": "Insufficient OHLC data"
            }), 400

        # Analyze zones
        zone_result = analyze_from_ohlc_data(ohlc_data, symbol, ZONE_CONFIG)

        # Calculate targets
        targets = target_calculator.calculate(
            zone_result,
            direction,
            entry_price=entry_price
        )

        is_valid, validation_msg = target_calculator.validate_targets(targets)

        return jsonify({
            "status": "ok",
            "targets": targets.to_dict(),
            "valid": is_valid,
            "validation_message": validation_msg,
            "zone_bias": zone_result.direction_bias,
            "zone_bias_strength": zone_result.bias_strength
        })

    except Exception as e:
        logger.error(f"Error calculating targets: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =============================================================================
# MULTI-INSTANCE COORDINATION ENDPOINTS
# =============================================================================

def get_event_key(event_currency: str, event_time: datetime) -> str:
    """Generate unique key for an event"""
    time_str = event_time.strftime("%Y%m%d_%H%M")
    return f"{event_currency}_{time_str}"


@app.route('/api/register-pair', methods=['POST'])
def register_pair():
    """
    Register a currency pair for an upcoming event.
    Called by each EA instance with its pair's technical data.

    Request body:
    {
        "pair": "GBPUSD",
        "event_currency": "GBP",
        "event_time": "2026-01-23T12:00:00",
        "current_price": 1.2345,
        "spread_points": 15,
        "zones": {
            "resistance_zones": [...],
            "support_zones": [...],
            "fvg_zones": [...],
            "direction_bias": "bullish",
            "bias_strength": 0.7
        },
        "ohlc": [...]  # Optional - last N bars for LLM analysis
    }
    """
    global registered_pairs

    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        pair = data.get('pair', '').upper()
        event_currency = data.get('event_currency', '').upper()
        event_time_str = data.get('event_time', '')

        if not pair or not event_currency or not event_time_str:
            return jsonify({
                "status": "error",
                "message": "pair, event_currency, and event_time are required"
            }), 400

        # Parse event time
        event_time = datetime.fromisoformat(event_time_str.replace('Z', '+00:00'))
        if event_time.tzinfo is not None:
            event_time = event_time.replace(tzinfo=None)

        event_key = get_event_key(event_currency, event_time)

        with pair_lock:
            if event_key not in registered_pairs:
                registered_pairs[event_key] = {}

            registered_pairs[event_key][pair] = {
                "pair": pair,
                "current_price": data.get('current_price', 0),
                "spread_points": data.get('spread_points', 0),
                "spread_pips": data.get('spread_pips'),
                "zones": data.get('zones', {}),
                "ohlc": data.get('ohlc', []),
                "registered_at": utcnow().isoformat()
            }

            pair_count = len(registered_pairs[event_key])

        logger.info(f"Pair registered: {pair} for {event_key} (total: {pair_count} pairs)")

        return jsonify({
            "status": "ok",
            "message": f"Pair {pair} registered for {event_currency} event",
            "event_key": event_key,
            "registered_pairs": pair_count
        })

    except Exception as e:
        logger.error(f"Error registering pair: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/market-data', methods=['POST'])
def report_market_data():
    """
    Endpoint for the EA to push current OHLC data per pair.
    Works in single-instance mode (no event key needed) — the background
    updater picks the right pair's data when analyzing an event.

    Request body:
    {
        "pair": "EURUSD",
        "current_price": 1.0842,
        "spread_points": 12,
        "ohlc_multi": {
            "M5":  [{"time": ..., "open": ..., "high": ..., "low": ..., "close": ...}, ...],
            "M15": [...],
            "H1":  [...]
        }
    }
    """
    global market_data_reports

    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        # Normalized (bare, suffix-free) name: the same form the EA can resolve
        # back to its broker symbol, and the form /api/signal compares against
        pair = normalize_pair(data.get('pair', ''))
        ohlc_multi = data.get('ohlc_multi', {})

        if not pair:
            return jsonify({"status": "error", "message": "pair is required"}), 400
        if not isinstance(ohlc_multi, dict) or not any(ohlc_multi.values()):
            return jsonify({"status": "error", "message": "ohlc_multi with at least one timeframe is required"}), 400

        # Clamp bar counts — the EA sends at most 60 per timeframe
        ohlc_multi = {tf: (bars[-200:] if isinstance(bars, list) else [])
                      for tf, bars in ohlc_multi.items()}

        # Bars-stalled detection: a chart keeps pushing (updated_at fresh)
        # through the gold daily break / weekend / a feed halt while its
        # newest M1 bar never advances. Track when the bars last moved so
        # entry_age_seconds can refuse such a chart (routing, recorder).
        newest_bar = _newest_bar_time(ohlc_multi)
        now_iso = utcnow().isoformat()
        offset_echo = data.get('broker_utc_offset_sec')
        try:
            offset_echo = int(float(offset_echo)) if offset_echo is not None else None
        except (TypeError, ValueError):
            offset_echo = None
        with market_data_lock:
            previous = market_data_reports.get(pair) or {}
            advanced_at = now_iso
            if (newest_bar is not None and previous.get('last_bar_time') == newest_bar
                    and previous.get('bars_advanced_at')):
                advanced_at = previous['bars_advanced_at']
            market_data_reports[pair] = {
                "pair": pair,
                "ohlc_multi": ohlc_multi,
                "spread_points": data.get('spread_points', 0),
                "spread_pips": data.get('spread_pips'),
                # EA >= 17.08.2026 echoes its effective pip (InpPipSizeOverride
                # or the forex rule); the server refuses to route/serve a chart
                # whose unit disagrees with instrument_profiles / the forex rule
                "pip_size": data.get('pip_size'),
                # EA >= 23.08.2026 echoes its broker clock offset; None = infer
                "broker_utc_offset_sec": offset_echo,
                "last_bar_time": newest_bar,
                "bars_advanced_at": advanced_at,
                "updated_at": now_iso
            }

        bars_info = {tf: len(bars) for tf, bars in ohlc_multi.items() if bars}
        # DEBUG: 4 charts x every 60s would drown the decision/trade logs
        logger.debug(f"Market data received for {pair}: {bars_info}")

        return jsonify({"status": "ok", "message": f"Market data stored for {pair}"})

    except Exception as e:
        logger.error(f"Error processing market data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _find_pair_data(store: dict, wanted_pair: str, currency: str = None):
    """
    Find an entry in a pair-keyed store by normalized name (suffix-tolerant).
    Fallback (only when `currency` given): any pair whose BASE currency is the
    event currency — never a quote-currency pair, because the rule engine maps
    "currency bullish" straight to BUY of the suggested pair and a quote-side
    match would invert the trade direction.
    """
    wanted = normalize_pair(wanted_pair)
    for key, value in store.items():
        if normalize_pair(key) == wanted:
            return value
    if currency:
        cur = currency.upper()
        for key, value in store.items():
            if normalize_pair(key).startswith(cur):
                return value
    return None


# Ignore EA-pushed OHLC older than this — a stale entry from a dead chart
# must not claim decision.pair (no live EA would ever receive that signal)
MARKET_DATA_MAX_AGE_SECONDS = 1800


def _newest_bar_time(ohlc_multi) -> Optional[int]:
    """Newest M1 bar time of a push (any timeframe as fallback), None when
    no bar carries a usable time."""
    if not isinstance(ohlc_multi, dict):
        return None
    for tf in ("M1", "M5", "M15", "H1"):
        bars = ohlc_multi.get(tf)
        if isinstance(bars, list) and bars:
            try:
                t = int(bars[-1].get("time", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if t > 0:
                return t
    return None


def _market_data_age_seconds(entry) -> float:
    # Thin wrapper over the canonical helper (market_context.entry_age_seconds)
    # so the server gates and the path recorder can never disagree on age
    return entry_age_seconds(entry)


def _unit_mismatch(symbol: str, entry) -> bool:
    """True when the EA on this chart reports a pip unit that disagrees with
    the server's unit for the symbol (profile pip_size, or the forex rule).

    Fail-closed for PROFILED instruments: an absent / zero / malformed echo on
    XAUUSD, GER40, US500 can only mean an EA build without InpPipSizeOverride
    (or a chart whose symbol info is not up yet) — i.e. a unit the server
    cannot vouch for — so it counts as a mismatch. Forex pairs keep backward
    compatibility with EA builds that echo nothing (None -> not checkable).
    Relative tolerance 1% (float formatting)."""
    if not entry:
        return False
    profiled = profile_for(symbol) is not None
    reported = entry.get('pip_size')
    try:
        reported = float(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported = None
    if reported is None or reported <= 0:
        return profiled
    expected = forex_pip_size(symbol)
    return abs(reported - expected) > 0.01 * expected


# Tickets whose reports already raised a unit-mismatch error (log once)
_unit_mismatch_reported = set()


def _unit_check_label(symbol: str, entry) -> Optional[bool]:
    """unit_ok for the panel: True/False when verifiable, None when the EA
    echoes nothing on a FOREX chart (old build; not enforced there)."""
    if not entry:
        return None
    reported = entry.get('pip_size')
    if reported in (None, "", 0, 0.0) and profile_for(symbol) is None:
        return None
    return not _unit_mismatch(symbol, entry)


def _routed_symbol_state(symbol: str) -> dict:
    """{'entry','age','fresh','unit_ok','reported_pip'} for a routed symbol
    from market_data_reports (caller holds market_data_lock). ONE definition
    of 'fresh' for routing and for the panel card."""
    entry = _find_pair_data(market_data_reports, symbol)
    age = _market_data_age_seconds(entry) if entry else None   # inf when the stamp is missing
    if age is not None and age == float('inf'):
        age = None                                              # "unknown", never int(inf)
    fresh = bool(entry) and age is not None and age <= MARKET_DATA_MAX_AGE_SECONDS
    return {"entry": entry, "age": age, "fresh": fresh,
            "unit_ok": _unit_check_label(symbol, entry),
            "reported_pip": (entry or {}).get('pip_size')}


def _pick_routed_market_entry(currency: str):
    """(symbol, market_entry) for the FIRST instrument in
    config.INSTRUMENT_ROUTING[currency] whose EA chart has pushed fresh
    market data in the RIGHT unit — or None (routing off / no routed chart
    alive / unit mismatch), in which case the caller keeps the DEFAULT_PAIRS
    flow untouched.

    Exact root match only (normalize_pair): a routed non-forex symbol must
    never be satisfied by the base-currency fallback of _find_pair_data, and
    a dead or mis-configured chart must never claim decision.pair (no EA
    would receive the signal, or would execute it in the wrong unit). Caller
    holds market_data_lock.
    """
    import config as cfg
    for symbol in cfg.routing_candidates(currency):
        state = _routed_symbol_state(symbol)
        if state["entry"] is None:
            continue
        if not state["fresh"]:
            age_txt = (f"{int(state['age'] // 60)} min" if state["age"] is not None
                       else "no timestamp")
            logger.info(f"Routed instrument {symbol} for {currency} has stale data "
                        f"({age_txt}) — skipping")
            continue
        if state["unit_ok"] is False:
            logger.warning(f"Routed instrument {symbol} for {currency}: EA reports pip_size "
                           f"{state['reported_pip']} but the server unit is "
                           f"{forex_pip_size(symbol)} — chart mis-configured or EA build "
                           f"without InpPipSizeOverride, skipping")
            continue
        return normalize_pair(symbol), state["entry"]
    return None


# Timeframes to try for stop-cluster (equal-high/low) detection, best first.
# M15/M5 recent swings are where a news trade's retail stops actually cluster;
# H1 is a coarse fallback. ZoneAnalyzer.analyze() needs >= 10 bars.
_LIQUIDITY_TF_PREFERENCE = ("M15", "M5", "H1", "M30", "M1")


def _liquidity_pools_from_ohlc(ohlc_multi, pair_name):
    """Run the server's ZoneAnalyzer on the OHLC we already hold to surface
    equal-high/low STOP CLUSTERS (liquidity pools) for the decision pair.
    find_liquidity_pools previously ran only on mock data in test endpoints —
    the live entry decision never saw it. Returns
    {"liquidity_pools": [{price, strength, touches}, ...], "liquidity_tf": tf}
    (nearest cluster first, both sides of price) or None. Pair-exact by
    construction (the pair's own OHLC), so the levels are safe to size against."""
    if not isinstance(ohlc_multi, dict):
        return None
    bars = tf_used = None
    for tf in _LIQUIDITY_TF_PREFERENCE:
        candidate = ohlc_multi.get(tf)
        if isinstance(candidate, list) and len(candidate) >= 10:
            bars, tf_used = candidate, tf
            break
    if bars is None:
        return None
    try:
        # Profiled instruments substitute their own pip-denominated
        # detection thresholds (gold: $1.50 tolerance instead of $0.30 —
        # with the forex value stop clusters were never found there).
        result = analyze_from_ohlc_data(bars, pair_name,
                                        zone_config_for(pair_name, ZONE_CONFIG))
    except Exception as e:
        logger.debug(f"Liquidity-pool analysis failed for {pair_name}: {e}")
        return None
    pools = [{"price": round(z.midpoint, 5),
              "strength": z.strength.value,
              "touches": z.touches}
             for z in (result.liquidity_above[:3] + result.liquidity_below[:3])]
    if not pools:
        return None
    return {"liquidity_pools": pools, "liquidity_tf": tf_used}


def _build_market_context_for_event(event):
    """
    Assemble the market context for an event from EA-pushed data:
    fresh market_data_reports OHLC for the suggested pair (or a pair with the
    event currency as base), zones from registered_pairs (multi-instance) and
    zone_reports — zones matched EXACTLY to the OHLC pair, never cross-pair.
    Returns a dict for LLMDecisionEngine.analyze_event(), or None.
    """
    try:
        currency = event.currency.upper()
        suggested = DEFAULT_PAIRS.get(currency, f"{currency}/USD")

        ohlc_multi = {}
        zones = None
        pair_name = normalize_pair(suggested)
        data_timestamp = None
        spread_points = None
        spread_pips = None

        with market_data_lock:
            # Event -> instrument routing (config.INSTRUMENT_ROUTING): a routed
            # instrument (e.g. XAUUSD for USD prints) claims the decision only
            # when ITS chart has pushed fresh data — exact symbol match, no
            # base-currency fallback. Otherwise: today's DEFAULT_PAIRS flow.
            routed = _pick_routed_market_entry(currency)
            if routed is not None:
                suggested, market_entry = routed
                pair_name = normalize_pair(suggested)
                logger.info(f"Routing {currency} event to {pair_name} "
                            f"(instrument routing, fresh EA data)")
            else:
                market_entry = _find_pair_data(market_data_reports, suggested, currency)
            if market_entry and _market_data_age_seconds(market_entry) > MARKET_DATA_MAX_AGE_SECONDS:
                logger.info(f"Ignoring stale market data for {market_entry.get('pair')} "
                            f"({int(_market_data_age_seconds(market_entry) // 60)} min old)")
                market_entry = None
            if market_entry:
                ohlc_multi = market_entry.get('ohlc_multi', {})
                pair_name = market_entry.get('pair', pair_name)
                data_timestamp = market_entry.get('updated_at')
                spread_points = market_entry.get('spread_points')
                spread_pips = market_entry.get('spread_pips')

        # Multi-instance registration may carry zone data for this event
        event_time = event.datetime_utc
        if event_time.tzinfo is not None:
            event_time = event_time.replace(tzinfo=None)
        event_key = get_event_key(currency, event_time)
        with pair_lock:
            if event_key in registered_pairs:
                reg_entry = _find_pair_data(registered_pairs[event_key], pair_name)
                if reg_entry and reg_entry.get('zones'):
                    zones = dict(reg_entry['zones'])

        # Zone reports: exact pair match only — mixing another pair's price
        # levels would produce absurd pip distances for SL/TP sizing
        with zone_reports_lock:
            zone_entry = _find_pair_data(zone_reports, pair_name)
            if zone_entry:
                zones = {**(zones or {}), **zone_entry}

        # Stop clusters (equal-high/low liquidity pools) computed SERVER-SIDE
        # from the OHLC we already hold — the ZoneAnalyzer capability was
        # otherwise unused in the live decision path. Extra keys only; never
        # clobbers the EA-reported bias / S-R levels.
        liq = _liquidity_pools_from_ohlc(ohlc_multi, pair_name)
        if liq:
            zones = {**(zones or {}), **liq}

        context = build_market_context(
            ohlc_multi, pair_name, zones=zones, registered_at=data_timestamp,
            spread_points=spread_points, spread_pips=spread_pips,
            broker_utc_offset_sec=(market_entry or {}).get('broker_utc_offset_sec'),
        )

        # CROSS-PAIR PICTURE: brief summaries of OTHER fresh pairs that
        # contain the event currency (broader view of the currency's state)
        cross = _build_cross_pair_summaries(currency, pair_name)
        if cross:
            if context is None:
                context = {"pair": pair_name}
            context["cross_pairs"] = cross

        return context
    except Exception as e:
        logger.warning(f"Could not build market context for {event.event_name}: {e}")
        return None


def _build_cross_pair_summaries(currency: str, exclude_pair: str, cap: int = 3):
    """
    One-line technical briefs of other FRESH pairs containing the event
    currency (base or quote), from EA-pushed market_data_reports. The
    decision pair itself is excluded — its full context is already in the
    prompt. Each line states the currency-strength direction explicitly
    (base vs quote semantics). Returns a list of strings (possibly empty).
    """
    currency = (currency or '').upper()
    excluded = normalize_pair(exclude_pair)
    summaries = []
    try:
        with market_data_lock:
            entries = [(key, dict(value)) for key, value in market_data_reports.items()]
        decision_profile = profile_for(excluded)
        for key, entry in entries:
            norm = normalize_pair(key)
            if norm == excluded:
                continue
            # Only FOREX pairs carrying the currency: a profiled instrument
            # (XAUUSD $0.10 pips) never appears in a forex prompt, and a
            # profiled decision only sees forex briefs (labelled) — pip
            # magnitudes are not comparable across the asset-class boundary
            if profile_for(norm) is not None:
                continue
            if len(norm) < 6 or currency not in (norm[:3], norm[3:6]):
                continue
            if _market_data_age_seconds(entry) > MARKET_DATA_MAX_AGE_SECONDS:
                continue
            brief = summarize_pair_brief(
                entry.get('ohlc_multi', {}), norm, currency,
                spread_points=entry.get('spread_points'),
                spread_pips=entry.get('spread_pips'),
            )
            if brief:
                if decision_profile is not None:
                    brief = f"{brief} [forex pips, not {decision_profile.name} pips]"
                summaries.append(brief)
            if len(summaries) >= cap:
                break
    except Exception as e:
        logger.debug(f"Cross-pair summaries failed: {e}")
    return summaries


@app.route('/api/event-reaction', methods=['POST'])
def report_event_reaction():
    """
    EA reports how price actually reacted to a released event.
    Called once per event, ~5 minutes after release.

    Request body:
    {
        "pair": "EURUSD",
        "event_name": "CPI m/m",
        "currency": "USD",
        "event_time": "2026-07-10T13:30:00",
        "price_at_event": 1.0842,
        "price_after_1min": 1.0830,
        "price_after_5min": 1.0801
    }
    forecast/previous/actual are backfilled later from the ForexFactory feed
    (see _backfill_reaction_actuals).
    """
    ensure_services()
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        required = ("pair", "event_name", "currency", "price_at_event")
        missing = [k for k in required if not data.get(k)]
        if missing:
            return jsonify({"status": "error", "message": f"Missing fields: {missing}"}), 400

        # forecast/previous/actual are backfilled later from the ForexFactory feed
        entry = decision_engine.reaction_history.record(data)
        return jsonify({"status": "ok", "reaction": entry})

    except Exception as e:
        logger.error(f"Error recording event reaction: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/maintenance/enrich-reactions', methods=['POST'])
def enrich_reactions():
    """Operator-triggered one-shot: fill forecast/previous on historical
    reaction rows from logs/decision_context/. Does NOT touch 'actual' and does
    NOT hit the network.

    Safe with the server running — it goes through the store's own lock and
    atomic_rewrite_jsonl. An out-of-band edit to logs/event_reactions.jsonl
    would instead be silently reverted, because the store loads the file once
    at startup and rewrites it whole from memory.
    """
    ensure_services()
    try:
        history = decision_engine.reaction_history
        filled = history.enrich_missing_context()
        if not getattr(history, 'last_enrich_persisted', True):
            # In-memory only: a restart would silently undo it, and a retry
            # would report 0 ("already done") while disk is still null.
            return jsonify({
                "status": "error",
                "updated": filled,
                "message": (f"enriched {filled} record(s) in memory but the "
                            f"rewrite of event_reactions.jsonl FAILED — see "
                            f"server.log, then restart and retry"),
            }), 500
        return jsonify({"status": "ok", "updated": filled})
    except Exception as e:
        logger.error(f"Reaction enrich failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/event-reactions', methods=['GET'])
def get_event_reactions():
    """List recorded event reactions. Filters: ?event=CPI&currency=USD&limit=20"""
    ensure_services()
    event_name = request.args.get('event', '')
    currency = request.args.get('currency', '')
    limit = request.args.get('limit', 50, type=int)

    history = decision_engine.reaction_history
    if event_name and currency:
        reactions = history.get_matching(event_name, currency, limit)
    else:
        reactions = history.get_recent(limit)

    return jsonify({"status": "ok", "count": len(reactions), "reactions": reactions})


_playbook_proposals = None   # lazy PlaybookProposals store (F5)
_playbook_proposals_lock = threading.Lock()


def _proposals_store():
    # Lock: two first-callers racing here would get two store instances,
    # and decide()'s per-instance lock could then double-apply a proposal
    global _playbook_proposals
    with _playbook_proposals_lock:
        if _playbook_proposals is None:
            from config import PLAYBOOK_PROPOSALS_FILE
            from playbook_distiller import PlaybookProposals
            _playbook_proposals = PlaybookProposals(PLAYBOOK_PROPOSALS_FILE)
        return _playbook_proposals


@app.route('/api/playbooks/distill', methods=['POST'])
def api_playbooks_distill():
    """Draft a playbook update for one event from the MEASURED statistics
    (F5). Synchronous LLM call (~10-30s) — triggered manually from the
    dashboard, never automatically. The result is a PENDING proposal; the
    operator approves/rejects it separately."""
    ensure_services()
    try:
        data = request.json or {}
        event_name = str(data.get('event_name') or '').strip()
        currency = str(data.get('currency') or '').strip().upper()
        if not event_name or len(currency) != 3:
            return jsonify({"status": "error",
                            "message": "event_name and 3-letter currency required"}), 400

        from llm_util import make_chat_fn
        chat_fn = make_chat_fn(max_tokens=600, timeout=60.0)
        if chat_fn is None:
            return jsonify({"status": "error",
                            "message": "LLM unavailable (no API key)"}), 503

        from playbook_distiller import (generate_proposal, find_playbook_key)
        from event_reaction_history import normalize_event_name
        stats = decision_engine._load_learned_stats()
        events = stats.get('events', {})
        if not events:
            # _load_learned_stats swallows a broken/absent file into {} (fine
            # for the prompt section, which just disappears). HERE that would
            # surface as a confident "z danymi: brak" — a false statement
            # pointing the operator away from the real problem (the FILE).
            return jsonify({"status": "error",
                            "message": ("Learned stats niedostępne/puste "
                                        "(knowledge/learned_stats.json) — "
                                        "odbuduj: python tools/build_learned_stats.py "
                                        "i sprawdź server.log")}), 503
        key_stats = f"{currency}|{normalize_event_name(event_name)}"
        learned = events.get(key_stats)
        if learned is None:
            # Bundle members (e.g. Core CPI m/m) carry their stats under the
            # dominant release — same alias fallback the entry prompt uses
            alias = (stats.get('bundle_alias') or {}).get(key_stats)
            learned = events.get((alias or {}).get('to'))
        if learned is None:
            # Nothing MEASURED to distill. The old path drafted BLIND here — a
            # real-looking entry with no measured basis (502 only when the
            # reply was garbage). Instead: guide the operator to names that DO
            # have data. Ranked typed-substring match first, then the tradeable
            # whitelist (a raw n-sort would bury USD "CPI m/m" at rank 18
            # under junk like Crude Oil Inventories n=286), then sample size.
            from config import HIGH_IMPACT_EVENTS
            wanted = [w.lower() for w in HIGH_IMPACT_EVENTS]
            typed = event_name.lower()
            avail = []
            for k, e in events.items():
                if not isinstance(e, dict):
                    continue     # one bad regen row must not 500 the guidance
                if (e.get('currency') or k.split('|', 1)[0]).upper() != currency:
                    continue
                name = e.get('event_name') or k.split('|', 1)[-1]
                try:
                    n = int(e.get('n_releases') or 0)
                except (TypeError, ValueError):
                    n = 0
                avail.append((typed in name.lower(),
                              any(w in name.lower() for w in wanted),
                              n, name))
            avail.sort(key=lambda t: (not t[0], not t[1], -t[2], t[3]))
            names = ", ".join(f"{t[3]} (n={t[2]})"
                              for t in avail[:12]) or "brak"
            return jsonify({"status": "error",
                            "message": (f"Brak zmierzonej próbki dla {currency} "
                                        f"\"{event_name}\". Wpisz dokładną nazwę "
                                        f"(z sufiksem m/m / q/q) — {currency} "
                                        f"z danymi: {names}"),
                            "available": [t[3] for t in avail]}), 404
        playbooks = decision_engine._load_playbooks()
        key = find_playbook_key(playbooks, event_name)
        current = playbooks.get(key) if isinstance(playbooks.get(key), dict) else None

        proposal = generate_proposal(chat_fn, event_name, currency,
                                     learned, current, key)
        if proposal is None:
            return jsonify({"status": "error",
                            "message": "model returned no usable proposal"}), 502
        if not _proposals_store().add(proposal):
            return jsonify({"status": "error",
                            "message": "proposal could not be persisted"}), 500
        logger.info(f"Playbook proposal drafted: {currency} {event_name} "
                    f"({proposal['id'][:8]})")
        return jsonify({"status": "ok", "proposal": proposal})
    except Exception as e:
        logger.error(f"Error distilling playbook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/playbooks/distill-batch', methods=['POST'])
def api_playbooks_distill_batch():
    """One click drafts playbook proposals for EVERY tradeable event with a
    meaty measured sample (F5, operator request): candidates are gated in
    playbook_distiller.select_batch_candidates (n>=10 releases, tradeable
    name, no pending proposal, 14-day cooldown, cap 10/click). Calls run in
    parallel; every draft still lands as PENDING — the Approve gate is
    untouched. Manual trigger only, never scheduled."""
    ensure_services()
    try:
        from llm_util import make_chat_fn
        chat_fn = make_chat_fn(max_tokens=600, timeout=60.0)
        if chat_fn is None:
            return jsonify({"status": "error",
                            "message": "LLM unavailable (no API key)"}), 503

        from playbook_distiller import (select_batch_candidates,
                                        generate_proposal)
        from config import HIGH_IMPACT_EVENTS
        store = _proposals_store()
        learned = decision_engine._load_learned_stats().get('events', {})
        if not learned:
            # Same guard as the single-event endpoint: a broken/absent stats
            # file must not masquerade as a green "Drafted 0, all counters 0"
            return jsonify({"status": "error",
                            "message": ("Learned stats niedostępne/puste "
                                        "(knowledge/learned_stats.json) — "
                                        "odbuduj: python tools/build_learned_stats.py "
                                        "i sprawdź server.log")}), 503
        playbooks = decision_engine._load_playbooks()
        candidates, skipped = select_batch_candidates(
            learned, playbooks, store.list(limit=500), HIGH_IMPACT_EVENTS,
            utcnow().isoformat())
        if not candidates:
            return jsonify({"status": "ok", "generated": [],
                            "skipped": skipped,
                            "message": "no candidates with fresh data"})

        from concurrent.futures import ThreadPoolExecutor

        def draft(c):
            return c, generate_proposal(chat_fn, c["event_name"],
                                        c["currency"], c["learned"],
                                        (playbooks.get(c["playbook_key"])
                                         if isinstance(playbooks.get(c["playbook_key"]), dict)
                                         else None),
                                        c["playbook_key"])

        generated, failed = [], 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for c, proposal in pool.map(draft, candidates):
                if proposal is not None and store.add(proposal):
                    generated.append(f"{c['currency']} {c['event_name']}")
                else:
                    failed += 1
        logger.info(f"Batch distillation: {len(generated)} drafted, "
                    f"{failed} failed, skipped={skipped}")
        return jsonify({"status": "ok", "generated": generated,
                        "failed": failed, "skipped": skipped})
    except Exception as e:
        logger.error(f"Error in batch distillation: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/playbooks/proposals', methods=['GET'])
def api_playbooks_proposals():
    """Distillation proposals, newest first (?status=pending)."""
    try:
        status = request.args.get('status') or None
        return jsonify({"status": "ok",
                        "proposals": _proposals_store().list(status=status)})
    except Exception as e:
        logger.error(f"Error listing playbook proposals: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/playbooks/proposals/decide', methods=['POST'])
def api_playbooks_decide():
    """Operator gate (F5): approve applies the entry to
    knowledge/event_playbooks.json (hot-reloaded by the engine); reject
    only marks the proposal. Body: {id, action: approve|reject}."""
    try:
        data = request.json or {}
        from config import EVENT_PLAYBOOKS_FILE
        result = _proposals_store().decide(str(data.get('id') or ''),
                                           str(data.get('action') or ''),
                                           EVENT_PLAYBOOKS_FILE)
        if not result.get('ok'):
            return jsonify({"status": "error",
                            "message": result.get('error')}), 400
        logger.info(f"Playbook proposal {data.get('action')}d: "
                    f"{result['proposal'].get('currency')} "
                    f"{result['proposal'].get('event_name')}")
        return jsonify({"status": "ok", "proposal": result['proposal']})
    except Exception as e:
        logger.error(f"Error deciding playbook proposal: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/calibration', methods=['GET'])
def api_calibration():
    """Calibration ledger (F4): past decisions incl. SKIPs scored against
    the measured post-event paths — Brier, hit rate vs stated confidence,
    reliability buckets, skip outcomes. Dashboard card + audit."""
    ensure_services()
    try:
        from calibration import build_summary
        decisions = decision_history.get_recent(300) if decision_history else []
        paths = path_recorder.get_recent(2000) if path_recorder else []
        return jsonify({"status": "ok",
                        "calibration": build_summary(decisions, paths)})
    except Exception as e:
        logger.error(f"Error building calibration summary: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/event-paths', methods=['GET'])
def get_event_paths():
    """Recorded post-event price paths (server-measured, ALL monitored
    events). Filters: ?limit=50"""
    ensure_services()
    limit = request.args.get('limit', 50, type=int)
    paths = path_recorder.get_recent(limit) if path_recorder else []
    return jsonify({"status": "ok", "count": len(paths), "paths": paths})


@app.route('/api/regimes', methods=['GET', 'POST'])
def api_regimes():
    """Monetary-policy regime per currency (auto-tracked from recorded rate
    decisions; LLM adjudicates ambiguous holds). POST {currency, regime}
    sets a manual override that wins until the bank's next observed decision."""
    ensure_services()
    if regime_tracker is None:
        return jsonify({"status": "error", "message": "tracker unavailable"}), 503

    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error",
                            "message": "JSON object body required"}), 400
        try:
            # set_manual validates both fields (unknown currency -> ValueError)
            entry = regime_tracker.set_manual(str(data.get('currency', '')),
                                              str(data.get('regime', '')))
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        return jsonify({"status": "ok",
                        "currency": str(data.get('currency', '')).upper().strip(),
                        "entry": entry})

    return jsonify({"status": "ok", "regimes": regime_tracker.all()})


# Backfill guards: the fetch runs in the single updater thread, so it must
# never stall the decision pipeline or hammer ForexFactory forever
_last_backfill_fetch = 0.0
BACKFILL_MIN_INTERVAL_SECONDS = 900   # at most one FF fetch per 15 min
BACKFILL_MAX_AGE_DAYS = 2             # the FF weekly XML feed is THIS-WEEK-ONLY;
                                      # 7 days promised a resolution it cannot
                                      # deliver and kept dead rows pending

# Imported, not copied: this precheck and the store's own filter gate the same
# rows, and a hand-mirrored literal would drift apart on the first edit.
from event_reaction_history import BACKFILL_MAX_ATTEMPTS  # noqa: E402


def _reaction_event_lookup(decision_id: str, currency: str,
                           event_name: str, event_minute: str) -> dict:
    """forecast/previous for one reaction, from the SERVER's own knowledge.
    Performs NO network I/O — it runs inside the EA's POST to
    /api/event-reaction.

    Order, most authoritative first:
      1. the decision that armed this reaction (decision_id is echoed by the
         EA) — literally the numbers the model was shown;
      2. the path recorder's schedule, filled well before the release;
      3. the calendar cache, read-only.
    """
    from event_reaction_history import normalize_event_name

    currency = (currency or '').upper()
    try:
        if decision_id and decision_history is not None:
            ctx = decision_history.get_context(decision_id) or {}
            event = (ctx.get('data_summary') or {}).get('event') or {}
            if event.get('forecast') or event.get('previous'):
                return {"forecast": event.get('forecast'),
                        "previous": event.get('previous'),
                        "source": "decision"}
    except Exception as e:
        logger.debug(f"Reaction lookup via decision failed: {e}")

    try:
        if path_recorder is not None:
            found = path_recorder.scheduled_event(currency, event_name, event_minute)
            if found:
                return found
    except Exception as e:
        logger.debug(f"Reaction lookup via path recorder failed: {e}")

    try:
        if calendar is not None:
            wanted = normalize_event_name(event_name)
            for event in calendar.peek_cached_events():
                if (event.currency or '').upper() != currency:
                    continue
                if normalize_event_name(event.event_name) != wanted:
                    continue
                if to_naive_utc(event.datetime_utc).isoformat()[:16] != event_minute:
                    continue
                return {"forecast": event.forecast, "previous": event.previous,
                        "source": "calendar"}
    except Exception as e:
        logger.debug(f"Reaction lookup via calendar failed: {e}")

    return {}


def _backfill_reaction_actuals():
    """Fill missing 'actual' values in the reaction log from the calendar feed."""
    global _last_backfill_fetch
    try:
        if decision_engine is None:
            return

        # Never run the (up to 30s) HTTP fetch while a decision is active —
        # an event is imminent and the updater must stay responsive
        with decision_lock:
            if next_decision is not None:
                return

        if time.time() - _last_backfill_fetch < BACKFILL_MIN_INTERVAL_SECONDS:
            return

        history = decision_engine.reaction_history
        cutoff = (utcnow() - timedelta(days=BACKFILL_MAX_AGE_DAYS)).isoformat()
        pending = [r for r in history.get_recent(200)
                   if r.get('actual') in (None, '')
                   and not r.get('test')
                   and r.get('backfill_attempts', 0) < BACKFILL_MAX_ATTEMPTS
                   and (r.get('event_time') or '') >= cutoff]
        # Recorder applies its own (tighter, 48h) window + non-data/attempt
        # exclusions — see EventPathRecorder._backfillable
        paths_pending = (path_recorder is not None
                         and path_recorder.has_pending_actuals())
        if not pending and not paths_pending:
            return

        _last_backfill_fetch = time.time()

        # Only ForexFactory carries released 'actual' values in its weekly feed
        events = []
        for source in getattr(calendar, 'sources', []):
            if 'ForexFactory' in source.__class__.__name__:
                try:
                    events = source.fetch_events(days_ahead=7)
                except Exception as e:
                    logger.warning(f"Backfill calendar fetch FAILED: {e}")
                break

        with_actual = sum(1 for e in events if getattr(e, 'actual', None))
        logger.info(f"Backfill pass: {len(events)} feed events "
                    f"({with_actual} with 'actual'), {len(pending)} reaction rows "
                    f"pending, paths_pending={paths_pending}")
        if events and with_actual == 0:
            # The single most useful line in this function: it distinguishes
            # "nothing has been released yet" from "this source structurally
            # cannot supply 'actual'", which is what has been happening.
            logger.warning("Backfill source returned ZERO released values — the "
                           "ForexFactory weekly XML feed carries no <actual> "
                           "element; 'actual'/'surprise' can never be filled "
                           "from it")

        if events:
            if pending:
                history.backfill_actuals(events)
                logger.info(f"Reaction backfill: {history.last_backfill_stats}")
            if paths_pending:
                path_recorder.backfill_actuals(events)
    except Exception as e:
        logger.warning(f"Reaction backfill error: {e}")


# Global storage for zone data from all EA instances
zone_reports = {}  # pair -> zone_data
zone_reports_lock = threading.Lock()


@app.route('/api/report-zone', methods=['POST'])
def report_zone():
    """
    Endpoint for EA to report its Zone indicator data.
    Called by each EA instance to share its zone analysis.
    This allows the server to use Zone data in pair selection even without Multi-Instance mode.

    Request body:
    {
        "pair": "GBPJPY",
        "zone_bias": 0.65,           // -1 to +1, positive = bullish
        "direction_bias": "bullish", // bullish/bearish/neutral
        "nearest_resistance": 193.50,
        "nearest_support": 192.00,
        "current_price": 192.75,
        "spread_points": 210
    }
    """
    global zone_reports

    try:
        data = request.json
        pair = data.get('pair', '').upper()

        if not pair:
            return jsonify({"status": "error", "message": "pair is required"}), 400

        with zone_reports_lock:
            zone_reports[pair] = {
                "zone_bias": data.get('zone_bias', 0),
                "direction_bias": data.get('direction_bias', 'neutral'),
                "bias_strength": abs(data.get('zone_bias', 0)),
                "nearest_resistance": data.get('nearest_resistance', 0),
                "nearest_support": data.get('nearest_support', 0),
                "current_price": data.get('current_price', 0),
                "spread_points": data.get('spread_points', 0),
                "spread_pips": data.get('spread_pips'),
                "updated_at": utcnow().isoformat()
            }

        logger.debug(f"Zone report received for {pair}: bias={data.get('zone_bias', 0):.2f}")

        return jsonify({
            "status": "ok",
            "message": f"Zone data received for {pair}",
            "pairs_reporting": len(zone_reports)
        })

    except Exception as e:
        logger.error(f"Error processing zone report: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# Tracks which decision was already logged as served (log once, not per poll)
_signal_served_log_key = None
# Snapshot of the decision most recently SERVED to an EA (signal:true).
# The true lineage anchor for /api/position/opened: next_decision may already
# point at the NEXT event (or be cleared) by the time the open report lands.
_last_served_signal = None  # {"decision_id","reasoning","forced","served_at"}


@app.route('/api/signal', methods=['GET'])
def get_mt5_signal():
    """
    Simplified endpoint for MT5
    Returns minimal data needed for trade execution

    Query params:
        pair: Optional - if provided, only returns signal if this pair was selected
              This enables multi-instance mode where only the best pair gets the trade
    """
    global next_decision, executed_trades
    ensure_services()

    try:
        # Check if position manager allows new trades
        can_trade, reason = position_manager.can_open_trade()
        if not can_trade:
            return jsonify({
                "signal": False,
                "message": f"Blocked: {reason}"
            })

        # Get optional pair filter from query params
        requesting_pair = request.args.get('pair', '').upper()

        with decision_lock:
            # No eager analysis here — the background updater preloads
            # decisions inside the event window (see get_trade_decision note)
            if next_decision and next_decision.direction != "SKIP":
                if next_decision.direction not in {"BUY", "SELL"}:
                    logger.error(
                        "Signal blocked by invalid direction: "
                        f"{next_decision.direction!r}"
                    )
                    return jsonify({
                        "signal": False,
                        "message": "Invalid direction in trade decision",
                    })
                raw_lot_percent = getattr(next_decision, "lot_percent", None)
                try:
                    lot_percent = float(raw_lot_percent)
                except (TypeError, ValueError):
                    lot_percent = math.nan
                if (
                    isinstance(raw_lot_percent, bool)
                    or not math.isfinite(lot_percent)
                    or lot_percent <= 0
                    or lot_percent > 100
                ):
                    logger.error(
                        "Signal blocked by invalid lot_percent: "
                        f"{raw_lot_percent!r}"
                    )
                    return jsonify({
                        "signal": False,
                        "message": "Invalid lot_percent in trade decision",
                    })

                # Calculate time until event
                # IMPORTANT: event_time is in UTC, so we must compare with UTC time
                event_time = datetime.fromisoformat(
                    next_decision.data_summary['event']['datetime']
                )
                # Remove timezone info if present (compare naive datetimes in UTC)
                if event_time.tzinfo is not None:
                    event_time = event_time.replace(tzinfo=None)
                # Use utcnow() to compare UTC with UTC
                time_until = (event_time - utcnow()).total_seconds()

                # Only serve signals inside the decision window — a decision
                # for a far event (e.g. pinned via manual /api/decision/refresh)
                # must never arm the EA; the EA stops polling once armed, so a
                # server-side release cannot un-arm it later
                if time_until > TRADING_CONFIG["preload_seconds"] + 60:
                    return jsonify({
                        "signal": False,
                        "message": f"Decision exists but event is {int(time_until)}s away "
                                   f"(signals are served inside the {TRADING_CONFIG['preload_seconds']}s window)"
                    })
                if time_until < 0:
                    return jsonify({
                        "signal": False,
                        "message": "Decision event has already passed",
                    })

                # Bare, suffix-free pair — the EA resolves its broker symbol itself
                decision_pair = normalize_pair(next_decision.pair)

                # Multi-instance mode: check if this pair is the selected one
                if requesting_pair:
                    # Check if trade already executed for this event
                    event_key = get_event_key(
                        next_decision.data_summary['event'].get('currency', ''),
                        event_time
                    )

                    if event_key in executed_trades:
                        return jsonify({
                            "signal": False,
                            "message": "Trade already executed for this event",
                            "event_key": event_key
                        })

                    # Only return signal if this is the selected pair
                    # (suffix-tolerant: EA may request as e.g. USDCAD.PRO)
                    if normalize_pair(requesting_pair) != decision_pair:
                        return jsonify({
                            "signal": False,
                            "message": f"Not selected - best pair is {decision_pair}",
                            "selected_pair": decision_pair,
                            "your_pair": requesting_pair
                        })

                    # Units guard: the EA on this chart echoes its effective
                    # pip in every market-data push. If it disagrees with the
                    # server's unit for the symbol (profile pip_size / forex
                    # rule), stop_loss_pips/take_profit_pips would be executed
                    # in the wrong unit — refuse the signal and say why.
                    with market_data_lock:
                        chart_entry = _find_pair_data(market_data_reports, requesting_pair)
                        mismatch = _unit_mismatch(requesting_pair, chart_entry)
                    if mismatch:
                        expected = forex_pip_size(requesting_pair)
                        reported = chart_entry.get('pip_size')
                        what = (f"EA reports pip_size {reported}" if reported
                                else "EA does not echo pip_size (build without InpPipSizeOverride)")
                        logger.error(f"Signal for {decision_pair} withheld: {what} vs server "
                                     f"{expected} — set InpPipSizeOverride={expected:g} "
                                     f"on that chart (EA build >= 17.08.2026)")
                        return jsonify({
                            "signal": False,
                            "message": (f"Unit mismatch: {what} vs server {expected:g} — set "
                                        f"InpPipSizeOverride on the {decision_pair} chart"),
                            "selected_pair": decision_pair,
                            "your_pair": requesting_pair,
                        })

                sl_pips = getattr(next_decision, 'stop_loss_pips', 0)
                tp_pips = getattr(next_decision, 'take_profit_pips', 0)
                raw_numbers = {
                    "confidence": getattr(next_decision, "confidence", None),
                    "entry_seconds_before": getattr(
                        next_decision, "entry_seconds_before", None
                    ),
                    "exit_minutes": getattr(
                        next_decision, "exit_minutes_after", None
                    ),
                    "stop_loss_percent": getattr(
                        next_decision, "stop_loss_percent", None
                    ),
                    "stop_loss_pips": sl_pips,
                    "take_profit_pips": tp_pips,
                    "max_loss_usd": POSITION_MANAGEMENT_CONFIG.get(
                        "max_loss_usd", 100.0
                    ),
                }
                normalized_numbers = {}
                try:
                    for name, value in raw_numbers.items():
                        if isinstance(value, bool):
                            raise ValueError(name)
                        normalized_numbers[name] = float(value)
                        if not math.isfinite(normalized_numbers[name]):
                            raise ValueError(name)
                except (TypeError, ValueError) as exc:
                    logger.error(
                        "Signal blocked by non-finite numeric field: "
                        f"{exc}"
                    )
                    return jsonify({
                        "signal": False,
                        "message": "Invalid numeric field in trade decision",
                    })

                if (
                    not 0 <= normalized_numbers["confidence"] <= 1
                    or normalized_numbers["entry_seconds_before"] < 0
                    or normalized_numbers["exit_minutes"] <= 0
                    or normalized_numbers["stop_loss_percent"] < 0
                    or normalized_numbers["stop_loss_pips"] < 0
                    or normalized_numbers["take_profit_pips"] < 0
                    or normalized_numbers["max_loss_usd"] <= 0
                ):
                    logger.error(
                        f"Signal blocked by invalid numeric ranges: {raw_numbers}"
                    )
                    return jsonify({
                        "signal": False,
                        "message": "Invalid risk fields in trade decision",
                    })

                # Capture the served decision for lineage stamping at
                # /api/position/opened (see _last_served_signal note)
                global _signal_served_log_key, _last_served_signal
                _last_served_signal = {
                    "decision_id": getattr(next_decision, 'decision_id', ''),
                    "reasoning": next_decision.reasoning,
                    "forced": getattr(next_decision, 'forced', False),
                    "exit_minutes": normalized_numbers["exit_minutes"],
                    "served_at": utcnow(),
                }

                # Trade-lifecycle log: one line when the EA first picks up the signal
                serve_key = f"{next_decision.event}_{event_time.isoformat()}_{requesting_pair}"
                if _signal_served_log_key != serve_key:
                    _signal_served_log_key = serve_key
                    logger.info(f"=== SIGNAL SERVED === {next_decision.direction} {decision_pair} "
                                f"to EA[{requesting_pair or 'any'}] | conf "
                                f"{normalized_numbers['confidence']:.0%} "
                                f"| T-{int(time_until)}s | {next_decision.event}")

                return jsonify({
                    "signal": True,
                    # Lineage key: joins this signal to its decision_history
                    # row; the EA may echo it back in reports (optional field)
                    "decision_id": getattr(next_decision, 'decision_id', ''),
                    "direction": next_decision.direction,
                    "pair": decision_pair,  # MT5 format (no slash)
                    "lot_percent": lot_percent,
                    # Panel-owned per-trade risk budget (USD). Single source of
                    # truth: the EA sizes the lot from this and uses it as its
                    # offline max-loss guardrail (no EA-side duplicate input).
                    "max_loss_usd": normalized_numbers["max_loss_usd"],
                    "confidence": normalized_numbers["confidence"],
                    "entry_seconds_before": normalized_numbers["entry_seconds_before"],
                    "exit_minutes": normalized_numbers["exit_minutes"],
                    "stop_loss_percent": normalized_numbers["stop_loss_percent"],
                    "stop_loss_pips": normalized_numbers["stop_loss_pips"],
                    "take_profit_pips": normalized_numbers["take_profit_pips"],
                    "time_until_event": int(time_until),
                    "event_time": event_time.isoformat(),
                    "event_name": next_decision.event,
                    "event_currency": next_decision.currency,
                    "forced": getattr(next_decision, 'forced', False),
                    "reasoning": next_decision.reasoning
                })
            else:
                return jsonify({
                    "signal": False,
                    "message": "No trade signal"
                })
    except Exception as e:
        logger.error(f"Error getting signal: {e}")
        return jsonify({"signal": False, "error": str(e)}), 500


@app.route('/api/trade-executed', methods=['GET', 'POST'])
def trade_executed():
    """
    Called by MT5 after trade execution
    Marks event as traded to prevent duplicates across EA instances

    NOTE: the EA only calls this as a FALLBACK when its full report to
    /api/position/opened failed — so the trade must still be counted
    against the panel's daily trade limit here (the EA has no per-chart
    gate of its own anymore).
    """
    global next_decision, executed_trades, registered_pairs
    ensure_services()

    try:
        # Get pair from query params or JSON body (silent=True: a bare GET
        # has no JSON content type and must not 500 on the parse itself)
        pair = request.args.get('pair', '')
        if not pair:
            body = request.get_json(silent=True) or {}
            pair = body.get('pair', '')

        # The EA always sends ?pair=<symbol>. A bare hit (operator pasting
        # the URL into a browser, a probing healthcheck) must not burn a
        # daily-trade slot or destroy the pending decision.
        if not pair:
            return jsonify({
                "status": "error",
                "message": "pair is required",
            }), 400

        logger.info(f"Trade executed notification received for {pair}")

        # Count against the daily limit even without position tracking
        position_manager.register_untracked_trade()

        event_key_to_cleanup = None

        # First lock: handle decision state
        with decision_lock:
            if next_decision:
                # Mark this event as executed
                event_time = datetime.fromisoformat(
                    next_decision.data_summary['event']['datetime']
                )
                if event_time.tzinfo is not None:
                    event_time = event_time.replace(tzinfo=None)

                event_key_to_cleanup = get_event_key(
                    next_decision.data_summary['event'].get('currency', ''),
                    event_time
                )
                executed_trades.add(event_key_to_cleanup)
                logger.info(f"Event {event_key_to_cleanup} marked as executed")
                # Stop the updater from re-analyzing this event after we
                # release next_decision (event may still be seconds ahead)
                _mark_decision_event_analyzed(next_decision)

            next_decision = None

        # Second lock: cleanup registered pairs (separate to avoid deadlock)
        if event_key_to_cleanup:
            with pair_lock:
                if event_key_to_cleanup in registered_pairs:
                    del registered_pairs[event_key_to_cleanup]

        return jsonify({"status": "ok", "message": "Trade recorded"})
    except Exception as e:
        logger.error(f"Error recording trade: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =============================================================================
# AI POSITION MANAGEMENT ENDPOINTS
# =============================================================================

@app.route('/api/position/reconcile', methods=['POST'])
def position_reconcile():
    """Reconcile the broker's empty/non-empty position observation."""
    ensure_services()
    try:
        data = request.json
        if not isinstance(data, dict) or "has_position" not in data:
            return jsonify({
                "status": "error",
                "message": "has_position is required",
            }), 422
        if data["has_position"]:
            registration = position_manager.on_position_opened(
                data,
                decision_id=str(data.get("decision_id") or ""),
                # Same reasoning as the reconcile path in update_position: a
                # missing flag must not silently mean "genuine trade".
                forced=position_manager.resolve_forced(data),
                recovered=True,
            )
            status_code = 409 if registration == "conflict" else 200
            return jsonify({
                "status": "conflict" if status_code == 409 else "ok",
                "reconciliation": registration,
                "allow_new_trades": False,
            }), status_code

        result = position_manager.reconcile_empty()
        return jsonify({"status": "ok", **result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 422
    except PositionPersistenceError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503


@app.route('/api/position/opened', methods=['POST'])
def position_opened():
    """
    Called by EA when a new position is opened.
    Initializes position tracking in PositionManager.

    Request body:
    {
        "ticket": 12345,
        "symbol": "NZDUSD",
        "direction": "BUY",
        "entry_price": 0.6200,
        "lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "tick_value": 10.0,
        "account_balance": 5000.00,
        "event_name": "Official Cash Rate"
    }
    """
    global next_decision, executed_trades, registered_pairs
    ensure_services()

    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        # Lineage binding: EA echo (future protocol) wins; else the signal
        # actually SERVED to this EA (captured at /api/signal time — the true
        # anchor); next_decision only as a last resort, because by the time a
        # late open report lands it may already hold the NEXT event's decision.
        entry_reasoning = ""
        forced_flag = False
        planned_exit_minutes = 0
        decision_id = data.get("decision_id", "")
        with decision_lock:
            served = _last_served_signal
            if served and (utcnow() - served["served_at"]).total_seconds() <= 900:
                decision_id = decision_id or served["decision_id"]
                entry_reasoning = served["reasoning"]
                forced_flag = served["forced"]
                planned_exit_minutes = int(served.get("exit_minutes") or 0)
            elif next_decision:
                decision_id = decision_id or getattr(next_decision, 'decision_id', '')
                entry_reasoning = next_decision.reasoning
                forced_flag = getattr(next_decision, 'forced', False)
                planned_exit_minutes = int(
                    getattr(next_decision, 'exit_minutes_after', 0) or 0)

        # forced is STICKY: any source that knows the entry was a
        # FORCE_DECISION coin flip wins. This is the endpoint the EA actually
        # uses to re-adopt a position after a restart, and after a server
        # restart _last_served_signal and next_decision are both empty — so
        # without the EA's own flag (and the decision_history fallback behind
        # it) a demo coin flip re-registered as a genuine trade and every
        # downstream forced filter was defeated for the rest of its life.
        forced_flag = bool(forced_flag) or position_manager.resolve_forced(
            dict(data, decision_id=decision_id)
        )

        registration = position_manager.on_position_opened(
            data,
            entry_reasoning=entry_reasoning,
            decision_id=decision_id,
            forced=forced_flag,
            recovered=bool(data.get("recovered", False)),
            planned_exit_minutes=planned_exit_minutes,
        )
        if registration == "conflict":
            return jsonify({
                "status": "conflict",
                "message": "A different broker position is already tracked",
            }), 409

        # Also mark trade as executed (same as /api/trade-executed)
        event_key_to_cleanup = None
        if registration == "opened" and not data.get("recovered", False):
            with decision_lock:
                if next_decision:
                    event_time = datetime.fromisoformat(
                        next_decision.data_summary['event']['datetime']
                    )
                    if event_time.tzinfo is not None:
                        event_time = event_time.replace(tzinfo=None)

                    event_key_to_cleanup = get_event_key(
                        next_decision.data_summary['event'].get('currency', ''),
                        event_time
                    )
                    executed_trades.add(event_key_to_cleanup)
                    # Stop the updater from re-analyzing this event after we
                    # release next_decision (event may still be seconds ahead)
                    _mark_decision_event_analyzed(next_decision)

                next_decision = None

        if event_key_to_cleanup:
            with pair_lock:
                if event_key_to_cleanup in registered_pairs:
                    del registered_pairs[event_key_to_cleanup]

        logger.info(f"Position opened: {data.get('direction')} {data.get('symbol')} "
                     f"@ {data.get('entry_price')} | ticket={data.get('ticket')}")

        return jsonify({
            "status": "ok",
            "message": "Position registered",
            "registration": registration,
        })

    except ValueError as e:
        logger.warning(f"Invalid position open snapshot: {e}")
        return jsonify({"status": "error", "message": str(e)}), 422
    except PositionPersistenceError as e:
        logger.error(f"Could not persist position open: {e}")
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        logger.error(f"Error registering position: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/position/report', methods=['POST'])
def position_report():
    """
    Called by EA every 5-15s to report position status.
    Returns AI command in the response (combined report+command in one HTTP call).

    Request body:
    {
        "ticket": 12345,
        "symbol": "NZDUSD",
        "direction": "BUY",
        "entry_price": 0.6200,
        "current_price": 0.6215,
        "lots": 0.50,
        "remaining_lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "profit_usd": 75.00,
        "tick_value": 10.0,
        "spread_pips": 2.5,
        "account_balance": 5000.00,
        "zone_bias": 0.35,
        "nearest_resistance": 0.6250,
        "nearest_support": 0.6180
    }

    Response:
    {
        "has_command": true,
        "command": {
            "action": "MODIFY_SL",
            "sl_price": 0.6205,
            "reason": "AI: Moving SL to break-even after $75 profit"
        }
    }
    """
    ensure_services()

    try:
        data = request.json
        if not data:
            return jsonify({"has_command": False, "action": "HOLD"})

        # Units guard on the report path too: the EA echoes its effective pip
        # in every report; a disagreement with the server unit means BE/trail/
        # MODIFY_TP distances would be computed in the wrong unit. Log once per
        # ticket (loud), keep managing (CLOSE is unit-free) — see wiki
        # multi-instrument "Niezmiennik jednostek".
        _report_symbol = str(data.get('symbol') or '')
        if _report_symbol and _unit_mismatch(_report_symbol, data):
            _tkt = str(data.get('ticket'))
            if _tkt not in _unit_mismatch_reported:
                _unit_mismatch_reported.add(_tkt)
                logger.error(f"Position {_tkt} on {_report_symbol}: EA pip_size "
                             f"{data.get('pip_size')} disagrees with server unit "
                             f"{forex_pip_size(_report_symbol)} — pip-denominated exit "
                             f"commands may land at the wrong distance; fix "
                             f"InpPipSizeOverride on that chart")

        # Update position and get command
        result = position_manager.update_position(data)
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error processing position report: {e}")
        return jsonify({"has_command": False, "action": "HOLD", "error": str(e)})


@app.route('/api/position/closed', methods=['POST'])
def position_closed():
    """
    Called by EA when position is closed (by AI command, SL, or EA safety guardrail).

    Request body:
    {
        "ticket": 12345,
        "close_price": 0.6230,
        "profit": 150.00,
        "reason": "AI: TP reached"
    }
    """
    ensure_services()

    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400
        if (
            data.get("position_id") in (None, "", "0")
            and data.get("ticket") in (None, "", 0, "0")
        ):
            return jsonify({
                "status": "error",
                "message": "position_id or ticket is required",
            }), 422

        record = position_manager.on_position_closed(data)
        if record is None:
            return jsonify({
                "status": "ok",
                "message": "Duplicate position close ignored",
                "duplicate": True,
            })

        # Post-trade reflection (F5): background thread — the EA's HTTP
        # call must return immediately, and a journal failure is never fatal
        try:
            _spawn_reflection(record)
        except Exception as e:
            logger.debug(f"Reflection spawn failed: {e}")

        return jsonify({"status": "ok", "message": "Position close recorded"})

    except PositionConflictError as e:
        logger.warning(f"Rejected stale position close: {e}")
        return jsonify({"status": "conflict", "message": str(e)}), 409
    except ValueError as e:
        logger.warning(f"Invalid position close report: {e}")
        return jsonify({"status": "error", "message": str(e)}), 422
    except PositionPersistenceError as e:
        logger.error(f"Could not persist position close: {e}")
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        logger.error(f"Error recording position close: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


_reflection_chat_fn = None          # lazily-built aux LLM channel (F5)
_reflection_chat_ready = False


def _spawn_reflection(trade_record):
    """Fire-and-forget reflection generation for a closed trade."""
    from config import REFLECTIONS_ENABLED
    from reflections import trade_eligible
    if not REFLECTIONS_ENABLED or not trade_eligible(trade_record):
        return

    def worker():
        global _reflection_chat_fn, _reflection_chat_ready
        try:
            if not _reflection_chat_ready:
                from llm_util import make_chat_fn
                _reflection_chat_fn = make_chat_fn()
                _reflection_chat_ready = True
            if _reflection_chat_fn is None or decision_engine is None:
                return
            # Event currency from the decision row (the symbol's base
            # currency is wrong for e.g. CAD events on USDCAD)
            currency = None
            d_id = trade_record.get('decision_id')
            if d_id and decision_history is not None:
                for d in decision_history.get_recent(300):
                    if d.get('decision_id') == d_id:
                        currency = d.get('currency')
                        break
            from reflections import generate_and_store
            generate_and_store(_reflection_chat_fn,
                               decision_engine.reflection_store,
                               trade_record, currency=currency)
        except Exception as e:
            logger.warning(f"Reflection worker failed: {e}")

    threading.Thread(target=worker, name="reflection", daemon=True).start()


@app.route('/api/position/status', methods=['GET'])
def position_status():
    """Get current position manager status (for monitoring/debugging)."""
    ensure_services()

    try:
        status = position_manager.get_status()
        return jsonify({"status": "ok", **status})

    except Exception as e:
        logger.error(f"Error getting position status: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/test-signal', methods=['POST'])
def create_test_signal():
    """
    Create a test signal for testing purposes.

    Request body:
    {
        "pair": "NZDUSD",           # Required: currency pair
        "direction": "BUY",         # Required: BUY or SELL
        "seconds_until": 60,        # Optional: seconds until event (default 60)
        "lot_percent": 100,         # Optional: lot size percent (default 100)
        "confidence": 0.8,          # Optional: confidence score (default 0.8)
        "exit_minutes": 5,          # Optional: exit after X minutes (default 5)
        "event_name": "TEST EVENT"  # Optional: event name
    }
    """
    global next_decision
    ensure_services()

    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        pair = data.get('pair', '').upper()
        direction = data.get('direction', '').upper()

        if not pair:
            return jsonify({"status": "error", "message": "pair is required"}), 400
        if direction not in ['BUY', 'SELL']:
            return jsonify({"status": "error", "message": "direction must be BUY or SELL"}), 400

        seconds_until = data.get('seconds_until', 60)
        lot_percent = data.get('lot_percent', 100)
        confidence = data.get('confidence', 0.8)
        exit_minutes = data.get('exit_minutes', 5)
        event_name = data.get('event_name', 'TEST EVENT')
        stop_loss_pips = data.get('stop_loss_pips', 40)
        take_profit_pips = data.get('take_profit_pips', 60)

        raw_numbers = {
            "seconds_until": seconds_until,
            "lot_percent": lot_percent,
            "confidence": confidence,
            "exit_minutes": exit_minutes,
            "stop_loss_pips": stop_loss_pips,
            "take_profit_pips": take_profit_pips,
        }
        try:
            normalized = {}
            for name, value in raw_numbers.items():
                if isinstance(value, bool):
                    raise ValueError(name)
                normalized[name] = float(value)
                if not math.isfinite(normalized[name]):
                    raise ValueError(name)
        except (TypeError, ValueError) as exc:
            return jsonify({
                "status": "error",
                "message": f"{exc} must be a finite number",
            }), 400

        if (
            normalized["seconds_until"] < 0
            or not 0 < normalized["lot_percent"] <= 100
            or not 0 <= normalized["confidence"] <= 1
            or normalized["exit_minutes"] <= 0
            or normalized["stop_loss_pips"] < 0
            or normalized["take_profit_pips"] < 0
        ):
            return jsonify({
                "status": "error",
                "message": "Invalid numeric range in test signal",
            }), 400

        seconds_until = normalized["seconds_until"]
        lot_percent = normalized["lot_percent"]
        confidence = normalized["confidence"]
        exit_minutes = normalized["exit_minutes"]
        stop_loss_pips = normalized["stop_loss_pips"]
        take_profit_pips = normalized["take_profit_pips"]

        # Calculate event time (use UTC to match server's UTC-based comparisons)
        event_time = utcnow() + timedelta(seconds=seconds_until)

        # Create test decision
        from llm_decision_engine import TradingDecision

        test_decision = TradingDecision(
            event=event_name,
            currency=pair[:3],
            pair=pair,
            direction=direction,
            confidence=confidence,
            lot_percent=lot_percent,
            entry_seconds_before=15,
            exit_minutes_after=exit_minutes,
            stop_loss_percent=40,  # Legacy field
            stop_loss_pips=stop_loss_pips,
            take_profit_pips=take_profit_pips,
            reasoning=f"TEST SIGNAL: {direction} {pair} - triggered manually for testing",
            data_summary={
                'event': {
                    'datetime': event_time.isoformat(),
                    'currency': pair[:3],
                    'name': event_name,
                    'impact': 'HIGH'
                },
                'test_mode': True
            },
            timestamp=utcnow()
        )

        with decision_lock:
            next_decision = test_decision

        logger.info(f"TEST SIGNAL created: {direction} {pair} in {seconds_until}s")

        return jsonify({
            "status": "ok",
            "message": f"Test signal created: {direction} {pair}",
            "event_time": event_time.isoformat(),
            "seconds_until": seconds_until,
            "signal": {
                "direction": direction,
                "pair": pair,
                "lot_percent": lot_percent,
                "confidence": confidence,
                "exit_minutes": exit_minutes
            }
        })

    except Exception as e:
        logger.error(f"Error creating test signal: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/test-signal', methods=['DELETE'])
def clear_test_signal():
    """Clear current test signal"""
    global next_decision

    with decision_lock:
        next_decision = None

    logger.info("Test signal cleared")
    return jsonify({"status": "ok", "message": "Signal cleared"})


def cleanup_stale_registrations():
    """Remove stale pair registrations older than 1 hour"""
    global registered_pairs, executed_trades

    # Drop market data from EAs that stopped pushing (dead charts) — the age
    # gate in _build_market_context_for_event ignores them anyway, this just
    # bounds memory
    with market_data_lock:
        dead = [p for p, entry in market_data_reports.items()
                if _market_data_age_seconds(entry) > 86400]
        for p in dead:
            del market_data_reports[p]
            logger.info(f"Cleaned up stale market data: {p}")

    with pair_lock:
        stale_keys = []
        for key in list(registered_pairs.keys()):
            # Parse event time from key (format: CURRENCY_YYYYMMDD_HHMM)
            # Note: event_time in key is UTC
            parts = key.split('_')
            if len(parts) >= 3:
                try:
                    event_time = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M")
                    # Remove if event was more than 1 hour ago (compare UTC with UTC)
                    if (utcnow() - event_time).total_seconds() > 3600:
                        stale_keys.append(key)
                except:
                    pass

        for key in stale_keys:
            del registered_pairs[key]
            logger.info(f"Cleaned up stale registration: {key}")

    # Also clean up old executed_trades (older than 24 hours)
    # This prevents the set from growing indefinitely
    # Note: executed_trades uses event_key format (UTC times), so we compare with UTC
    stale_executed = set()
    for key in executed_trades:
        parts = key.split('_')
        if len(parts) >= 3:
            try:
                event_time = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M")
                if (utcnow() - event_time).total_seconds() > 86400:  # 24 hours
                    stale_executed.add(key)
            except:
                pass
    executed_trades -= stale_executed


def _analyzed_event_key(event) -> str:
    """Create unique key for tracking analyzed events."""
    dt_str = event.datetime_utc.strftime("%Y%m%d_%H%M") if hasattr(event, 'datetime_utc') else str(event)
    name = getattr(event, 'event_name', '')
    currency = getattr(event, 'currency', '')
    return f"{currency}_{name}_{dt_str}"


def _mark_event_analyzed(event, reason: str = "SKIP"):
    """Mark an event as already analyzed. Prevents re-analysis."""
    key = _analyzed_event_key(event)
    analyzed_events[key] = utcnow()
    logger.info(f"Event marked as analyzed ({reason}): {key}")


def _select_release_group(events, reference):
    """(dominant, shadowed) for the release `reference` belongs to.

    Statistical agencies publish a family at one timestamp (CAD CPI m/m with
    Median/Trimmed/Common CPI y/y; NFP with Average Hourly Earnings). There is
    ONE price path, so there is one decision: the dominant member is analyzed
    and the rest are marked analyzed immediately — before this, each sibling
    bought its own panel on the next 15 s scan, each starting later against
    the panel's max(T-20s, 10s) deadline, and the trade ended up labelled with
    the weakest member of the release (production, 17.08.2026).

    Caller holds decision_lock.
    """
    group = event_cluster.same_release(events, reference)
    dominant, shadowed = event_cluster.pick_dominant(group)
    return (dominant or reference), shadowed


def _is_event_analyzed(event) -> bool:
    """Check if event was already analyzed."""
    return _analyzed_event_key(event) in analyzed_events


def _mark_decision_event_analyzed(decision) -> None:
    """Mark a decision's event as analyzed when its trade opens/executes.
    The open handlers clear next_decision while the event can still be a few
    seconds ahead (entry is at T-15s) — without this marker the next updater
    tick saw an 'unanalyzed' in-window event and paid for a SECOND full LLM
    analysis (with SKYTOWER_ENSEMBLE_K=3 that doubled the entry cost:
    2026-07-22 GBP CPI ran 3 Fable calls at 07:57 and 3 more at 08:00 CEST).
    Must be called under decision_lock (reads the live decision object)."""
    try:
        key = _analyzed_event_key_from_decision(decision)
        analyzed_events[key] = utcnow()
        logger.info(f"Event marked as analyzed (trade executed): {key}")
    except Exception as e:
        logger.debug(f"Could not mark decision event analyzed: {e}")


def _should_retry_analysis(attempts: int, retry_room_seconds: int) -> bool:
    """Whether a failed event analysis is worth another scan.

    Retry while the preload window still has room for a full panel plus
    serving, and only up to MAX_ANALYSIS_ATTEMPTS so a permanent fault (bad
    config, unreachable provider) cannot spin every scan for 24h.
    """
    return (attempts < MAX_ANALYSIS_ATTEMPTS
            and retry_room_seconds >= MIN_ANALYSIS_RETRY_SECONDS)


def _cleanup_analysis_failures():
    """Drop failure counters that can no longer influence anything, so a
    long-running process cannot accumulate them (the 24/7 machine never
    restarts).

    Retiring on "the event is in analyzed_events" alone is not enough: an
    event whose retry was scheduled but whose window then closed is never
    analyzed, never marked, and simply falls out of the scan — its counter
    would live forever. Age them out as well, on the same 24h horizon the
    analyzed_events cleanup uses.
    """
    now = utcnow()
    for key in [k for k, (_, seen) in _analysis_failures.items()
                if k in analyzed_events or (now - seen).total_seconds() > 86400]:
        del _analysis_failures[key]


def _cleanup_analyzed_events():
    """Remove analyzed event records older than 24 hours."""
    now = utcnow()
    stale = [k for k, v in analyzed_events.items() if (now - v).total_seconds() > 86400]
    for k in stale:
        del analyzed_events[k]
    if stale:
        logger.debug(f"Cleaned up {len(stale)} stale analyzed event records")


# One-shot synthetic event for end-to-end dry-runs.
# Set SKYTOWER_FAKE_EVENT_IN_SECONDS=180 and restart: the full pipeline
# (preload -> LLM decision -> EA signal -> entry at T-15s -> exit) runs
# without waiting for a real calendar event. DEMO ONLY.
_fake_event = None
_fake_event_initialized = False


def _get_fake_test_event():
    global _fake_event, _fake_event_initialized
    if not _fake_event_initialized:
        _fake_event_initialized = True
        try:
            seconds = int(os.getenv("SKYTOWER_FAKE_EVENT_IN_SECONDS", "0") or 0)
        except ValueError:
            seconds = 0
        if seconds > 0:
            from calendar_fetcher import EconomicEvent
            _fake_event = EconomicEvent(
                datetime_utc=utcnow() + timedelta(seconds=seconds),
                currency="USD",
                event_name="CPI m/m (FAKE TEST EVENT)",
                impact="HIGH",
                forecast="0.3%",
                previous="0.2%",
                source="fake-test",
            )
            logger.warning(f"FAKE TEST EVENT injected — fires in {seconds}s "
                           f"at {_fake_event.datetime_utc.isoformat()} UTC (dry-run mode)")
    return _fake_event


def _get_next_unanalyzed_events() -> list:
    """
    Get upcoming tradeable events, excluding already-analyzed ones.
    Returns list of events sorted by time (nearest first).
    """
    # No explicit currencies: the default path routes through the shared
    # get_monitored_events() accessor — one cache entry for all callers.
    # event_keywords=None -> calendar reads cfg.HIGH_IMPACT_EVENTS at call
    # time; passing the module-level import here pinned the import-time list,
    # silently ignoring panel tier edits until restart.
    events = calendar.get_tradeable_events(event_keywords=None)

    fake = _get_fake_test_event()
    if fake is not None:
        # Keep the list sorted by time — the updater's scan loop breaks at the
        # first out-of-window event, so an unsorted head would shadow nearer
        # real events. (Fake datetimes are naive UTC, calendar ones tz-aware.)
        def _naive_time(evt):
            dt = evt.datetime_utc
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        events = sorted(list(events) + [fake], key=_naive_time)

    result = []
    for event in events:
        # Skip already-analyzed events
        if _is_event_analyzed(event):
            continue

        # Skip events that already passed by more than 2 minutes
        event_time = event.datetime_utc
        if event_time.tzinfo is not None:
            event_time = event_time.replace(tzinfo=None)
        if (event_time - utcnow()).total_seconds() < -120:
            continue

        result.append(event)

    return result


def _run_regime_scan():
    """Feed newly-backfilled rate decisions to the regime tracker. The scan
    may make ONE LLM call (~up to 30s, ambiguous hold) in the updater
    thread, so it is double-guarded: never while a decision is active, and
    never when a monitored event is imminent — a stall here could push
    event analysis past the entry window."""
    if regime_tracker is None or path_recorder is None:
        return
    try:
        with decision_lock:
            if next_decision is not None:
                return
        # Imminent-event gate (cache-hit: same calendar key as the updater)
        now = utcnow()
        for event in calendar.get_monitored_events():
            event_time = event.datetime_utc
            if event_time.tzinfo is not None:
                event_time = event_time.replace(tzinfo=None)
            until = (event_time - now).total_seconds()
            if 0 <= until <= 480:
                return
            if until > 480:
                break   # sorted by time — nothing imminent
        regime_tracker.scan(path_recorder.get_recent(150))
    except Exception as e:
        logger.debug(f"Regime scan error: {e}")


def _run_event_path_recorder():
    """Feed the event-path recorder each updater tick: schedule monitored
    events approaching release and measure passed ones from EA-pushed M1.
    Uses the SAME calendar call signature as get_tradeable_events, so it hits
    the per-key cache and adds ZERO feed fetches. Impact-only filter — events
    outside the name whitelist are recorded too (learning from everything,
    not just what we would trade)."""
    if path_recorder is None or calendar is None:
        return

    # While a decision is active an event is imminent — never risk a calendar
    # fetch (expired cache = seconds of HTTP) in the updater thread. Pending
    # measurements and the T0 spread snapshot still run; scheduling loses
    # nothing because events enter the pending list up to 2h in advance.
    with decision_lock:
        decision_active = next_decision is not None

    events = []
    if not decision_active:
        try:
            # Shared accessor = shared cache entry with the decision pipeline;
            # this call can never become an independent feed fetcher
            events = calendar.get_monitored_events()
        except Exception as e:
            logger.debug(f"Path recorder calendar read failed: {e}")
            events = []

    fake = _get_fake_test_event()
    if fake is not None:
        events = list(events) + [fake]

    # Shallow snapshot under the lock; the recorder only reads bar lists and
    # the market-data handler replaces entries wholesale, never mutates them
    with market_data_lock:
        snapshot = {pair: dict(entry) for pair, entry in market_data_reports.items()}

    path_recorder.tick(events, snapshot)


def background_decision_updater():
    """
    Intelligent background thread that:
    1. Pre-loads decisions before events (within PRELOAD window)
    2. Handles SKIP decisions immediately — moves to next event (BUG-6 FIX)
    3. Releases decision_lock during LLM calls for API responsiveness
    4. Records all decisions to audit log
    5. Cleans up stale registrations periodically
    """
    global next_decision

    PRELOAD_SECONDS = TRADING_CONFIG["preload_seconds"]        # decision window start (env: SKYTOWER_PRELOAD_SECONDS)
    CHECK_INTERVAL = TRADING_CONFIG["decision_check_interval"]  # scan loop interval (env: SKYTOWER_CHECK_INTERVAL)
    CLEANUP_INTERVAL = 300  # Cleanup every 5 minutes

    last_logged_event_key = None
    last_cleanup_time = time.time()

    while True:
        try:
            time.sleep(CHECK_INTERVAL)

            # Periodic cleanup
            if time.time() - last_cleanup_time > CLEANUP_INTERVAL:
                cleanup_stale_registrations()
                _cleanup_analyzed_events()
                _cleanup_analysis_failures()
                _backfill_reaction_actuals()
                _run_regime_scan()
                last_cleanup_time = time.time()

            # Event-path recorder: schedule upcoming monitored events and
            # measure passed ones from EA-pushed M1 (in-memory + one JSONL
            # append per event; never blocks the decision pipeline)
            try:
                _run_event_path_recorder()
            except Exception as e:
                logger.debug(f"Event path recorder error: {e}")

            # === PHASE 1: Check state & find event to analyze (under lock, fast) ===
            event_to_analyze = None

            with decision_lock:
                if next_decision:
                    # We have an active (non-SKIP) decision — check if event passed
                    try:
                        event_time = datetime.fromisoformat(
                            next_decision.data_summary['event']['datetime']
                        )
                        if event_time.tzinfo is not None:
                            event_time = event_time.replace(tzinfo=None)

                        time_until = (event_time - utcnow()).total_seconds()

                        # Event passed - reset decision
                        if time_until < -120:  # 2 minutes after event
                            logger.info(f"Event passed ({next_decision.event}). Resetting decision.")
                            next_decision = None
                            last_logged_event_key = None
                            continue

                        # Self-heal: a decision pinned for an event far outside
                        # the preload window (e.g. via manual /api/decision/refresh)
                        # would block the pipeline for hours/days — release it
                        if time_until > PRELOAD_SECONDS + 60:
                            logger.warning(
                                f"Decision for {next_decision.event} is {int(time_until)}s away "
                                f"(window is {PRELOAD_SECONDS}s) — releasing pipeline.")
                            next_decision = None
                            last_logged_event_key = None
                            continue

                        # Log active decision once
                        evt_key = _analyzed_event_key_from_decision(next_decision)
                        if last_logged_event_key != evt_key:
                            last_logged_event_key = evt_key
                            logger.info(f"Decision ready: {next_decision.event} @ {event_time}")
                            logger.info(f"  Direction: {next_decision.direction} | "
                                        f"Confidence: {next_decision.confidence:.0%} | "
                                        f"Time until: {int(time_until)}s")

                    except Exception as e:
                        logger.debug(f"Error checking event time: {e}")

                else:
                    # No active decision — scan for events to analyze
                    events = _get_next_unanalyzed_events()

                    for event in events:
                        event_time = event.datetime_utc
                        if event_time.tzinfo is not None:
                            event_time = event_time.replace(tzinfo=None)

                        time_until = (event_time - utcnow()).total_seconds()

                        if time_until < -30:
                            # Event already passed, skip
                            continue
                        elif 0 < time_until <= PRELOAD_SECONDS:
                            # In pre-load window — analyze this RELEASE (the
                            # whole same-minute cluster, one decision)
                            event_to_analyze, shadowed = _select_release_group(
                                events, event)
                            for sibling in shadowed:
                                _mark_event_analyzed(
                                    sibling,
                                    f"co-released with {event_to_analyze.event_name}")
                            if shadowed:
                                logger.info(
                                    f"Release cluster: {len(shadowed) + 1} events at "
                                    f"{event_time:%H:%M} UTC ({event_to_analyze.currency}) "
                                    f"-> analyzing {event_to_analyze.event_name} "
                                    f"({event_to_analyze.impact}); shadowed: "
                                    f"{[s.event_name for s in shadowed]}")
                            break
                        elif time_until > PRELOAD_SECONDS:
                            # Next event is too far away, stop scanning
                            break

            # === PHASE 2: LLM call OUTSIDE lock (can take 20-60s) ===
            if event_to_analyze:
                logger.info(f"")
                logger.info(f"{'='*60}")
                logger.info(f"ANALYZING EVENT - {event_to_analyze.event_name} ({event_to_analyze.currency})")

                evt_time = event_to_analyze.datetime_utc
                if evt_time.tzinfo is not None:
                    evt_time = evt_time.replace(tzinfo=None)
                secs_until = int((evt_time - utcnow()).total_seconds())
                logger.info(f"Time until event: {secs_until}s ({secs_until // 60}min)")
                logger.info(f"{'='*60}")

                start_time = time.time()
                try:
                    market_ctx = _build_market_context_for_event(event_to_analyze)
                    if market_ctx:
                        logger.info(f"Market context available for {market_ctx.get('pair')} "
                                    f"(trend: {market_ctx.get('trend')}, "
                                    f"age: {market_ctx.get('data_age_minutes', '?')} min)")
                    else:
                        logger.info("No market context available (EA has not pushed price data)")

                    # Everything else printing in this same minute — the tape
                    # prices the COMBINED surprise, so the model must see the
                    # siblings even when the name whitelist ignores them.
                    # peek_cached_events(): the cache is warm (PHASE 1 just
                    # read it) and it can never fetch — a network stall here
                    # would eat the preload window the panel needs.
                    co_released = []
                    try:
                        co_released = event_cluster.co_release_brief(
                            event_to_analyze, calendar.peek_cached_events())
                    except Exception as e:
                        logger.debug(f"Co-release lookup failed: {e}")
                    if co_released:
                        logger.info(f"Co-released this minute: "
                                    f"{[c['name'] for c in co_released]}")

                    new_decision = decision_engine.analyze_event(
                        event_to_analyze, market_ctx, co_released=co_released)
                    elapsed = time.time() - start_time

                    # Record ALL decisions to audit log. Recording is an AUDIT
                    # side effect and must NOT be able to discard a decision
                    # the panel already paid for: a JSONL/disk failure here
                    # used to land in the handler below, which marks the event
                    # analyzed for 24h — a completed, actionable BUY/SELL was
                    # silently thrown away while the dashboard stayed green.
                    if decision_history:
                        try:
                            decision_history.record(new_decision)
                        except Exception as rec_err:
                            logger.error(
                                f"Could not record decision to history: {rec_err}"
                            )

                    # === PHASE 3: Apply decision (under lock) ===
                    with decision_lock:
                        if new_decision.direction != "SKIP":
                            # Actionable decision — store it
                            next_decision = new_decision
                            logger.info(f"DECISION: {new_decision.direction} in {elapsed:.1f}s")
                            logger.info(f"  Pair: {new_decision.pair} | "
                                        f"Confidence: {new_decision.confidence:.0%}")
                            logger.info(f"  Reasoning: {new_decision.reasoning[:150]}...")
                        else:
                            # SKIP — mark as analyzed, do NOT block pipeline
                            _mark_event_analyzed(event_to_analyze)
                            logger.info(f"SKIP in {elapsed:.1f}s for {new_decision.event}")
                            logger.info(f"  Confidence: {new_decision.confidence:.0%}")
                            logger.info(f"  Reasoning: {new_decision.reasoning[:150]}...")
                            # next_decision stays None → next iteration scans for more events

                except Exception as e:
                    logger.error(f"Error analyzing event {event_to_analyze.event_name}: {e}")
                    # A TRANSIENT failure must not burn the event for the day.
                    # Marking it analyzed on the first exception meant one
                    # network blip or full disk ended trading for that release
                    # even with 100+ seconds of preload window left. Retry on
                    # the next scan; give up after MAX_ANALYSIS_ATTEMPTS (or
                    # when no room is left) so a permanent error cannot loop.
                    key = _analyzed_event_key(event_to_analyze)
                    attempts = _analysis_failures.get(key, (0, utcnow()))[0] + 1
                    _analysis_failures[key] = (attempts, utcnow())
                    # Room must be measured NOW, not from the secs_until read
                    # before the attempt: a failed analysis can burn 20-60s (or
                    # the panel's whole deadline), so the pre-attempt value
                    # overstates the window by the length of the failure and
                    # would authorise a retry that lands after the entry
                    # moment — paying for a panel whose answer /api/signal
                    # refuses to serve.
                    retry_room = int(
                        (evt_time - utcnow()).total_seconds()
                    ) - TRADING_CONFIG['entry_seconds_before']
                    if not _should_retry_analysis(attempts, retry_room):
                        logger.error(
                            f"Giving up on {event_to_analyze.event_name} after "
                            f"{attempts} attempt(s), {retry_room}s of entry "
                            f"window left"
                        )
                        _mark_event_analyzed(event_to_analyze)
                    else:
                        logger.warning(
                            f"Will retry {event_to_analyze.event_name} on the "
                            f"next scan (attempt {attempts}/"
                            f"{MAX_ANALYSIS_ATTEMPTS}, {retry_room}s left)"
                        )

        except Exception as e:
            logger.error(f"Background updater error: {e}")
            time.sleep(60)


def _analyzed_event_key_from_decision(decision) -> str:
    """Create event key from a TradingDecision object."""
    try:
        evt = decision.data_summary.get('event', {})
        dt_str = evt.get('datetime', '')[:16].replace('-', '').replace(':', '').replace('T', '_')
        return f"{evt.get('currency', '')}_{decision.event}_{dt_str}"
    except Exception:
        return str(id(decision))


if __name__ == '__main__':
    import sys

    # Setup logging with absolute path (BUG-8 FIX)
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(_log_dir, exist_ok=True)

    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")
    logger.add(os.path.join(_log_dir, "server.log"), rotation="1 day", retention="30 days")

    print("=" * 60)
    print("SkyTower-AI Server")
    print("=" * 60)

    from config import FORCE_DECISION
    if FORCE_DECISION:
        logger.warning("=" * 60)
        logger.warning("FORCE_DECISION TEST MODE IS ACTIVE — SKIP is disabled!")
        logger.warning("Every analyzed event WILL produce a BUY/SELL signal.")
        logger.warning("Use this ONLY on a DEMO account.")
        logger.warning("=" * 60)

    # EFFECTIVE risk limits, not the documented defaults. Panel values in
    # logs/runtime_overrides.json outrank .env permanently and used to be
    # applied without a single line of output — an operator could believe the
    # per-trade budget was $100 while the EA was sizing lots against a value
    # written weeks earlier. max_loss_usd is the number that both sizes the
    # position and arms every max-loss guardrail, so it is logged at startup.
    _risk = POSITION_MANAGEMENT_CONFIG
    logger.info(
        f"EFFECTIVE RISK LIMITS: max_loss_usd=${_risk.get('max_loss_usd'):,.0f}/trade | "
        f"max_daily_loss_usd=${_risk.get('max_daily_loss_usd'):,.0f}/day | "
        f"max_daily_trades={_risk.get('max_daily_trades')} | "
        f"max_hold_minutes={_risk.get('max_hold_minutes')} | "
        f"profit_protection={_risk.get('profit_protection_percent'):g}% drop, "
        f"arms at {_risk.get('profit_protection_floor_pct'):g}% of budget, "
        f"grace {_risk.get('profit_protection_grace_seconds')}s "
        f"(defaults < .env < dashboard panel)"
    )
    import config as _cfg
    for _note in getattr(_cfg, "RISK_LIMIT_NOTES", []):
        logger.warning(f"RISK LIMIT CORRECTED: {_note}")
    # Same idea for the instrument routing table: config drops invalid
    # entries at import (env / overrides file) and only print()s about it —
    # invisible on the 24/7 machine. Surface the notes and the EFFECTIVE table.
    for _note in getattr(_cfg, "CONFIG_NOTES", []):
        logger.warning(f"CONFIG NOTE: {_note}")
    logger.info(f"EFFECTIVE INSTRUMENT ROUTING: {_cfg.INSTRUMENT_ROUTING or 'OFF'} "
                f"(defaults < .env < dashboard panel)")

    # Initialize services
    init_services()

    # Start background updater
    updater_thread = threading.Thread(target=background_decision_updater, daemon=True)
    updater_thread.start()

    # Run server
    print(f"\nServer starting on http://{SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")
    print("Endpoints:")
    print("  GET  /health                - Health check")
    print("  GET  /api/signal            - Get MT5 trade signal")
    print("  GET  /api/decision          - Get full trading decision")
    print("  POST /api/decision/refresh  - Force refresh decision")
    print("  GET  /api/events            - Get upcoming events")
    print("  GET  /api/cot/<currency>    - Get COT data")
    print("  GET  /api/sentiment/<pair>  - Get sentiment")
    print("  POST /api/zones             - Analyze market structure zones")
    print("  POST /api/targets           - Calculate trade targets")
    print("  GET  /api/config            - Get configuration")
    print("  --- AI Decision Audit ---")
    print("  GET  /api/decisions/history  - Decision audit log")
    print("  GET  /api/datasources/status - Data source health")
    print("  --- AI Position Management ---")
    print("  POST /api/position/opened   - EA reports position opened")
    print("  POST /api/position/report   - EA reports status + gets AI command")
    print("  POST /api/position/closed   - EA reports position closed")
    print("  GET  /api/position/status   - Position manager status")
    print("=" * 60)

    app.run(
        host=SERVER_CONFIG['host'],
        port=SERVER_CONFIG['port'],
        debug=SERVER_CONFIG['debug'],
        threaded=True
    )
