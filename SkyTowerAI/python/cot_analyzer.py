"""
COT (Commitment of Traders) Data Analyzer
Fetches and analyzes CFTC COT reports for institutional positioning
"""
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass
from loguru import logger
import io


@dataclass
class COTPosition:
    """Represents COT positioning data for a currency"""
    currency: str
    report_date: datetime
    # Commercial (hedgers/banks)
    commercial_long: int
    commercial_short: int
    commercial_net: int
    # Non-Commercial (speculators/funds)
    noncommercial_long: int
    noncommercial_short: int
    noncommercial_net: int
    # Small traders
    nonreportable_long: int
    nonreportable_short: int
    nonreportable_net: int
    # Open Interest
    open_interest: int
    # Calculated metrics
    net_positioning: str  # "BULLISH", "BEARISH", "NEUTRAL"
    positioning_strength: float  # -1.0 to 1.0

    def to_dict(self):
        return {
            "currency": self.currency,
            "report_date": self.report_date.isoformat(),
            "commercial_net": self.commercial_net,
            "noncommercial_net": self.noncommercial_net,
            "net_positioning": self.net_positioning,
            "positioning_strength": self.positioning_strength,
        }


class COTDataFetcher:
    """
    Fetches COT data from CFTC
    Uses the public reporting environment API
    """

    # CFTC futures contracts mapping to currencies
    CURRENCY_CONTRACTS = {
        "USD": "U.S. DOLLAR INDEX",
        "EUR": "EURO FX",
        "GBP": "BRITISH POUND",
        "JPY": "JAPANESE YEN",
        "CHF": "SWISS FRANC",
        "CAD": "CANADIAN DOLLAR",
        "AUD": "AUSTRALIAN DOLLAR",
        "NZD": "NEW ZEALAND DOLLAR",
        "MXN": "MEXICAN PESO",
    }

    # CFTC Socrata API endpoint
    CFTC_API_URL = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"

    def __init__(self):
        self._cache = {}
        self._cache_time = None

    def fetch_cot_data(self, currency: str, weeks_back: int = 52) -> Optional[pd.DataFrame]:
        """
        Fetch COT data for a specific currency

        Args:
            currency: Currency code (e.g., 'CAD', 'AUD')
            weeks_back: How many weeks of historical data

        Returns:
            DataFrame with COT data or None
        """
        if currency.upper() not in self.CURRENCY_CONTRACTS:
            logger.warning(f"Currency {currency} not available in COT data")
            return None

        contract_name = self.CURRENCY_CONTRACTS[currency.upper()]

        try:
            # Calculate date range
            end_date = datetime.now()
            start_date = end_date - timedelta(weeks=weeks_back)

            # Build query. PREFIX match, not '%name%': a substring match for
            # 'BRITISH POUND' also returns the 'EURO FX/BRITISH POUND XRATE'
            # cross contract, interleaving sign-inverted rows into the series
            # (weekly change then compared main-vs-cross of the SAME week).
            params = {
                "$where": f"market_and_exchange_names like '{contract_name} - %' "
                          f"AND report_date_as_yyyy_mm_dd >= '{start_date.strftime('%Y-%m-%d')}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": weeks_back,
            }

            response = requests.get(self.CFTC_API_URL, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            if not data:
                logger.warning(f"No COT data found for {currency}")
                return None

            df = pd.DataFrame(data)

            # Defensive post-filter in case the API-side LIKE ever loosens:
            # keep only rows of the exact contract ('NAME - EXCHANGE').
            if 'market_and_exchange_names' in df.columns:
                df = df[df['market_and_exchange_names'].str.startswith(
                    contract_name + ' -')]
                if df.empty:
                    logger.warning(f"No exact-contract COT rows for {currency}")
                    return None

            # Convert numeric columns
            numeric_cols = [
                'noncomm_positions_long_all',
                'noncomm_positions_short_all',
                'comm_positions_long_all',
                'comm_positions_short_all',
                'nonrept_positions_long_all',
                'nonrept_positions_short_all',
                'open_interest_all',
            ]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            df['report_date'] = pd.to_datetime(df['report_date_as_yyyy_mm_dd'])

            logger.info(f"Fetched {len(df)} weeks of COT data for {currency}")
            return df

        except Exception as e:
            logger.error(f"Error fetching COT data for {currency}: {e}")
            return None

    def get_latest_positioning(self, currency: str) -> Optional[COTPosition]:
        """
        Get the latest COT positioning for a currency

        Returns:
            COTPosition object with current positioning
        """
        df = self.fetch_cot_data(currency, weeks_back=10)
        if df is None or df.empty:
            return None

        # Get latest row
        latest = df.iloc[0]

        try:
            # Extract values
            noncomm_long = int(latest.get('noncomm_positions_long_all', 0) or 0)
            noncomm_short = int(latest.get('noncomm_positions_short_all', 0) or 0)
            comm_long = int(latest.get('comm_positions_long_all', 0) or 0)
            comm_short = int(latest.get('comm_positions_short_all', 0) or 0)
            nonrept_long = int(latest.get('nonrept_positions_long_all', 0) or 0)
            nonrept_short = int(latest.get('nonrept_positions_short_all', 0) or 0)
            open_interest = int(latest.get('open_interest_all', 0) or 0)

            # Calculate nets
            noncomm_net = noncomm_long - noncomm_short
            comm_net = comm_long - comm_short
            nonrept_net = nonrept_long - nonrept_short

            # Determine positioning
            # Non-commercials (speculators) are the key signal
            if noncomm_net > 0:
                positioning = "BULLISH"
            elif noncomm_net < 0:
                positioning = "BEARISH"
            else:
                positioning = "NEUTRAL"

            # Calculate positioning strength (-1 to 1)
            # Based on historical percentile
            strength = self._calculate_strength(df, noncomm_net)

            return COTPosition(
                currency=currency,
                report_date=pd.to_datetime(latest['report_date']),
                commercial_long=comm_long,
                commercial_short=comm_short,
                commercial_net=comm_net,
                noncommercial_long=noncomm_long,
                noncommercial_short=noncomm_short,
                noncommercial_net=noncomm_net,
                nonreportable_long=nonrept_long,
                nonreportable_short=nonrept_short,
                nonreportable_net=nonrept_net,
                open_interest=open_interest,
                net_positioning=positioning,
                positioning_strength=strength,
            )

        except Exception as e:
            logger.error(f"Error parsing COT data for {currency}: {e}")
            return None

    def _calculate_strength(self, df: pd.DataFrame, current_net: int) -> float:
        """
        Calculate positioning strength based on historical percentile

        Returns value from -1.0 (extreme bearish) to 1.0 (extreme bullish)
        """
        try:
            # Calculate historical non-commercial net positions
            df['net'] = (df['noncomm_positions_long_all'].astype(float) -
                         df['noncomm_positions_short_all'].astype(float))

            min_net = df['net'].min()
            max_net = df['net'].max()

            if max_net == min_net:
                return 0.0

            # Normalize to -1 to 1 range
            strength = 2 * (current_net - min_net) / (max_net - min_net) - 1

            return round(strength, 3)

        except Exception as e:
            logger.debug(f"Error calculating strength: {e}")
            return 0.0

    def get_positioning_change(self, currency: str) -> Dict:
        """
        Get week-over-week change in positioning

        Returns:
            Dict with change metrics
        """
        df = self.fetch_cot_data(currency, weeks_back=5)
        if df is None or len(df) < 2:
            return {"error": "Insufficient data"}

        current = df.iloc[0]
        previous = df.iloc[1]

        try:
            current_net = (float(current.get('noncomm_positions_long_all', 0) or 0) -
                           float(current.get('noncomm_positions_short_all', 0) or 0))
            previous_net = (float(previous.get('noncomm_positions_long_all', 0) or 0) -
                            float(previous.get('noncomm_positions_short_all', 0) or 0))

            change = current_net - previous_net
            change_pct = (change / abs(previous_net) * 100) if previous_net != 0 else 0

            return {
                "currency": currency,
                "current_net": current_net,
                "previous_net": previous_net,
                "change": change,
                "change_percent": round(change_pct, 2),
                "direction": "INCREASING_LONGS" if change > 0 else "INCREASING_SHORTS" if change < 0 else "UNCHANGED"
            }

        except Exception as e:
            logger.error(f"Error calculating change for {currency}: {e}")
            return {"error": str(e)}


class COTAnalyzer:
    """
    Analyzes COT data for trading signals
    """

    def __init__(self):
        self.fetcher = COTDataFetcher()

    def analyze_currency(self, currency: str) -> Dict:
        """
        Complete COT analysis for a currency

        Returns:
            Dict with analysis results
        """
        position = self.fetcher.get_latest_positioning(currency)
        change = self.fetcher.get_positioning_change(currency)

        if position is None:
            return {
                "currency": currency,
                "error": "Could not fetch COT data",
                "signal": "NEUTRAL",
                "confidence": 0.0
            }

        # Analyze signal
        signal = self._determine_signal(position, change)

        return {
            "currency": currency,
            "report_date": position.report_date.isoformat(),
            "positioning": position.net_positioning,
            "strength": position.positioning_strength,
            "noncommercial_net": position.noncommercial_net,
            "commercial_net": position.commercial_net,
            "weekly_change": change.get("change", 0),
            "weekly_change_direction": change.get("direction", "UNKNOWN"),
            "signal": signal["signal"],
            "confidence": signal["confidence"],
            "reasoning": signal["reasoning"]
        }

    def _determine_signal(self, position: COTPosition, change: Dict) -> Dict:
        """
        Determine trading signal based on COT data

        Uses institutional positioning logic:
        - Follow non-commercial (speculator) positions - they move markets
        - When retail/small traders are extreme, fade them
        - Strength indicates how extreme the position is vs history
        """
        signal = "NEUTRAL"
        confidence = 0.5
        reasons = []

        # Check institutional positioning based on ACTUAL net position
        # noncommercial_net > 0 = institutions are LONG
        # noncommercial_net < 0 = institutions are SHORT
        net = position.noncommercial_net
        strength_abs = abs(position.positioning_strength)

        if net > 0 and strength_abs > 0.5:
            # Institutions are LONG and position is significant
            signal = "BULLISH"
            confidence += 0.15 if strength_abs > 0.7 else 0.1
            reasons.append(f"Institutions net LONG ({net:,}), strength: {position.positioning_strength:.2f}")
        elif net < 0 and strength_abs > 0.5:
            # Institutions are SHORT and position is significant
            signal = "BEARISH"
            confidence += 0.15 if strength_abs > 0.7 else 0.1
            reasons.append(f"Institutions net SHORT ({net:,}), strength: {position.positioning_strength:.2f}")

        # Check momentum (weekly change)
        if change.get("direction") == "INCREASING_LONGS":
            if signal != "BEARISH":
                signal = "BULLISH"
                confidence += 0.1
            reasons.append("Institutions adding longs week-over-week")
        elif change.get("direction") == "INCREASING_SHORTS":
            if signal != "BULLISH":
                signal = "BEARISH"
                confidence += 0.1
            reasons.append("Institutions adding shorts week-over-week")

        # Contrarian signal from retail (non-reportable)
        retail_ratio = position.nonreportable_net / max(position.open_interest, 1)
        if retail_ratio > 0.1:  # Retail very long
            # Be cautious about going long
            if signal == "BULLISH":
                confidence -= 0.1
            reasons.append("Retail traders heavily long (contrarian bearish)")
        elif retail_ratio < -0.1:  # Retail very short
            if signal == "BEARISH":
                confidence -= 0.1
            reasons.append("Retail traders heavily short (contrarian bullish)")

        return {
            "signal": signal,
            "confidence": min(max(confidence, 0.0), 1.0),
            "reasoning": "; ".join(reasons) if reasons else "No strong signals"
        }

# =============================================================================
# TESTING
# =============================================================================
if __name__ == "__main__":
    from loguru import logger
    import sys

    logger.remove()
    logger.add(sys.stdout, level="INFO")

    print("Testing COT Analyzer...")
    print("=" * 60)

    analyzer = COTAnalyzer()

    # Analyze key currencies for SkyTower strategy
    currencies = ["CAD", "AUD", "NZD", "USD", "GBP"]

    for currency in currencies:
        print(f"\n{currency} COT Analysis:")
        print("-" * 40)

        result = analyzer.analyze_currency(currency)

        if "error" in result and result.get("error"):
            print(f"  Error: {result['error']}")
        else:
            print(f"  Report Date: {result.get('report_date', 'N/A')}")
            print(f"  Positioning: {result.get('positioning', 'N/A')}")
            print(f"  Strength: {result.get('strength', 0):.2f}")
            print(f"  Weekly Change: {result.get('weekly_change_direction', 'N/A')}")
            print(f"  Signal: {result.get('signal', 'N/A')} (confidence: {result.get('confidence', 0):.2f})")
            print(f"  Reasoning: {result.get('reasoning', 'N/A')}")
