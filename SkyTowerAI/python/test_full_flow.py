"""
Full Trading Flow Test
Tests the complete flow from signal to trade execution timing
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

import requests
import time
from datetime import datetime

# Configuration
SERVER_URL = "http://127.0.0.1:5555"
TEST_SECONDS_UNTIL_EVENT = 120  # 2 minutes in the future for testing


def check_server():
    """Check if server is running"""
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        return r.status_code == 200
    except:
        return False


def create_test_signal(pair: str, direction: str, seconds_until: int):
    """Create a test signal"""
    print(f"\n{'='*60}")
    print(f"Creating TEST signal: {direction} {pair}")
    print(f"Event in: {seconds_until} seconds ({seconds_until/60:.1f} min)")
    print(f"{'='*60}")

    try:
        r = requests.post(
            f"{SERVER_URL}/api/test-signal",
            json={
                "pair": pair,
                "direction": direction,
                "seconds_until": seconds_until,
                "lot_percent": 80,
                "confidence": 0.75,
                "exit_minutes": 5,
                "event_name": "TEST - Full Flow Test"
            },
            timeout=10
        )

        if r.status_code == 200:
            data = r.json()
            print(f"Signal created successfully!")
            print(f"  Event time: {data.get('event_time', 'N/A')}")
            return True
        else:
            print(f"Failed: {r.text}")
            return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def get_signal():
    """Get current signal (as MT5 would)"""
    try:
        r = requests.get(f"{SERVER_URL}/api/signal", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def simulate_mt5_polling():
    """Simulate MT5 EA polling the server"""
    print(f"\n{'='*60}")
    print("SIMULATING MT5 EA BEHAVIOR")
    print(f"{'='*60}")
    print("MT5 polls server every 60 seconds for signals")
    print("When event is within 60 seconds, it prepares to trade")
    print("At 15 seconds before event, it opens position")
    print(f"{'='*60}\n")

    check_interval = 10  # Faster for testing (real EA uses 60s)
    trade_executed = False

    while not trade_executed:
        signal = get_signal()
        now = datetime.now()

        if signal.get("signal"):
            time_until = signal.get("time_until_event", 0)
            direction = signal.get("direction")
            pair = signal.get("pair")
            confidence = signal.get("confidence", 0)
            reasoning = signal.get("reasoning", "")[:80]

            print(f"[{now.strftime('%H:%M:%S')}] SIGNAL: {direction} {pair}")
            print(f"           Confidence: {confidence:.0%} | Time until: {time_until}s")
            print(f"           Reasoning: {reasoning}...")

            # Simulate entry timing
            if time_until <= 15 and time_until > 0:
                print(f"\n{'*'*60}")
                print(f"* TRADE EXECUTION TIME!")
                print(f"* {direction} {pair} @ {confidence:.0%} confidence")
                print(f"* Entry: NOW (15 seconds before event)")
                print(f"{'*'*60}")
                trade_executed = True

                # Notify server that trade was executed
                try:
                    requests.get(f"{SERVER_URL}/api/trade-executed", timeout=5)
                    print("Server notified of trade execution.")
                except:
                    pass

            elif time_until < 0:
                print(f"  Event passed ({time_until}s ago). Resetting...")
                trade_executed = True
        else:
            msg = signal.get("message", "No signal")
            print(f"[{now.strftime('%H:%M:%S')}] No signal. {msg}")

        if not trade_executed:
            time.sleep(check_interval)


def test_llm_decision_timing():
    """Test how long LLM decision takes"""
    print(f"\n{'='*60}")
    print("TESTING LLM DECISION TIMING")
    print(f"{'='*60}")

    # First, clear any existing signal
    try:
        requests.post(f"{SERVER_URL}/api/test-signal/clear", timeout=5)
    except:
        pass

    # Force refresh decision (triggers LLM)
    print("Forcing decision refresh (this calls the LLM)...")
    start = time.time()

    try:
        r = requests.post(f"{SERVER_URL}/api/decision/refresh", timeout=120)
        elapsed = time.time() - start

        if r.status_code == 200:
            data = r.json()
            decision = data.get("decision", {})

            print(f"\nDecision generated in: {elapsed:.1f} seconds")
            print(f"-" * 40)

            if decision:
                print(f"Event: {decision.get('event', 'N/A')}")
                print(f"Currency: {decision.get('currency', 'N/A')}")
                print(f"Direction: {decision.get('direction', 'N/A')}")
                print(f"Confidence: {decision.get('confidence', 0):.0%}")
                print(f"Reasoning: {decision.get('reasoning', 'N/A')[:100]}...")
            else:
                print("No upcoming tradeable events.")

            return elapsed
        else:
            print(f"Error: {r.text}")
            return None
    except Exception as e:
        elapsed = time.time() - start
        print(f"Error after {elapsed:.1f}s: {e}")
        return None


def main():
    print("=" * 60)
    print("SkyTower-AI Full Flow Test")
    print("=" * 60)

    # Check server
    if not check_server():
        print("\nERROR: Server not running!")
        print("Start the server first: python server.py")
        return

    print("Server: OK")

    # Menu
    print("\nTest options:")
    print("1. Test LLM decision timing (see how long decisions take)")
    print("2. Create test signal and simulate MT5 (quick test)")
    print("3. Full test with real upcoming event (uses LLM)")
    print("4. Check current signal status")

    try:
        choice = input("\nSelect option (1-4): ").strip()
    except:
        choice = "1"

    if choice == "1":
        test_llm_decision_timing()

    elif choice == "2":
        # Quick test with artificial signal
        if create_test_signal("NZDUSD", "BUY", TEST_SECONDS_UNTIL_EVENT):
            simulate_mt5_polling()

    elif choice == "3":
        # Test with real LLM decision
        print("\nGenerating real decision with LLM...")
        elapsed = test_llm_decision_timing()

        if elapsed:
            print(f"\nLLM Decision Timing Analysis:")
            print(f"-" * 40)
            print(f"Decision generation: {elapsed:.1f}s")
            print(f"")
            print(f"RECOMMENDED TIMING:")
            print(f"  - Pre-load decision: {max(300, elapsed * 2 + 60):.0f}s before event")
            print(f"  - This allows time for retries if API fails")
            print(f"")
            print(f"Current server settings:")
            print(f"  - Pre-load: 300s (5 min) before event")
            print(f"  - Entry: 15s before event")

    elif choice == "4":
        signal = get_signal()
        print("\nCurrent signal status:")
        print("-" * 40)

        if signal.get("signal"):
            print(f"Direction: {signal.get('direction')}")
            print(f"Pair: {signal.get('pair')}")
            print(f"Confidence: {signal.get('confidence', 0):.0%}")
            print(f"Time until event: {signal.get('time_until_event')}s")
            print(f"Event: {signal.get('event_name')}")
            print(f"Reasoning: {signal.get('reasoning', '')[:100]}...")
        else:
            print(f"No active signal: {signal.get('message', signal.get('error', 'Unknown'))}")

    print("\n" + "=" * 60)
    print("Test complete!")


if __name__ == "__main__":
    main()
