"""
SkyTower-AI Flask Server
Provides REST API for MT5 Expert Advisor communication
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import threading
import time
from loguru import logger

from config import SERVER_CONFIG, TRADING_CONFIG, ZONE_CONFIG, EXIT_CONFIG, POSITION_MANAGEMENT_CONFIG
from calendar_fetcher import CalendarAggregator
from cot_analyzer import COTAnalyzer
from sentiment_analyzer import SentimentAggregator
from llm_decision_engine import LLMDecisionEngine, TradingDecision
from zone_analyzer import ZoneAnalyzer, PriceBar, analyze_from_ohlc_data
from target_calculator import TargetCalculator, calculate_trade_targets
from position_manager import PositionManager
from exit_decision_engine import ExitDecisionEngine

app = Flask(__name__)
CORS(app)

# Global instances
decision_engine = None
calendar = None
zone_analyzer = None
target_calculator = None
position_manager = None
exit_engine = None
next_decision = None
decision_lock = threading.Lock()

# Multi-instance coordination
# Structure: { "event_key": { "pair": {...data...}, "pair2": {...} }, ... }
registered_pairs = {}
pair_lock = threading.Lock()
# Tracks which pair was selected for each event
selected_pairs = {}  # { "event_key": "GBPJPY" }
executed_trades = set()  # Tracks executed event_keys to prevent duplicates


def init_services():
    """Initialize all services"""
    global decision_engine, calendar, zone_analyzer, target_calculator, position_manager, exit_engine
    logger.info("Initializing SkyTower-AI services...")

    decision_engine = LLMDecisionEngine()
    calendar = CalendarAggregator()
    zone_analyzer = ZoneAnalyzer(ZONE_CONFIG)
    target_calculator = TargetCalculator(ZONE_CONFIG)
    exit_engine = ExitDecisionEngine()
    position_manager = PositionManager(exit_engine=exit_engine)

    logger.info("Services initialized successfully (with AI Position Manager)")


def ensure_services():
    """Ensure services are initialized (lazy initialization)"""
    global decision_engine, calendar, zone_analyzer, target_calculator, position_manager, exit_engine
    if decision_engine is None or calendar is None:
        init_services()


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": "4.0.0"
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

        events = calendar.get_upcoming_events(
            currencies=currencies,
            impact_filter="HIGH",
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
        with decision_lock:
            if next_decision is None:
                # Generate new decision
                next_decision = decision_engine.get_next_trade_recommendation()

            if next_decision:
                return jsonify({
                    "status": "ok",
                    "decision": next_decision.to_dict()
                })
            else:
                return jsonify({
                    "status": "ok",
                    "decision": None,
                    "message": "No trade recommendation available"
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
        with decision_lock:
            next_decision = decision_engine.get_next_trade_recommendation()

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
                "registered_at": datetime.now().isoformat()
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
                "updated_at": datetime.utcnow().isoformat()
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
                    event_time_str = datetime.utcnow().isoformat()
            else:
                event_time_str = datetime.utcnow().isoformat()

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
            if next_decision is None:
                next_decision = decision_engine.get_next_trade_recommendation()

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
                time_until = (event_time - datetime.utcnow()).total_seconds()

                # Get the decision's pair (normalize format)
                decision_pair = next_decision.pair.replace('/', '').upper()

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
                    if requesting_pair != decision_pair:
                        return jsonify({
                            "signal": False,
                            "message": f"Not selected - best pair is {decision_pair}",
                            "selected_pair": decision_pair,
                            "your_pair": requesting_pair
                        })

                sl_pips = getattr(next_decision, 'stop_loss_pips', 0)
                tp_pips = getattr(next_decision, 'take_profit_pips', 0)
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
        event_time = datetime.utcnow() + timedelta(seconds=seconds_until)

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
            timestamp=datetime.now()
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
                    if (datetime.utcnow() - event_time).total_seconds() > 3600:
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
                if (datetime.utcnow() - event_time).total_seconds() > 86400:  # 24 hours
                    stale_executed.add(key)
            except:
                pass
    executed_trades -= stale_executed


def background_decision_updater():
    """
    Intelligent background thread that:
    1. Pre-loads decisions 5 minutes before events
    2. Resets decisions after events pass
    3. Ensures LLM has time to process before entry
    4. Cleans up stale registrations periodically
    """
    global next_decision

    PRELOAD_SECONDS = 120  # 2 minutes before event - decision must be ready
    CHECK_INTERVAL = 15    # Check every 15 seconds for better responsiveness
    CLEANUP_INTERVAL = 300  # Cleanup every 5 minutes

    last_event_time = None
    decision_ready_for_event = False
    last_cleanup_time = time.time()

    while True:
        try:
            time.sleep(CHECK_INTERVAL)

            # Periodic cleanup of stale registrations
            if time.time() - last_cleanup_time > CLEANUP_INTERVAL:
                cleanup_stale_registrations()
                last_cleanup_time = time.time()

            with decision_lock:
                # Check current decision state
                if next_decision:
                    try:
                        # Event time is in UTC
                        event_time = datetime.fromisoformat(
                            next_decision.data_summary['event']['datetime']
                        )
                        if event_time.tzinfo is not None:
                            event_time = event_time.replace(tzinfo=None)

                        # Use utcnow() to compare UTC with UTC
                        time_until = (event_time - datetime.utcnow()).total_seconds()

                        # Event passed - reset decision
                        if time_until < -120:  # 2 minutes after event
                            logger.info(f"Event passed ({next_decision.event}). Resetting decision.")
                            next_decision = None
                            decision_ready_for_event = False
                            last_event_time = None
                            continue

                        # Track that we have a decision for this event
                        if last_event_time != event_time:
                            last_event_time = event_time
                            decision_ready_for_event = True
                            logger.info(f"Decision ready for {next_decision.event} @ {event_time}")
                            logger.info(f"  Direction: {next_decision.direction} | Confidence: {next_decision.confidence:.0%}")
                            logger.info(f"  Time until event: {int(time_until)}s ({int(time_until/60)}min)")

                    except Exception as e:
                        logger.debug(f"Error checking event time: {e}")

                else:
                    # No current decision - check if we need to pre-load one
                    decision_ready_for_event = False

                    # Check for upcoming events
                    event = calendar.get_next_tradeable_event()

                    if event:
                        # Event time is in UTC
                        event_time = event.datetime_utc
                        if event_time.tzinfo is not None:
                            event_time = event_time.replace(tzinfo=None)

                        # Use utcnow() to compare UTC with UTC
                        time_until = (event_time - datetime.utcnow()).total_seconds()

                        # Pre-load decision 5 minutes before
                        if 0 < time_until <= PRELOAD_SECONDS:
                            logger.info(f"")
                            logger.info(f"{'='*60}")
                            logger.info(f"PRE-LOADING DECISION - Event in {int(time_until)}s")
                            logger.info(f"Event: {event.event_name} ({event.currency})")
                            logger.info(f"{'='*60}")

                            # Generate decision (this may take 20-60 seconds for LLM)
                            start_time = time.time()
                            new_decision = decision_engine.get_next_trade_recommendation()
                            elapsed = time.time() - start_time

                            if new_decision:
                                next_decision = new_decision
                                logger.info(f"Decision generated in {elapsed:.1f}s")
                                logger.info(f"  Direction: {new_decision.direction}")
                                logger.info(f"  Confidence: {new_decision.confidence:.0%}")
                                logger.info(f"  Reasoning: {new_decision.reasoning[:100]}...")
                            else:
                                logger.info(f"No trade signal generated (SKIP or no data)")

        except Exception as e:
            logger.error(f"Background updater error: {e}")
            time.sleep(60)


if __name__ == '__main__':
    import sys

    # Setup logging
    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")
    logger.add("logs/server.log", rotation="1 day", retention="30 days")

    print("=" * 60)
    print("SkyTower-AI Server")
    print("=" * 60)

    # Initialize services
    init_services()

    # Start background updater
    updater_thread = threading.Thread(target=background_decision_updater, daemon=True)
    updater_thread.start()

    # Run server
    print(f"\nServer starting on http://{SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")
    print("Endpoints:")
    print("  GET  /health              - Health check")
    print("  GET  /api/signal          - Get MT5 trade signal")
    print("  GET  /api/decision        - Get full trading decision")
    print("  POST /api/decision/refresh - Force refresh decision")
    print("  GET  /api/events          - Get upcoming events")
    print("  GET  /api/cot/<currency>  - Get COT data")
    print("  GET  /api/sentiment/<pair> - Get sentiment")
    print("  POST /api/zones           - Analyze market structure zones")
    print("  GET  /api/zones/<symbol>  - Simple zone query (test)")
    print("  POST /api/targets         - Calculate trade targets")
    print("  GET  /api/config          - Get configuration")
    print("  --- AI Position Management ---")
    print("  POST /api/position/opened - EA reports position opened")
    print("  POST /api/position/report - EA reports status + gets AI command")
    print("  POST /api/position/closed - EA reports position closed")
    print("  GET  /api/position/status - Position manager status")
    print("=" * 60)

    app.run(
        host=SERVER_CONFIG['host'],
        port=SERVER_CONFIG['port'],
        debug=SERVER_CONFIG['debug'],
        threaded=True
    )
