"""
SkyTower-AI Flask Server
Provides REST API for MT5 Expert Advisor communication
"""
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from datetime import datetime, timedelta
from timeutil import utcnow
import json
import os
import threading
import time
from loguru import logger

from config import SERVER_CONFIG, TRADING_CONFIG, ZONE_CONFIG, EXIT_CONFIG, POSITION_MANAGEMENT_CONFIG
from config import HIGH_IMPACT_EVENTS, CURRENCY_PAIRS, DEFAULT_PAIRS
from market_context import build_market_context, normalize_pair
from calendar_fetcher import CalendarAggregator
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from llm_decision_engine import LLMDecisionEngine, TradingDecision
from zone_analyzer import ZoneAnalyzer, PriceBar, analyze_from_ohlc_data
from target_calculator import TargetCalculator, calculate_trade_targets
from position_manager import PositionManager
from exit_decision_engine import ExitDecisionEngine
from decision_history import DecisionHistory

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
next_decision = None
decision_lock = threading.Lock()

# Event analysis tracking (BUG-6 FIX)
# Tracks which events have been analyzed (SKIP) to avoid re-analyzing
analyzed_events = {}  # {"event_key": datetime_analyzed}

# Multi-instance coordination
# Structure: { "event_key": { "pair": {...data...}, "pair2": {...} }, ... }
registered_pairs = {}
pair_lock = threading.Lock()
# Tracks which pair was selected for each event
selected_pairs = {}  # { "event_key": "GBPJPY" }
executed_trades = set()  # Tracks executed event_keys to prevent duplicates

# Per-pair market data pushed by the EA (works in single-instance mode,
# unlike registered_pairs which needs multi-instance + a known event).
# Structure: { "EURUSD": {"ohlc_multi": {"M5": [...], ...}, "current_price": ..., "updated_at": ...} }
market_data_reports = {}
market_data_lock = threading.Lock()


def init_services():
    """Initialize all services"""
    global decision_engine, calendar, zone_analyzer, target_calculator, position_manager, exit_engine, decision_history
    logger.info("Initializing SkyTower-AI services...")

    decision_engine = LLMDecisionEngine()
    calendar = CalendarAggregator()
    zone_analyzer = ZoneAnalyzer(ZONE_CONFIG)
    target_calculator = TargetCalculator(ZONE_CONFIG)
    exit_engine = ExitDecisionEngine()
    position_manager = PositionManager(exit_engine=exit_engine)
    decision_history = DecisionHistory()

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
            "tier1_events": cfg.TIER1_EVENTS,
            "tier2_events": cfg.TIER2_EVENTS,
            "all_events": cfg.HIGH_IMPACT_EVENTS,
            "currencies": list(cfg.CURRENCY_PAIRS.keys()),
            "min_impact": getattr(cfg, "MIN_IMPACT_LEVEL", "MEDIUM"),
        })

    # POST: update event config at runtime
    data = request.json or {}
    if 'tier1_events' in data:
        cfg.TIER1_EVENTS = data['tier1_events']
    if 'tier2_events' in data:
        cfg.TIER2_EVENTS = data['tier2_events']
    if 'tier1_events' in data or 'tier2_events' in data:
        cfg.HIGH_IMPACT_EVENTS = cfg.TIER1_EVENTS + cfg.TIER2_EVENTS
    if 'min_impact' in data:
        level = str(data['min_impact']).strip().upper()
        if level in ("LOW", "MEDIUM", "HIGH"):
            cfg.MIN_IMPACT_LEVEL = level
            logger.info(f"Min impact level set to {level} via dashboard "
                        f"(add SKYTOWER_MIN_IMPACT={level} to .env to survive restarts)")
    return jsonify({"status": "ok", "message": "Event config updated"})


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


