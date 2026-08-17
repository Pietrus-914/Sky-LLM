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

      prof = profile_for(pos.symbol)
      max_hold = prof.max_hold_minutes if prof and prof.max_hold_minutes else <today>

  ``profile_value(symbol, field, default)`` is the same thing in one call.
* Units invariant: ``pip_size`` is what "1 pip" means ON THE WIRE for this
  symbol (``stop_loss_pips``, ``spread_pips`` ...). The EA must be attached to
  the chart with ``InpPipSizeOverride == pip_size`` (0 = its forex rule) so
  both sides count the same unit. This is a documented convention, verified
  at dry-run — not enforced at runtime (yet).
* Zero project imports on purpose (this module sits BELOW trading_units,
  which market_context imports) — the root normalisation duplicates
  market_context.normalize_pair semantics deliberately.

Adding an instrument = adding one entry to ``PROFILES``. Never register a
forex pair here (guarded by ``_assert_not_forex``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# ISO codes that identify a FOREX leg. A six-letter root whose both halves are
# in this set is a forex pair and must NEVER carry a profile (it would silently
# change live pip math). Metals (XAU/XAG) and indices are not in the set.
_FX_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "MXN", "ZAR", "SGD", "HKD", "TRY", "CNH",
    "CNY", "RUB", "ILS", "THB", "INR", "KRW", "BRL",
})


def normalize_root(symbol: str) -> str:
    """``'xauusd.pro'`` -> ``'XAUUSD'``, ``'GER40.cash'`` -> ``'GER40'``,
    ``'usd/cad'`` -> ``'USDCAD'`` (same rule as market_context.normalize_pair;
    duplicated to keep this module import-free)."""
    p = (symbol or "").upper().replace("/", "")
    return re.sub(r"[._\-].*$", "", p)


@dataclass(frozen=True)
class InstrumentProfile:
    name: str                       # canonical root, e.g. "XAUUSD"
    asset_class: str                # "metal" | "index" | "energy"
    pip_size: float                 # price units per 1 pip on the wire
    units_label: str                # human/LLM label, e.g. "pips (1 pip = $0.10)"
    price_digits: int               # quote decimals for formatting
    quote_currency: str             # currency the P/L is denominated in
    base_tag: str                   # left leg for base/quote semantics ("XAU", "DAX")
    sl_range: Tuple[float, float]   # LLM stop_loss_pips clamp
    tp_range: Tuple[float, float]   # LLM take_profit_pips clamp
    exit_range: Tuple[int, int] = (5, 15)   # LLM exit_minutes clamp
    lot_max: int = 85
    # None = keep the global/default value used by forex today
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
    notes: str = ""

    def clamp_sl(self, pips: float) -> float:
        lo, hi = self.sl_range
        return min(max(float(pips), lo), hi)

    def clamp_tp(self, pips: float) -> float:
        lo, hi = self.tp_range
        return min(max(float(pips), lo), hi)


def _assert_not_forex(root: str) -> None:
    if len(root) == 6 and root[:3] in _FX_CODES and root[3:6] in _FX_CODES:
        raise ValueError(f"{root} is a forex pair — profiles are for non-forex CFDs only")


# ---------------------------------------------------------------------------
# Registry. Values marked (verify) come from broker marketing pages / desk
# knowledge and must be confirmed against the MT5 Symbol Specification of the
# account actually used before the first live trade on that instrument.
# ---------------------------------------------------------------------------
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
# (median |m30| ~$8-9 on CPI/NFP, MAE p75 ~$7.4, MFE median ~$14-16).
register(InstrumentProfile(
    name="XAUUSD",
    asset_class="metal",
    pip_size=0.10,
    units_label="pips (1 pip = $0.10 on XAUUSD)",
    price_digits=2,
    quote_currency="USD",
    base_tag="XAU",
    sl_range=(50.0, 250.0),          # $5 .. $25
    tp_range=(30.0, 400.0),          # $3 .. $40
    exit_range=(5, 15),
    lot_max=85,
    max_hold_minutes=None,           # keep the global 30 min
    emergency_spread_pips=40.0,      # $4.00
    typical_news_spread_pips=12.0,   # ~$1.2 at the print (verify)
    exit_llm_interval_seconds=None,
    tp_sanity_band_pips=None,        # 500 pips = $50: fine as-is
    rule_be_buffer_pips=5.0,         # $0.50 instead of $0.10
    rule_trail_pips=30.0,            # $3 instead of $1
    commission_cushion_usd_per_lot=None,
    learning_tag="XAU",
    aliases=("GOLD",),
    notes="EA: InpPipSizeOverride=0.10, InpMaxSpreadPips~15 ($1.5), "
          "InpEmergencySpreadPips~40, InpSlippage~30 pts ($0.30). Leverage 1:100 (verify).",
))

