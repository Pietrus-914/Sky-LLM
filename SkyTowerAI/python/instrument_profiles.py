"""Instrument profiles — the ONE place where a non-forex CFD symbol declares
its units and its risk envelope.

Why this exists
---------------
Every pip-denominated number in the system (LLM SL/TP clamps, spread gates,
break-even tolerances, MODIFY_TP sanity band, ATR/drift in the prompt, path
statistics) assumes a six-letter forex pair: 1 pip = 0.0001 (0.01 for JPY
quotes). Fed a symbol like ``XAUUSD`` (2-digit gold quote) or ``GER40``
(index points) that rule is off by 100-10 000x and the failure is SILENT
(spread read as 200 "pips" -> entry blocked / emergency close; SL of 0.25
index points -> instant stop-out; MODIFY_TP band of 0.05 points -> command
demoted to HOLD).

Contract
--------
* ``profile_for(symbol)`` returns the profile for a known non-forex root
  (broker suffixes stripped, e.g. ``XAUUSD.pro`` -> ``XAUUSD``) and **None for
  every forex pair** — callers keep today's expression in the None branch, so
  the forex flow stays byte-identical:

      max_hold = profile_value(pos.symbol, "max_hold_minutes", <today's value>)

* Units invariant: ``pip_size`` is what "1 pip" means ON THE WIRE for this
  symbol (``stop_loss_pips``, ``spread_pips`` ...). The EA on that chart must
  run with ``InpPipSizeOverride == pip_size`` (0 = its forex rule) and it
  echoes its effective pip in every push/report as ``pip_size`` — the server
  refuses to route to / serve a chart whose echo disagrees (see server.py
  ``_unit_mismatch``).
* Asset-class isolation: statistics, episodes, reaction summaries and
  cross-pair briefs never mix a profiled instrument's magnitudes with forex
  pips (``same_asset_class``). A profiled instrument only ever sees its own
  blocks; forex prompts never see profiled symbols.
* Zero project imports on purpose (this module sits BELOW trading_units,
  which market_context imports). ``market_context.normalize_pair`` delegates
  to ``normalize_root`` here so there is exactly one root rule.

Adding an instrument = adding one ``register(...)`` entry. Never register a
forex pair (guarded). Values marked (verify) come from broker marketing pages
and must be confirmed against the MT5 Symbol Specification / the EA's
``SkyTower SPEC:`` OnInit print before the first live trade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ISO codes that identify a FOREX leg. A six-letter root whose both halves are
# in this set is a forex pair and must NEVER carry a profile (it would silently
# change live pip math). Metals (XAU/XAG) and indices are not in the set.
_FX_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "MXN", "ZAR", "SGD", "HKD", "TRY", "CNH",
    "CNY", "RUB", "ILS", "THB", "INR", "KRW", "BRL",
})

_SUFFIX_RE = re.compile(r"[._\-].*$")


def normalize_root(symbol: Optional[str]) -> str:
    """``'xauusd.pro'`` -> ``'XAUUSD'``, ``'GER40.cash'`` -> ``'GER40'``,
    ``'usd/cad'`` -> ``'USDCAD'``. Single definition of the root rule
    (market_context.normalize_pair delegates here)."""
    p = (symbol or "").upper().replace("/", "")
    return _SUFFIX_RE.sub("", p)


def is_forex_root(symbol: Optional[str]) -> bool:
    root = normalize_root(symbol)
    return len(root) == 6 and root[:3] in _FX_CODES and root[3:6] in _FX_CODES


@dataclass(frozen=True)
class InstrumentProfile:
    name: str                       # canonical root, e.g. "XAUUSD"
    asset_class: str                # "metal" | "index" | "energy"
    pip_size: float                 # price units per 1 pip on the wire
    units_label: str                # human/LLM label, e.g. "pips (1 pip = $0.10)"
    quote_currency: str             # currency the P/L is denominated in
    base_tag: str                   # left leg for base/quote semantics ("XAU", "DAX")
    sl_range: Tuple[float, float]   # LLM stop_loss_pips clamp (must fit the EA chart's
                                    # InpMinSLPips/InpMaxSLPips, see ea_inputs)
    tp_range: Tuple[float, float]   # LLM take_profit_pips clamp
    default_sl_pips: float          # applied when a tradeable decision carries no SL
    exit_range: Tuple[int, int] = (5, 15)   # LLM exit_minutes clamp
    lot_max: int = 85
    # None = keep the global/panel value used by forex today
    max_hold_minutes: Optional[int] = None
    emergency_spread_pips: Optional[float] = None
    typical_news_spread_pips: Optional[float] = None
    exit_llm_interval_seconds: Optional[int] = None
    tp_sanity_band_pips: Optional[float] = None
    rule_be_buffer_pips: Optional[float] = None
    rule_trail_pips: Optional[float] = None
    commission_cushion_usd_per_lot: Optional[float] = None
    exit_system_prompt: Optional[str] = None
    learning_tag: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    # Zone detection (config.ZONE_CONFIG is forex-pip denominated: a 3-pip
    # equal-level tolerance is $0.30 on gold, so stop clusters were never
    # found there). None = keep the global value.
    zone_equal_level_tolerance_pips: Optional[float] = None
    zone_min_fvg_pips: Optional[float] = None
    # Event policy applied when a currency is ROUTED to this instrument
    # (config.routed_event_policy): names (substring, case-insensitive)
    # added to / removed from the tradeable whitelist for that currency.
    # The forex whitelist is tuned for forex reactions; an instrument may
    # react to different releases (gold: Core PCE / PPI move it, Home Sales
    # do not). Ignored when TRADE_ALL_EVENTS is on (skip_events still apply).
    extra_events: Tuple[str, ...] = ()
    skip_events: Tuple[str, ...] = ()
    # Noise gates of the statistics (tools/build_learned_stats) and the
    # calibration ledger, in THIS instrument's pips. The forex globals
    # (2 / 1 / 15 pips) are ~10x too loose on a $0.10-pip gold chart whose
    # news spread alone is 12 pips: a $0.10 "move" counted as a direction.
    # None = the forex globals.
    stats_min_move_pips: Optional[float] = None
    stats_min_directional_pips: Optional[float] = None
    calibration_big_move_pips: Optional[float] = None
    # Recommended EA inputs for the chart of this instrument (documentation
    # rendered in the panel/RUNBOOK; the EA does not read this dict).
    ea_inputs: Dict[str, float] = field(default_factory=dict)
    notes: str = ""

    def carries_currency(self, currency: Optional[str]) -> bool:
        """Is `currency` a leg (or the learning tag) of this instrument?"""
        cur = (currency or "").upper()
        return bool(cur) and cur in (self.quote_currency, self.base_tag, self.learning_tag)


def _assert_not_forex(root: str) -> None:
    if is_forex_root(root):
        raise ValueError(f"{root} is a forex pair — profiles are for non-forex CFDs only")


PROFILES: Dict[str, InstrumentProfile] = {}


def register(profile: InstrumentProfile) -> InstrumentProfile:
    for key in (profile.name,) + tuple(profile.aliases):
        root = normalize_root(key)
        _assert_not_forex(root)
        if root in PROFILES and PROFILES[root] is not profile:
            raise ValueError(f"duplicate instrument profile key {root}")
        PROFILES[root] = profile
    return profile


# Gold. 2-digit quote (point 0.01). We define 1 pip = $0.10 so that a
# typical CPI/NFP stop of $8-10 reads as 80-100 pips and a $0.5-0.6 spread
# as 5-6 pips — the same order of magnitude the forex prompt/clamps and the
# EA gates were tuned for. Ranges from the 2023-26 M1 event study
# (median |m30| ~$8-9 on CPI/NFP, MAE p75 ~$7.4, MFE median ~$14-16) and
# knowledge/learned_stats.json XAUUSD blocks.
register(InstrumentProfile(
    name="XAUUSD",
    asset_class="metal",
    pip_size=0.10,
    units_label="pips (1 pip = $0.10 on XAUUSD)",
    quote_currency="USD",
    base_tag="XAU",
    # SL floor 60 = $6: the CPI adverse wick (excursion AGAINST the eventual
    # direction in the first 5 min) is p75 54 / p80 65 pips, so a $5 stop was
    # wicked out in a quarter of CORRECT calls. Ceiling 120 = $12 covers the
    # FOMC p90 (101). Widening the clamp costs no extra dollar risk — the EA
    # sizes the lot from the stop distance.
    sl_range=(60.0, 120.0),          # $6 .. $12 (EA chart: InpMinSLPips 60, InpMaxSLPips 120)
    tp_range=(30.0, 400.0),          # $3 .. $40
    default_sl_pips=80.0,            # $8 — median CPI/NFP |m30| ~ $8-9
    exit_range=(5, 15),
    emergency_spread_pips=40.0,      # $4.00
    typical_news_spread_pips=12.0,   # ~$1.2 at the print (verify)
    rule_be_buffer_pips=5.0,         # $0.50 instead of $0.10
    rule_trail_pips=30.0,            # $3 instead of $1
    learning_tag="XAU",
    aliases=("GOLD",),
    # Equal highs/lows within $1.50 form a stop cluster on gold (M5 bars
    # around a print range $1-4); FVGs below $1 are noise.
    zone_equal_level_tolerance_pips=15.0,
    zone_min_fvg_pips=10.0,
    # tools/strategy_lab.py --pair XAUUSD (4 721 paths 2023-26, entry at T0
    # in the surprise direction, 15-min exit, $1.2 spread): CPI +80..100 pips,
    # NFP +64, Core PCE +28, GDP Price Index +29, PPI/Core PPI +14..15;
    # New/Existing Home Sales -14 (the surprise does not drive gold).
    extra_events=("Core PCE Price Index", "PPI"),
    skip_events=("New Home Sales", "Existing Home Sales"),
    # Same proportion to the typical print move as the forex gates
    # (2 / 1 / 15 pips on 30-50 pip moves): 10 / 5 / 50 pips on 80-100.
    stats_min_move_pips=10.0,
    stats_min_directional_pips=5.0,
    calibration_big_move_pips=50.0,
    # Spread gates keep the FOREX proportions (spread / typical print move):
    # forex max 10 / extreme 15 pips on a 30-50 pip move -> gold max 25 /
    # extreme 30 pips ($2.5 / $3.0) on an 80-pip move. Tune from the EA log
    # ("Spread EXTREME" / "Trade blocked by final spread check" lines).
    ea_inputs={"InpPipSizeOverride": 0.10, "InpMaxSpreadPips": 25, "InpExtremeSpreadPips": 30,
               "InpEmergencySpreadPips": 40, "InpUseSpreadLotReduction": 0,
               "InpSlippage": 30, "InpMaxMarginUsePercent": 50, "InpMinSLPips": 60,
               "InpMaxSLPips": 120},
    notes="Leverage 1:100 at Purple SC (verify). Spread ~$0.5-0.6 normal, widening at prints unmeasured. "
          "At 1:100 a $1 000 account carries ~0.2 lot (margin-capped): the risk at the stop is then "
          "~$160, not the panel budget — the EA echoes it as risk_usd.",
))

# German index CFD. Quote in index points (1-2 decimals). 1 pip = 1 point.
# max_hold / exit cadence deliberately inherit the panel/global values: a
# per-instrument hold horizon needs the panel and the EA's InpMaxHoldMinutes
# to agree, which v1 does not wire (see research/DAX_OPEN_PLAN.md).
register(InstrumentProfile(
    name="GER40",
    asset_class="index",
    pip_size=1.0,
    units_label="index points (1 pip = 1.0 point on GER40)",
    quote_currency="EUR",
    base_tag="DAX",
    sl_range=(20.0, 100.0),
    tp_range=(15.0, 250.0),
    default_sl_pips=60.0,
    exit_range=(5, 15),
    emergency_spread_pips=10.0,
    typical_news_spread_pips=4.0,    # 2.1-2.5 normal, widening at prints (verify)
    rule_be_buffer_pips=2.0,
    rule_trail_pips=15.0,
    learning_tag="DAX",
    aliases=("DE40", "DAX40", "GER30", "DE30"),
    zone_equal_level_tolerance_pips=5.0,
    zone_min_fvg_pips=3.0,
    ea_inputs={"InpPipSizeOverride": 1.0, "InpMaxSpreadPips": 4, "InpExtremeSpreadPips": 10,
               "InpEmergencySpreadPips": 10, "InpUseSpreadLotReduction": 0,
               "InpSlippage": 300, "InpMaxMarginUsePercent": 50, "InpMinSLPips": 20,
               "InpMaxSLPips": 100},
    notes="Leverage 1:100 at Purple SC (verify).",
))

# S&P 500 index CFD. 1 pip = 1 index point. Stops of 8-19 points are only
# honoured if the EA chart runs InpMinSLPips=8 (default 20 would widen them).
register(InstrumentProfile(
    name="US500",
    asset_class="index",
    pip_size=1.0,
    units_label="index points (1 pip = 1.0 point on US500)",
    quote_currency="USD",
    base_tag="SPX",
    sl_range=(8.0, 60.0),
    tp_range=(6.0, 100.0),
    default_sl_pips=20.0,
    exit_range=(5, 15),
    emergency_spread_pips=5.0,
    typical_news_spread_pips=1.5,    # 0.4-0.7 normal (verify widening)
    rule_be_buffer_pips=1.0,
    rule_trail_pips=8.0,
    learning_tag="SPX",
    aliases=("SPX500", "SP500"),
    zone_equal_level_tolerance_pips=1.5,
    zone_min_fvg_pips=1.0,
    ea_inputs={"InpPipSizeOverride": 1.0, "InpMaxSpreadPips": 3, "InpExtremeSpreadPips": 5,
               "InpEmergencySpreadPips": 5, "InpUseSpreadLotReduction": 0,
               "InpSlippage": 100, "InpMaxMarginUsePercent": 50, "InpMinSLPips": 8,
               "InpMaxSLPips": 60},
    notes="Contract size $1 vs $10/pt UNVERIFIED — read SkyTower SPEC before any live trade.",
))


def profile_for(symbol: Optional[str]) -> Optional[InstrumentProfile]:
    """Profile for a non-forex CFD root, else None (forex / unknown)."""
    if not symbol:
        return None
    return PROFILES.get(normalize_root(symbol))


def canonical_symbol(symbol: Optional[str]) -> str:
    """Root with profile aliases resolved: ``'GOLD'`` -> ``'XAUUSD'``,
    ``'xauusd.pro'`` -> ``'XAUUSD'``, forex roots unchanged. The routing
    table stores THIS form so a routed alias matches the EA's push root."""
    root = normalize_root(symbol)
    prof = PROFILES.get(root)
    return prof.name if prof is not None else root


