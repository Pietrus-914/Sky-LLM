"""
Market context builder for entry decisions.

Turns raw OHLC (pushed by the EA via /api/register-pair) and zone reports
into a compact, LLM-friendly summary: trend per timeframe, volatility (ATR),
position within the daily range, and distances to recent extremes.

Pure functions, no I/O — the server wires this into the decision pipeline.
"""
import re
import math
from typing import Dict, List, Optional
from datetime import datetime, timezone
from timeutil import utcnow
from trading_units import forex_pip_size


def pip_size(pair: str) -> float:
    """Pip size for a pair (same 4/5-digit convention as ZoneAnalyzer._to_pips)."""
    return forex_pip_size(pair)


def _positive_finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def normalize_pair(pair: str) -> str:
    """
    Canonical pair name for matching and for the EA: uppercase, no slash,
    broker suffix stripped ("EURUSD.pro" / "USDCAD_SB" / "usd/cad" -> "EURUSD"/"USDCAD"/"USDCAD").
    The EA resolves its broker's actual symbol (suffix, case) itself via
    ConvertPairToSymbol, so bare names are what must cross the wire.
    """
    p = (pair or "").upper().replace("/", "")
    return re.sub(r"[._\-].*$", "", p)


def _closes(bars: List[Dict]) -> List[float]:
    return [float(b.get("close", 0)) for b in bars]


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _trend(bars: List[Dict], sma_period: int = 20) -> str:
    """
    Simple trend classification: close vs SMA(20) combined with
    higher-highs / lower-lows over the last `sma_period` bars.
    """
    if len(bars) < sma_period:
        return "UNKNOWN"

    closes = _closes(bars)
    last_close = closes[-1]
    sma = _sma(closes, sma_period)

    recent = bars[-sma_period:]
    half = sma_period // 2
    first_half, second_half = recent[:half], recent[half:]
    hh = max(float(b["high"]) for b in second_half) > max(float(b["high"]) for b in first_half)
    ll = min(float(b["low"]) for b in second_half) < min(float(b["low"]) for b in first_half)

    if last_close > sma and hh and not ll:
        return "UP"
    if last_close < sma and ll and not hh:
        return "DOWN"
    if last_close > sma:
        return "UP_WEAK"
    if last_close < sma:
        return "DOWN_WEAK"
    return "SIDEWAYS"


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Simple (non-smoothed) RSI over the last `period` bars."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for prev, cur in zip(closes[-(period + 1):-1], closes[-period:]):
        delta = cur - prev
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = gains / losses
    return round(100 - 100 / (1 + rs), 1)


def _drift_pips(bars: List[Dict], n_bars: int, pip: float) -> Optional[float]:
    """Price change over the last n_bars (pre-news drift / momentum)."""
    if len(bars) < n_bars + 1:
        return None
    return round((float(bars[-1]["close"]) - float(bars[-(n_bars + 1)]["close"])) / pip, 1)


from timeutil import to_naive_utc, utc_epoch as _utc_epoch


def entry_age_seconds(entry: Dict, now: datetime = None) -> float:
    """Age of a pushed market-data entry (its 'updated_at' ISO stamp) in
    seconds. Canonical freshness helper — server gates and the path recorder
    must agree on the same answer. inf when the stamp is missing/garbage."""
    try:
        ts = to_naive_utc(datetime.fromisoformat(entry.get('updated_at', '')))
        return ((now or utcnow()) - ts).total_seconds()
    except (ValueError, TypeError):
        return float('inf')


def infer_broker_offset_seconds(bars: List[Dict], pushed_at: datetime) -> Optional[int]:
    """
    MT5 bar times (rates[].time) are BROKER time epochs, not UTC. Infer the
    broker's UTC offset from the newest bar: a fresh finest-timeframe bar
    opened at most ~2 minutes before the push, so (last_bar_time - push_time)
    rounded to the nearest 30 min is the offset.

    Returns None when not inferable — including when the newest bar is too
    OLD relative to the push (quiet market, halt): a large gap would snap the
    rounding to a wrong 30-min multiple, and a silently-wrong offset poisons
    both candle labels and path measurements. Callers must treat None as
    "don't trust bar times right now", not as offset 0.
    """
    if not bars:
        return None
    try:
        last_t = int(bars[-1].get("time", 0))
    except (TypeError, ValueError):
        return None
    if last_t <= 0:
        return None
    raw = last_t - _utc_epoch(pushed_at)
    offset = int(round((raw + 60) / 1800.0)) * 1800
    # FX brokers live within UTC-12..UTC+14; anything else is a broken clock
    if not -12 * 3600 <= offset <= 14 * 3600:
        return None
    # Residual = how far the newest bar sits from "just before the push".
    # A fresh M1/M5 bar gives residual in [-120, 0]; beyond ~8 minutes the
    # 30-min rounding can no longer be trusted — refuse instead of guessing.
    if abs(raw - offset + 60) > 480:
        return None
    return offset