# German index CFD. Quote in index points (1-2 decimals). 1 pip = 1 point.
register(InstrumentProfile(
    name="GER40",
    asset_class="index",
    pip_size=1.0,
    units_label="index points (1 pip = 1.0 point on GER40)",
    price_digits=1,
    quote_currency="EUR",
    base_tag="DAX",
    sl_range=(20.0, 150.0),
    tp_range=(15.0, 250.0),
    exit_range=(5, 30),
    lot_max=85,
    max_hold_minutes=90,
    emergency_spread_pips=10.0,
    typical_news_spread_pips=4.0,    # 2.1-2.5 normal, widening at prints (verify)
    exit_llm_interval_seconds=60,
    tp_sanity_band_pips=None,
    rule_be_buffer_pips=2.0,
    rule_trail_pips=15.0,
    commission_cushion_usd_per_lot=None,
    learning_tag="DAX",
    aliases=("DE40", "DAX40", "GER30", "DE30"),
    notes="EA: InpPipSizeOverride=1.0, InpMaxSpreadPips~4, InpEmergencySpreadPips~10, "
          "InpSlippage 300-500 pts. Leverage 1:100 at Purple SC (verify).",
))

# S&P 500 index CFD. 1 pip = 1 index point.
register(InstrumentProfile(
    name="US500",
    asset_class="index",
    pip_size=1.0,
    units_label="index points (1 pip = 1.0 point on US500)",
    price_digits=2,
    quote_currency="USD",
    base_tag="SPX",
    sl_range=(8.0, 60.0),
    tp_range=(6.0, 100.0),
    exit_range=(5, 30),
    lot_max=85,
    max_hold_minutes=None,
    emergency_spread_pips=5.0,
    typical_news_spread_pips=1.5,    # 0.4-0.7 normal (verify widening)
    exit_llm_interval_seconds=None,
    tp_sanity_band_pips=None,
    rule_be_buffer_pips=1.0,
    rule_trail_pips=8.0,
    commission_cushion_usd_per_lot=None,
    learning_tag="SPX",
    aliases=("SPX500", "US500.cash", "SP500"),
    notes="EA: InpPipSizeOverride=1.0. Contract size $1 vs $10/pt UNVERIFIED — read "
          "SYMBOL_TRADE_CONTRACT_SIZE before any live trade.",
))


def profile_for(symbol: Optional[str]) -> Optional[InstrumentProfile]:
    """Profile for a non-forex CFD root, else None (forex / unknown)."""
    if not symbol:
        return None
    return PROFILES.get(normalize_root(symbol))


def profile_value(symbol: Optional[str], field_name: str, default):
    """``profile.<field>`` when the symbol has a profile AND the field is set,
    else ``default`` — the "None means today's behaviour" contract in one call."""
    prof = profile_for(symbol)
    if prof is None:
        return default
    value = getattr(prof, field_name, None)
    return default if value is None else value


def is_forex_root(symbol: Optional[str]) -> bool:
    root = normalize_root(symbol)
    return len(root) == 6 and root[:3] in _FX_CODES and root[3:6] in _FX_CODES


__all__ = [
    "InstrumentProfile", "PROFILES", "register", "profile_for", "profile_value",
    "normalize_root", "is_forex_root",
]