def zone_config_for(symbol: Optional[str], base_config: Dict) -> Dict:
    """ZONE_CONFIG with the instrument's own pip-denominated detection
    thresholds substituted (forex: the very same dict object)."""
    prof = profile_for(symbol)
    if prof is None:
        return base_config
    cfg = dict(base_config)
    if prof.zone_equal_level_tolerance_pips is not None:
        cfg["equal_level_tolerance_pips"] = float(prof.zone_equal_level_tolerance_pips)
    if prof.zone_min_fvg_pips is not None:
        cfg["min_fvg_size_pips"] = float(prof.zone_min_fvg_pips)
    return cfg


def event_policy_for(symbol: Optional[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(extra_events, skip_events)`` of the instrument, ``((), ())`` for
    forex / unknown."""
    prof = profile_for(symbol)
    if prof is None:
        return (), ()
    return tuple(prof.extra_events), tuple(prof.skip_events)


def profile_value(symbol: Optional[str], field_name: str, default):
    """``profile.<field>`` when the symbol has a profile AND the field is set,
    else ``default`` — the "None means today's behaviour" contract in one call."""
    prof = profile_for(symbol)
    if prof is None:
        return default
    value = getattr(prof, field_name, None)
    return default if value is None else value


def symbol_carries_currency(symbol: Optional[str], currency: Optional[str]) -> bool:
    """Does the symbol expose the event currency? Forex: the six-letter root
    contains it as base or quote (unchanged rule). Profiled instrument: its
    quote currency / base tag / learning tag."""
    prof = profile_for(symbol)
    if prof is not None:
        return prof.carries_currency(currency)
    root = normalize_root(symbol)
    cur = (currency or "").upper()
    return len(root) >= 6 and bool(cur) and cur in (root[:3], root[3:6])


def same_asset_class(symbol_a: Optional[str], symbol_b: Optional[str]) -> bool:
    """Forex <-> forex, or the SAME profiled instrument. Everything that pools
    magnitudes across symbols (stats fallback, episodes, cross-pair briefs,
    reaction averages) must pass this gate — a $0.10-pip gold number next to
    a 0.0001-pip forex number is silently wrong either way."""
    pa, pb = profile_for(symbol_a), profile_for(symbol_b)
    if pa is None and pb is None:
        return True
    return pa is not None and pb is not None and pa.name == pb.name


def validate_routing_symbol(symbol: Optional[str], currency: Optional[str]) -> Optional[str]:
    """None if `symbol` may be routed for events of `currency`, else the
    reason: the symbol must be a forex pair or a profiled instrument, AND the
    currency must be one of its legs (a decision on an instrument that does
    not carry the event currency has no direction semantics)."""
    root = normalize_root(symbol)
    if not root:
        return "empty symbol"
    if not is_forex_root(root) and profile_for(root) is None:
        return (f"{root}: no instrument profile — add it to instrument_profiles.py "
                f"before routing to it")
    if not symbol_carries_currency(root, currency):
        return f"{root} does not carry {str(currency).upper()} (base/quote) — cannot route"
    return None


__all__ = [
    "InstrumentProfile", "PROFILES", "register", "profile_for", "profile_value",
    "normalize_root", "canonical_symbol", "is_forex_root", "symbol_carries_currency",
    "same_asset_class", "validate_routing_symbol", "zone_config_for", "event_policy_for",
]