@app.route('/api/datasources/status', methods=['GET'])
def get_datasource_status():
    """Check which data sources are currently responding."""
    ensure_services()
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

    return jsonify({"status": "ok", "data_sources": status})


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

        return jsonify({
            "status": "ok",
            "count": len(events),
            "events": [e.to_dict() for e in events]
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
        upcoming = calendar.get_tradeable_events(
            event_keywords=HIGH_IMPACT_EVENTS,
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

        with decision_lock:
            next_decision = decision_engine.get_next_trade_recommendation()

            # Record to decision history
            if next_decision and decision_history:
                decision_history.record(next_decision)

            if next_decision:
                return jsonify({
                    "status": "ok",
                    "message": "Decision refreshed",
                    "decision": next_decision.to_dict()
                })
            else:
                return jsonify({
                    "status": "ok",
                    "message": "No trade recommendation available"
                })
    except Exception as e:
        logger.error(f"Error refreshing decision: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/analyze', methods=['POST'])
def analyze_custom_event():
    """
    Analyze a specific event
    Request body should contain event details
    """
    ensure_services()
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        # Create event from request data
        from calendar_fetcher import EconomicEvent
        event = EconomicEvent(
            datetime_utc=datetime.fromisoformat(data.get('datetime')),
            currency=data.get('currency', ''),
            event_name=data.get('event_name', ''),
            impact=data.get('impact', 'HIGH'),
            forecast=data.get('forecast'),
            previous=data.get('previous'),
        )

        decision = decision_engine.analyze_event(event)

        return jsonify({
            "status": "ok",
            "decision": decision.to_dict()
        })
    except Exception as e:
        logger.error(f"Error analyzing event: {e}")
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

        # Use default pip-based targets from config
        pip_size = 0.0001 if "JPY" not in symbol else 0.01

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

        with market_data_lock:
            market_data_reports[pair] = {
                "pair": pair,
                "ohlc_multi": ohlc_multi,
                "spread_points": data.get('spread_points', 0),
                "updated_at": utcnow().isoformat()
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


def _market_data_age_seconds(entry) -> float:
    try:
        ts = datetime.fromisoformat(entry.get('updated_at', ''))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return (utcnow() - ts).total_seconds()
    except (ValueError, TypeError):
        return float('inf')


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

        with market_data_lock:
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

        return build_market_context(
            ohlc_multi, pair_name, zones=zones, registered_at=data_timestamp,
            spread_points=spread_points
        )
    except Exception as e:
        logger.warning(f"Could not build market context for {event.event_name}: {e}")
        return None


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


# Backfill guards: the fetch runs in the single updater thread, so it must
# never stall the decision pipeline or hammer ForexFactory forever
_last_backfill_fetch = 0.0
BACKFILL_MIN_INTERVAL_SECONDS = 900   # at most one FF fetch per 15 min
BACKFILL_MAX_AGE_DAYS = 7             # FF weekly feed can't resolve older records


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
                   and (r.get('event_time') or '') >= cutoff]
        if not pending:
            return

        _last_backfill_fetch = time.time()

        # Only ForexFactory carries released 'actual' values in its weekly feed
        events = []
        for source in getattr(calendar, 'sources', []):
            if 'ForexFactory' in source.__class__.__name__:
                try:
                    events = source.fetch_events(days_ahead=7)
                except Exception as e:
                    logger.debug(f"Backfill calendar fetch failed: {e}")
                break
        if events:
            history.backfill_actuals(events)
    except Exception as e:
        logger.debug(f"Reaction backfill error: {e}")


@app.route('/api/registered-pairs', methods=['GET'])
def get_registered_pairs():
    """Get all registered pairs for current/upcoming events"""
    with pair_lock:
        return jsonify({
            "status": "ok",
            "events": {
                key: {
                    "pairs": list(pairs.keys()),
                    "count": len(pairs)
                }
                for key, pairs in registered_pairs.items()
            }
        })


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


@app.route('/api/zone-reports', methods=['GET'])
def get_zone_reports():
    """Get all reported zone data from EA instances"""
    with zone_reports_lock:
        return jsonify({
            "status": "ok",
            "pairs": dict(zone_reports),
            "count": len(zone_reports)
        })


@app.route('/api/select-best-pair', methods=['POST'])
def select_best_pair():
    """
    Trigger LLM analysis to select the best pair for an event.
    Called when all pairs are registered or before event time.

    Request body:
    {
        "event_key": "GBP_20260123_1200",
        "event_name": "Retail Sales m/m",
        "forecast": "0.0%",
        "previous": "-0.1%"
    }
    """
    global selected_pairs, next_decision
    ensure_services()

    try:
        data = request.json
        event_key = data.get('event_key', '')

        if not event_key:
            return jsonify({"status": "error", "message": "event_key required"}), 400

        with pair_lock:
            if event_key not in registered_pairs:
                return jsonify({
                    "status": "error",
                    "message": f"No pairs registered for {event_key}"
                }), 400

            pairs_data = registered_pairs[event_key].copy()

        # Merge zone_reports data into pairs_data
        # This allows EAs to report zone data separately via /api/report-zone
        with zone_reports_lock:
            for pair, pair_info in pairs_data.items():
                if pair in zone_reports:
                    zone_data = zone_reports[pair]
                    # Update zones dict with reported zone data
                    if not pair_info.get('zones') or not pair_info['zones'].get('direction_bias'):
                        pair_info['zones'] = {
                            'direction_bias': zone_data.get('direction_bias', 'neutral'),
                            'bias_strength': zone_data.get('bias_strength', 0),
                            'nearest_resistance': zone_data.get('nearest_resistance', 0),
                            'nearest_support': zone_data.get('nearest_support', 0),
                        }
                    # Also update spread_points if reported
                    if zone_data.get('spread_points', 0) > 0:
                        pair_info['spread_points'] = zone_data['spread_points']
                    logger.info(f"Merged zone data for {pair}: bias={zone_data.get('direction_bias')}, spread={zone_data.get('spread_points')}")

        if len(pairs_data) == 0:
            return jsonify({
                "status": "error",
                "message": "No pairs available for analysis"
            }), 400

        logger.info(f"Selecting best pair from {len(pairs_data)} candidates: {list(pairs_data.keys())}")

        # Parse event time from event_key (format: CURRENCY_YYYYMMDD_HHMM)
        event_time_str = data.get('event_time')
        if not event_time_str:
            # Try to parse from event_key (event_key contains UTC time)
            parts = event_key.split('_')
            if len(parts) >= 3:
                try:
                    event_time_str = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M").isoformat()
                except:
                    event_time_str = utcnow().isoformat()
            else:
                event_time_str = utcnow().isoformat()

        # Build multi-pair prompt for LLM
        event_info = {
            "event_name": data.get('event_name', 'Unknown Event'),
            "currency": event_key.split('_')[0],
            "forecast": data.get('forecast', ''),
            "previous": data.get('previous', ''),
            "event_time": event_time_str
        }

        # Use decision engine with multi-pair context
        decision = decision_engine.get_best_pair_recommendation(
            event_info=event_info,
            pairs_data=pairs_data
        )

        if decision:
            # Use both locks together to ensure atomic state update
            with decision_lock:
                with pair_lock:
                    selected_pairs[event_key] = decision.pair
                next_decision = decision

            logger.info(f"Best pair selected: {decision.pair} with {decision.confidence:.0%} confidence")

            return jsonify({
                "status": "ok",
                "selected_pair": decision.pair,
                "direction": decision.direction,
                "confidence": decision.confidence,
                "reasoning": decision.reasoning,
                "all_pairs_analyzed": list(pairs_data.keys())
            })
        else:
            return jsonify({
                "status": "ok",
                "selected_pair": None,
                "message": "LLM recommends SKIP for all pairs"
            })

    except Exception as e:
        logger.error(f"Error selecting best pair: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# Tracks which decision was already logged as served (log once, not per poll)
_signal_served_log_key = None


@app.route('/api/signal', methods=['GET'])
def get_mt5_signal():
    """
    Simplified endpoint for MT5
    Returns minimal data needed for trade execution

    Query params:
        pair: Optional - if provided, only returns signal if this pair was selected
              This enables multi-instance mode where only the best pair gets the trade
    """
    global next_decision, selected_pairs, executed_trades
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

                sl_pips = getattr(next_decision, 'stop_loss_pips', 0)
                tp_pips = getattr(next_decision, 'take_profit_pips', 0)

                # Trade-lifecycle log: one line when the EA first picks up the signal
                global _signal_served_log_key
                serve_key = f"{next_decision.event}_{event_time.isoformat()}_{requesting_pair}"
                if _signal_served_log_key != serve_key:
                    _signal_served_log_key = serve_key
                    logger.info(f"=== SIGNAL SERVED === {next_decision.direction} {decision_pair} "
                                f"to EA[{requesting_pair or 'any'}] | conf {next_decision.confidence:.0%} "
                                f"| T-{int(time_until)}s | {next_decision.event}")

                return jsonify({
                    "signal": True,
                    "direction": next_decision.direction,
                    "pair": decision_pair,  # MT5 format (no slash)
                    "lot_percent": next_decision.lot_percent,
                    "confidence": next_decision.confidence,
                    "entry_seconds_before": next_decision.entry_seconds_before,
                    "exit_minutes": next_decision.exit_minutes_after,
                    "stop_loss_percent": next_decision.stop_loss_percent,
                    "stop_loss_pips": sl_pips,
                    "take_profit_pips": tp_pips,
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
    """
    global next_decision, executed_trades, registered_pairs

    try:
        # Get pair from query params or JSON body
        pair = request.args.get('pair', '')
        if not pair and request.json:
            pair = request.json.get('pair', '')

        logger.info(f"Trade executed notification received for {pair or 'unknown pair'}")

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

            next_decision = None

        # Second lock: cleanup registered pairs (separate to avoid deadlock)
        if event_key_to_cleanup:
            with pair_lock:
                if event_key_to_cleanup in registered_pairs:
                    del registered_pairs[event_key_to_cleanup]
                if event_key_to_cleanup in selected_pairs:
                    del selected_pairs[event_key_to_cleanup]

        return jsonify({"status": "ok", "message": "Trade recorded"})
    except Exception as e:
        logger.error(f"Error recording trade: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =============================================================================
# AI POSITION MANAGEMENT ENDPOINTS
# =============================================================================

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

        # Get entry reasoning from current decision
        entry_reasoning = ""
        with decision_lock:
            if next_decision:
                entry_reasoning = next_decision.reasoning

        position_manager.on_position_opened(data, entry_reasoning=entry_reasoning)

        # Also mark trade as executed (same as /api/trade-executed)
        event_key_to_cleanup = None

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

            next_decision = None

        if event_key_to_cleanup:
            with pair_lock:
                if event_key_to_cleanup in registered_pairs:
                    del registered_pairs[event_key_to_cleanup]
                if event_key_to_cleanup in selected_pairs:
                    del selected_pairs[event_key_to_cleanup]

        logger.info(f"Position opened: {data.get('direction')} {data.get('symbol')} "
                     f"@ {data.get('entry_price')} | ticket={data.get('ticket')}")

        return jsonify({"status": "ok", "message": "Position registered"})

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

        position_manager.on_position_closed(data)

        return jsonify({"status": "ok", "message": "Position close recorded"})

    except Exception as e:
        logger.error(f"Error recording position close: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


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
    global registered_pairs, selected_pairs, executed_trades

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
            if key in selected_pairs:
                del selected_pairs[key]
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


def _mark_event_analyzed(event):
    """Mark an event as already analyzed (SKIP). Prevents re-analysis."""
    key = _analyzed_event_key(event)
    analyzed_events[key] = utcnow()
    logger.info(f"Event marked as analyzed (SKIP): {key}")


def _is_event_analyzed(event) -> bool:
    """Check if event was already analyzed."""
    return _analyzed_event_key(event) in analyzed_events


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
    events = calendar.get_tradeable_events(
        event_keywords=HIGH_IMPACT_EVENTS,
        currencies=list(CURRENCY_PAIRS.keys())
    )

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
                _backfill_reaction_actuals()
                last_cleanup_time = time.time()

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
                            # In pre-load window — analyze this event
                            event_to_analyze = event
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

                    new_decision = decision_engine.analyze_event(event_to_analyze, market_ctx)
                    elapsed = time.time() - start_time

                    # Record ALL decisions to audit log
                    if decision_history:
                        decision_history.record(new_decision)

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
                    # Mark as analyzed to avoid infinite retry loop
                    _mark_event_analyzed(event_to_analyze)

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