def _compact_candles(bars: List[Dict], count: int,
                     broker_offset_seconds: int = 0) -> List[str]:
    """Last `count` bars as compact 'HH:MM O/H/L/C' strings (oldest first).
    Bar times arrive in BROKER time — broker_offset_seconds shifts the labels
    to true UTC (the prompt promises UTC; unshifted labels would misalign the
    candles against the event time the model reasons about)."""
    out = []
    for b in bars[-count:]:
        try:
            ts = datetime.fromtimestamp(int(b.get("time", 0)) - broker_offset_seconds,
                                        tz=timezone.utc).strftime("%H:%M")
        except (ValueError, OSError, OverflowError):
            ts = "--:--"
        out.append(f"{ts} {float(b['open']):.5f}/{float(b['high']):.5f}/"
                   f"{float(b['low']):.5f}/{float(b['close']):.5f}")
    return out


def _atr_pips(bars: List[Dict], pair: str, period: int = 14) -> Optional[float]:
    """Classic ATR(14) expressed in pips."""
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(period + 1):-1], bars[-period:]):
        high, low = float(cur["high"]), float(cur["low"])
        prev_close = float(prev["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return round((sum(trs) / period) / pip_size(pair), 1)


def build_market_context(
    ohlc_by_timeframe: Dict[str, List[Dict]],
    pair: str,
    zones: Optional[Dict] = None,
    registered_at: Optional[str] = None,
    spread_points: Optional[float] = None,
    spread_pips: Optional[float] = None,
) -> Optional[Dict]:
    """
    Build the market-context dict for the entry LLM prompt.

    Args:
        ohlc_by_timeframe: {"M5": [bars], "M15": [bars], "H1": [bars]} —
            each bar a dict with open/high/low/close (time/volume optional),
            oldest first.
        pair: e.g. "EURUSD"
        zones: optional zone report (zone_bias, direction_bias,
            nearest_resistance, nearest_support, ...)
        registered_at: ISO timestamp of when the OHLC was pushed, for staleness info

    Returns:
        Summary dict, or None if there is no usable OHLC at all.
    """
    ohlc_by_timeframe = ohlc_by_timeframe or {}
    usable = {tf: bars for tf, bars in ohlc_by_timeframe.items() if bars and len(bars) >= 2}
    if not usable and not zones:
        return None

    context: Dict = {"pair": pair}
    pip = pip_size(pair)

    if usable:
        # Last price from the finest available timeframe
        finest = next((tf for tf in ("M1", "M5", "M15", "H1") if tf in usable), None)
        if finest is None:
            finest = list(usable.keys())[0]
        last_price = float(usable[finest][-1]["close"])
        context["last_price"] = last_price

        context["trend"] = {tf: _trend(bars) for tf, bars in usable.items()}
        context["atr_pips"] = {
            tf: atr for tf, bars in usable.items()
            if (atr := _atr_pips(bars, pair)) is not None
        }
        context["rsi14"] = {
            tf: rsi for tf, bars in usable.items()
            if (rsi := _rsi(_closes(bars))) is not None
        }
        context["bars_analyzed"] = {tf: len(bars) for tf, bars in usable.items()}

        # Pre-news drift: how far price has already run into the event
        # (strategy principle: pre-positioning often reverses on release)
        drift = {}
        if "M1" in usable:
            d = _drift_pips(usable["M1"], 15, pip)
            if d is not None:
                drift["last_15min"] = d
        if "M5" in usable:
            for label, n in (("last_30min", 6), ("last_60min", 12), ("last_3h", 36)):
                d = _drift_pips(usable["M5"], n, pip)
                if d is not None:
                    drift[label] = d
        if drift:
            context["drift_pips"] = drift

        # Raw candles for the LLM (compact, oldest->newest); kept out of the
        # summary JSON block by the prompt builder and rendered as a table.
        # M1 tail is deliberately short (token budget) — fine pre-news detail
        # comes mostly from trend/RSI/drift computed on the full M1 set.
        # Bar times are broker time — shift labels to the promised UTC using
        # the offset inferred from the finest timeframe's newest bar.
        push_ts = None
        if registered_at:
            try:
                push_ts = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                push_ts = None
        # Only M1/M5 bars are fresh enough for a reliable inference (an H1
        # bar can be an hour old, which defeats the 30-min rounding)
        offset = None
        if finest in ("M1", "M5"):
            offset = infer_broker_offset_seconds(usable[finest], push_ts or utcnow())
        if offset is None:
            # Don't silently regress to mislabeled times — tell the model
            # (this note stays in the summary JSON the prompt renders)
            context["candle_times_note"] = (
                "bar clock offset could not be verified — candle HH:MM labels "
                "may be BROKER time, not UTC; do not anchor timing on them")
        context["candles"] = {}
        for tf, count in (("M1", 20), ("M5", 36), ("M15", 24), ("H1", 24)):
            if tf in usable:
                context["candles"][tf] = _compact_candles(usable[tf], count,
                                                          offset or 0)

        # Daily range position & distance to extremes from the widest window we have
        widest = max(usable.values(), key=len) if "H1" not in usable else usable["H1"]
        window_high = max(float(b["high"]) for b in widest)
        window_low = min(float(b["low"]) for b in widest)
        # Include last_price so a fresher fine-timeframe close can't fall outside the window
        window_high = max(window_high, last_price)
        window_low = min(window_low, last_price)
        if window_high > window_low:
            context["range_position_pct"] = round(
                (last_price - window_low) / (window_high - window_low) * 100
            )
        context["distance_to_recent_high_pips"] = round((window_high - last_price) / pip, 1)
        context["distance_to_recent_low_pips"] = round((last_price - window_low) / pip, 1)

    explicit_spread = _positive_finite(spread_pips)
    legacy_spread = _positive_finite(spread_points)
    if explicit_spread is not None:
        context["current_spread_pips"] = round(explicit_spread, 1)
    elif spread_pips is None and legacy_spread is not None:
        # Backward compatibility for pre-stage-2 EAs, which reported points
        # from the project's standard 3/5-digit broker feed.
        context["current_spread_pips"] = round(legacy_spread / 10.0, 1)

    if zones:
        context["zones"] = _summarize_zones(zones, context.get("last_price"), pip)

    if registered_at:
        context["data_age_minutes"] = _age_minutes(registered_at)

    return context


def summarize_pair_brief(
    ohlc_by_timeframe: Dict[str, List[Dict]],
    pair: str,
    event_currency: str,
    spread_points: Optional[float] = None,
    spread_pips: Optional[float] = None,
) -> Optional[str]:
    """
    One-line technical brief of a sibling pair for the CROSS-PAIR PICTURE
    prompt section. CRITICAL: states the base/quote semantics explicitly —
    'USD strength = price UP' is only true when USD is the BASE currency
    (USDCAD yes, GBPUSD no), and the model must never assume up = strong.
    """
    ohlc_by_timeframe = ohlc_by_timeframe or {}
    usable = {tf: bars for tf, bars in ohlc_by_timeframe.items()
              if bars and len(bars) >= 2}
    if not usable:
        return None

    norm = normalize_pair(pair)
    currency = (event_currency or '').upper()
    if len(norm) >= 6 and norm[:3] == currency:
        semantics = f"{currency} is BASE -> {currency} strength = price UP"
    elif len(norm) >= 6 and norm[3:6] == currency:
        semantics = f"{currency} is QUOTE -> {currency} strength = price DOWN"
    else:
        semantics = "event currency not in pair"

    pip = pip_size(norm)
    parts = []

    trends = {tf: _trend(bars) for tf, bars in usable.items()}
    trend_txt = " / ".join(f"{tf} {trends[tf]}"
                           for tf in ("M1", "M5", "M15", "H1") if tf in trends)
    if trend_txt:
        parts.append(f"trend {trend_txt}")

    for tf in ("M5", "M15", "H1"):
        if tf in usable:
            rsi = _rsi(_closes(usable[tf]))
            if rsi is not None:
                parts.append(f"RSI14({tf}) {rsi}")
            break

    if "M5" in usable:
        d = _drift_pips(usable["M5"], 12, pip)
        if d is not None:
            parts.append(f"drift 60min {d:+.1f} pips")

    widest = max(usable.values(), key=len)
    last_price = float(usable[next(iter(usable))][-1]["close"])
    window_high = max(max(float(b["high"]) for b in widest), last_price)
    window_low = min(min(float(b["low"]) for b in widest), last_price)
    if window_high > window_low:
        pos = round((last_price - window_low) / (window_high - window_low) * 100)
        parts.append(f"range pos {pos}%")

    explicit_spread = _positive_finite(spread_pips)
    legacy_spread = _positive_finite(spread_points)
    if explicit_spread is not None:
        parts.append(f"spread {explicit_spread:.1f} pips")
    elif spread_pips is None and legacy_spread is not None:
        parts.append(f"spread {legacy_spread / 10.0:.1f} pips")

    if not parts:
        return None
    return f"{norm} [{semantics}]: " + ", ".join(parts)


def _summarize_zones(zones: Dict, last_price: Optional[float], pip: float) -> Dict:
    """Compact zone summary (S/R distances, bias) for the prompt."""
    summary: Dict = {
        "direction_bias": zones.get("direction_bias", "neutral"),
        "bias_strength": zones.get("bias_strength", zones.get("zone_bias", 0)),
    }
    resistance = zones.get("nearest_resistance") or 0
    support = zones.get("nearest_support") or 0
    price = last_price or zones.get("current_price") or 0
    # Sanity: resistance must be above price, support below, and within a
    # plausible range — otherwise the zone report belongs to a different
    # pair/price scale and would feed absurd pip distances to the LLM.
    MAX_LEVEL_DISTANCE_PIPS = 1000
    if resistance and price:
        distance = round((resistance - price) / pip, 1)
        if 0 < distance <= MAX_LEVEL_DISTANCE_PIPS:
            summary["nearest_resistance"] = resistance
            summary["resistance_distance_pips"] = distance
    if support and price:
        distance = round((price - support) / pip, 1)
        if 0 < distance <= MAX_LEVEL_DISTANCE_PIPS:
            summary["nearest_support"] = support
            summary["support_distance_pips"] = distance
    # Multi-instance registrations carry full zone lists — count them
    for key in ("resistance_zones", "support_zones", "fvg_zones"):
        if isinstance(zones.get(key), list):
            summary[f"{key}_count"] = len(zones[key])

    # Stop clusters (equal-high/low liquidity pools), computed server-side from
    # the pair's own OHLC. Report the NEAREST cluster on each side as a pip
    # distance + strength: the model reads a cluster in the SURPRISE direction
    # as potential fuel that can extend the move if run — never as a target.
    # Same MAX_LEVEL_DISTANCE_PIPS gate as S/R keeps a wrong-scale level out.
    pools = zones.get("liquidity_pools")
    if isinstance(pools, list) and price:
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            level = pool.get("price") or 0
            if not level:
                continue
            signed = round((level - price) / pip, 1)
            side = "liquidity_above" if signed > 0 else "liquidity_below"
            dist = abs(signed)
            if not (0 < dist <= MAX_LEVEL_DISTANCE_PIPS):
                continue
            prev = summary.get(side)
            if prev is None or dist < prev["distance_pips"]:
                summary[side] = {"distance_pips": dist,
                                 "strength": pool.get("strength"),
                                 "touches": pool.get("touches")}
        if zones.get("liquidity_tf") and (
                "liquidity_above" in summary or "liquidity_below" in summary):
            summary["liquidity_tf"] = zones["liquidity_tf"]
    return summary


def _age_minutes(iso_timestamp: str) -> Optional[int]:
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return max(0, int((utcnow() - ts).total_seconds() // 60))
    except (ValueError, AttributeError):
        return None
