"""
Sentiment Analyzer
Fetches retail sentiment data from multiple free sources
Uses as contrarian indicator (per SkyTower strategy)
"""
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Dict, Optional, List
from dataclasses import dataclass
from loguru import logger
import re


@dataclass
class SentimentData:
    """Retail sentiment data for a currency pair"""
    pair: str
    source: str
    timestamp: datetime
    long_percent: float
    short_percent: float
    net_sentiment: float  # -100 to +100
    signal: str  # CONTRARIAN_LONG, CONTRARIAN_SHORT, NEUTRAL
    confidence: float

    def to_dict(self):
        return {
            "pair": self.pair,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "long_percent": self.long_percent,
            "short_percent": self.short_percent,
            "net_sentiment": self.net_sentiment,
            "signal": self.signal,
            "confidence": self.confidence
        }


class MyfxbookSentiment:
    """Fetches sentiment from Myfxbook Community Outlook"""

    URL = "https://www.myfxbook.com/community/outlook"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def fetch_sentiment(self) -> Dict[str, SentimentData]:
        """Fetch sentiment for all available pairs"""
        results = {}

        try:
            response = requests.get(self.URL, headers=self.HEADERS, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'lxml')

            # Find sentiment rows
            rows = soup.find_all('tr', class_='outlook-row')

            for row in rows:
                try:
                    pair_cell = row.find('td', class_='outlook-symbol')
                    if not pair_cell:
                        continue

                    pair = pair_cell.text.strip().upper()

                    # Get percentages
                    long_cell = row.find('td', class_='outlook-long')
                    short_cell = row.find('td', class_='outlook-short')

                    if long_cell and short_cell:
                        long_pct = self._parse_percent(long_cell.text)
                        short_pct = self._parse_percent(short_cell.text)

                        if long_pct is not None and short_pct is not None:
                            sentiment = self._create_sentiment(pair, long_pct, short_pct, "myfxbook")
                            results[pair] = sentiment

                except Exception as e:
                    logger.debug(f"Error parsing Myfxbook row: {e}")
                    continue

            logger.info(f"Fetched sentiment for {len(results)} pairs from Myfxbook")

        except Exception as e:
            logger.error(f"Error fetching Myfxbook sentiment: {e}")

        return results

    def _parse_percent(self, text: str) -> Optional[float]:
        """Parse percentage from text"""
        try:
            match = re.search(r'([\d.]+)', text.replace('%', ''))
            if match:
                return float(match.group(1))
        except:
            pass
        return None

    def _create_sentiment(self, pair: str, long_pct: float, short_pct: float, source: str) -> SentimentData:
        """Create SentimentData object with contrarian signal"""
        net = long_pct - short_pct

        # Contrarian logic: when retail is heavily positioned one way, trade the opposite
        if long_pct >= 70:
            signal = "CONTRARIAN_SHORT"
            confidence = min((long_pct - 50) / 50, 1.0)
        elif short_pct >= 70:
            signal = "CONTRARIAN_LONG"
            confidence = min((short_pct - 50) / 50, 1.0)
        elif long_pct >= 60:
            signal = "CONTRARIAN_SHORT"
            confidence = (long_pct - 50) / 50 * 0.7
        elif short_pct >= 60:
            signal = "CONTRARIAN_LONG"
            confidence = (short_pct - 50) / 50 * 0.7
        else:
            signal = "NEUTRAL"
            confidence = 0.3

        return SentimentData(
            pair=pair,
            source=source,
            timestamp=datetime.now(),
            long_percent=long_pct,
            short_percent=short_pct,
            net_sentiment=net,
            signal=signal,
            confidence=confidence
        )


class FXSSISentiment:
    """Fetches sentiment from FXSSI Current Ratio"""

    URL = "https://fxssi.com/tools/current-ratio"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def fetch_sentiment(self) -> Dict[str, SentimentData]:
        """Fetch sentiment from FXSSI"""
        results = {}

        try:
            response = requests.get(self.URL, headers=self.HEADERS, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'lxml')

            # Find sentiment containers
            containers = soup.find_all('div', class_='ratio-row')

            for container in containers:
                try:
                    pair_elem = container.find('span', class_='pair-name')
                    if not pair_elem:
                        continue

                    pair = pair_elem.text.strip().upper().replace('/', '')

                    # Find long/short percentages
                    long_elem = container.find('span', class_='long-percent')
                    short_elem = container.find('span', class_='short-percent')

                    if long_elem and short_elem:
                        long_pct = self._parse_percent(long_elem.text)
                        short_pct = self._parse_percent(short_elem.text)

                        if long_pct and short_pct:
                            sentiment = self._create_sentiment(pair, long_pct, short_pct, "fxssi")
                            results[pair] = sentiment

                except Exception as e:
                    logger.debug(f"Error parsing FXSSI row: {e}")
                    continue

            logger.info(f"Fetched sentiment for {len(results)} pairs from FXSSI")

        except Exception as e:
            logger.error(f"Error fetching FXSSI sentiment: {e}")

        return results

    def _parse_percent(self, text: str) -> Optional[float]:
        """Parse percentage from text"""
        try:
            match = re.search(r'([\d.]+)', text)
            if match:
                return float(match.group(1))
        except:
            pass
        return None

    def _create_sentiment(self, pair: str, long_pct: float, short_pct: float, source: str) -> SentimentData:
        """Create SentimentData with contrarian signal"""
        net = long_pct - short_pct

        if long_pct >= 70:
            signal = "CONTRARIAN_SHORT"
            confidence = min((long_pct - 50) / 50, 1.0)
        elif short_pct >= 70:
            signal = "CONTRARIAN_LONG"
            confidence = min((short_pct - 50) / 50, 1.0)
        elif long_pct >= 60:
            signal = "CONTRARIAN_SHORT"
            confidence = (long_pct - 50) / 50 * 0.7
        elif short_pct >= 60:
            signal = "CONTRARIAN_LONG"
            confidence = (short_pct - 50) / 50 * 0.7
        else:
            signal = "NEUTRAL"
            confidence = 0.3

        return SentimentData(
            pair=pair,
            source=source,
            timestamp=datetime.now(),
            long_percent=long_pct,
            short_percent=short_pct,
            net_sentiment=net,
            signal=signal,
            confidence=confidence
        )


class DukascopySentiment:
    """
    Fetches sentiment from Dukascopy SWFX Sentiment Index
    Uses their JSON API
    """

    # Dukascopy provides JSON sentiment data
    BASE_URL = "https://freeserv.dukascopy.com/2.0/index.php"

    def fetch_sentiment(self) -> Dict[str, SentimentData]:
        """Fetch sentiment from Dukascopy"""
        results = {}

        try:
            params = {
                "path": "chart/json3",
                "instrument": "EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD,USD/CAD,NZD/USD",
                "offer_side": "A",
                "interval": "1MIN",
                "splits": "1",
                "limit": "1",
                "type": "sentiment"
            }

            response = requests.get(self.BASE_URL, params=params, timeout=30)

            if response.status_code == 200:
                # Dukascopy response can be complex, try to parse
                try:
                    data = response.json()
                    # Process based on actual response format
                    logger.info(f"Dukascopy response received")
                except:
                    pass

        except Exception as e:
            logger.debug(f"Dukascopy fetch error: {e}")

        return results


class TradingViewTechnical:
    """
    Fetches technical analysis from TradingView Scanner API
    Free, reliable, real-time data
    """

    URL = "https://scanner.tradingview.com/forex/scan"
    PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "AUDNZD", "AUDCAD", "GBPCAD"]

    def fetch_sentiment(self) -> Dict[str, SentimentData]:
        """Fetch technical analysis from TradingView"""
        results = {}

        try:
            # Build tickers list
            tickers = [f"FX:{pair}" for pair in self.PAIRS]

            payload = {
                "symbols": {"tickers": tickers},
                "columns": ["name", "close", "Recommend.All", "Recommend.MA", "Recommend.Other"]
            }

            response = requests.post(self.URL, json=payload, timeout=15)
            response.raise_for_status()

            data = response.json()

            for item in data.get("data", []):
                try:
                    pair = item["d"][0]  # name
                    recommend_all = item["d"][2]  # Overall recommendation (-1 to 1)
                    recommend_ma = item["d"][3]   # Moving Averages
                    recommend_osc = item["d"][4]  # Oscillators

                    if recommend_all is None:
                        continue

                    # Convert recommendation to sentiment-like format
                    # TradingView: -1 = strong sell, 0 = neutral, +1 = strong buy
                    # We invert for contrarian: if TV says buy, retail is probably long

                    # Map to long/short percentages (rough approximation)
                    # recommend_all > 0 means bullish technical = retail likely long
                    long_pct = 50 + (recommend_all * 25)  # Maps -1..1 to 25..75
                    short_pct = 100 - long_pct

                    # Signal based on technical
                    if recommend_all >= 0.3:
                        # Bullish technicals = retail long = contrarian short
                        signal = "CONTRARIAN_SHORT"
                        confidence = min(abs(recommend_all), 1.0) * 0.8
                    elif recommend_all <= -0.3:
                        # Bearish technicals = retail short = contrarian long
                        signal = "CONTRARIAN_LONG"
                        confidence = min(abs(recommend_all), 1.0) * 0.8
                    else:
                        signal = "NEUTRAL"
                        confidence = 0.3

                    results[pair] = SentimentData(
                        pair=pair,
                        source="tradingview",
                        timestamp=datetime.now(),
                        long_percent=round(long_pct, 1),
                        short_percent=round(short_pct, 1),
                        net_sentiment=round(long_pct - short_pct, 1),
                        signal=signal,
                        confidence=round(confidence, 3)
                    )

                except Exception as e:
                    logger.debug(f"Error parsing TradingView item: {e}")
                    continue

            logger.info(f"Fetched technical analysis for {len(results)} pairs from TradingView")

        except Exception as e:
            logger.error(f"Error fetching TradingView data: {e}")

        return results


class SimulatedSentiment:
    """
    Provides simulated sentiment data when live feeds unavailable
    Based on typical retail positioning patterns
    """

    # Typical retail sentiment patterns (retail usually wrong at extremes)
    TYPICAL_SENTIMENT = {
        "EURUSD": {"long": 45, "short": 55},
        "GBPUSD": {"long": 52, "short": 48},
        "USDJPY": {"long": 58, "short": 42},
        "USDCHF": {"long": 48, "short": 52},
        "AUDUSD": {"long": 62, "short": 38},  # Retail often long AUD
        "USDCAD": {"long": 55, "short": 45},
        "NZDUSD": {"long": 65, "short": 35},  # Retail often long NZD
        "AUDNZD": {"long": 50, "short": 50},
        "AUDCAD": {"long": 55, "short": 45},
        "GBPCAD": {"long": 48, "short": 52},
    }

    def fetch_sentiment(self) -> Dict[str, SentimentData]:
        """Return simulated sentiment data"""
        results = {}

        for pair, data in self.TYPICAL_SENTIMENT.items():
            long_pct = data["long"]
            short_pct = data["short"]
            net = long_pct - short_pct

            # Contrarian logic
            if long_pct >= 60:
                signal = "CONTRARIAN_SHORT"
                confidence = (long_pct - 50) / 50 * 0.6  # Lower confidence for simulated
            elif short_pct >= 60:
                signal = "CONTRARIAN_LONG"
                confidence = (short_pct - 50) / 50 * 0.6
            else:
                signal = "NEUTRAL"
                confidence = 0.2

            results[pair] = SentimentData(
                pair=pair,
                source="simulated",
                timestamp=datetime.now(),
                long_percent=long_pct,
                short_percent=short_pct,
                net_sentiment=net,
                signal=signal,
                confidence=confidence
            )

        logger.info(f"Generated simulated sentiment for {len(results)} pairs")
        return results


class SentimentAggregator:
    """
    Aggregates sentiment from multiple sources
    Provides unified contrarian signals
    """

    def __init__(self):
        self.sources = [
            TradingViewTechnical(),  # Primary source - reliable
            MyfxbookSentiment(),
            FXSSISentiment(),
        ]
        # Fallback to simulated data
        self.fallback = SimulatedSentiment()
        self._cache = {}
        self._cache_time = None

    def get_sentiment(self, pair: str) -> Optional[SentimentData]:
        """
        Get aggregated sentiment for a specific pair

        Args:
            pair: Currency pair (e.g., "EURUSD", "NZDUSD")

        Returns:
            Aggregated SentimentData or None
        """
        pair = pair.upper().replace('/', '')

        all_sentiment = self.get_all_sentiment()

        if pair in all_sentiment:
            return all_sentiment[pair]

        return None

    def get_all_sentiment(self) -> Dict[str, SentimentData]:
        """
        Get sentiment for all available pairs (aggregated from all sources)
        """
        # Check cache (5 minute validity)
        from datetime import timedelta
        if (self._cache_time and
                datetime.now() - self._cache_time < timedelta(minutes=5)):
            return self._cache

        aggregated = {}

        for source in self.sources:
            try:
                data = source.fetch_sentiment()
                for pair, sentiment in data.items():
                    pair_normalized = pair.upper().replace('/', '')

                    if pair_normalized not in aggregated:
                        aggregated[pair_normalized] = sentiment
                    else:
                        # Average the values from multiple sources
                        existing = aggregated[pair_normalized]
                        aggregated[pair_normalized] = self._merge_sentiment(existing, sentiment)

            except Exception as e:
                logger.error(f"Error fetching from source: {e}")

        # If no data from live sources, use fallback
        if len(aggregated) == 0:
            logger.warning("No live sentiment data, using simulated data")
            aggregated = self.fallback.fetch_sentiment()

        self._cache = aggregated
        self._cache_time = datetime.now()

        return aggregated

    def _merge_sentiment(self, s1: SentimentData, s2: SentimentData) -> SentimentData:
        """Merge two sentiment readings by averaging"""
        long_avg = (s1.long_percent + s2.long_percent) / 2
        short_avg = (s1.short_percent + s2.short_percent) / 2
        net_avg = long_avg - short_avg

        # Determine signal based on averaged values
        if long_avg >= 70:
            signal = "CONTRARIAN_SHORT"
            confidence = min((long_avg - 50) / 50, 1.0)
        elif short_avg >= 70:
            signal = "CONTRARIAN_LONG"
            confidence = min((short_avg - 50) / 50, 1.0)
        elif long_avg >= 60:
            signal = "CONTRARIAN_SHORT"
            confidence = (long_avg - 50) / 50 * 0.7
        elif short_avg >= 60:
            signal = "CONTRARIAN_LONG"
            confidence = (short_avg - 50) / 50 * 0.7
        else:
            signal = "NEUTRAL"
            confidence = 0.3

        return SentimentData(
            pair=s1.pair,
            source="aggregated",
            timestamp=datetime.now(),
            long_percent=long_avg,
            short_percent=short_avg,
            net_sentiment=net_avg,
            signal=signal,
            confidence=confidence
        )

    def get_currency_sentiment(self, currency: str) -> Dict:
        """
        Get overall sentiment for a currency across all its pairs

        Args:
            currency: Currency code (e.g., "NZD", "CAD")

        Returns:
            Dict with aggregated sentiment analysis
        """
        currency = currency.upper()
        all_sentiment = self.get_all_sentiment()

        matching_pairs = []
        for pair, data in all_sentiment.items():
            if currency in pair:
                matching_pairs.append(data)

        if not matching_pairs:
            return {
                "currency": currency,
                "signal": "NO_DATA",
                "confidence": 0.0,
                "pairs_analyzed": 0
            }

        # Aggregate signals
        bullish_signals = sum(1 for s in matching_pairs if s.signal == "CONTRARIAN_LONG")
        bearish_signals = sum(1 for s in matching_pairs if s.signal == "CONTRARIAN_SHORT")
        avg_confidence = sum(s.confidence for s in matching_pairs) / len(matching_pairs)

        if bullish_signals > bearish_signals:
            signal = "BULLISH"
        elif bearish_signals > bullish_signals:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        return {
            "currency": currency,
            "signal": signal,
            "confidence": avg_confidence,
            "pairs_analyzed": len(matching_pairs),
            "bullish_count": bullish_signals,
            "bearish_count": bearish_signals,
            "pairs": [s.to_dict() for s in matching_pairs]
        }


# =============================================================================
# TESTING
# =============================================================================
if __name__ == "__main__":
    import sys
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    print("Testing Sentiment Aggregator...")
    print("=" * 60)

    aggregator = SentimentAggregator()

    # Get all sentiment
    print("\nFetching sentiment data...")
    all_sentiment = aggregator.get_all_sentiment()

    print(f"\nFound sentiment for {len(all_sentiment)} pairs:")
    print("-" * 60)

    # Show key pairs for SkyTower strategy
    key_pairs = ["NZDUSD", "AUDUSD", "USDCAD", "GBPUSD", "AUDNZD"]

    for pair in key_pairs:
        if pair in all_sentiment:
            s = all_sentiment[pair]
            print(f"{pair}: Long {s.long_percent:.1f}% | Short {s.short_percent:.1f}% | "
                  f"Signal: {s.signal} (conf: {s.confidence:.2f})")
        else:
            print(f"{pair}: No data available")

    # Test currency-level sentiment
    print("\n" + "=" * 60)
    print("Currency-level sentiment analysis:")
    print("-" * 60)

    for currency in ["NZD", "CAD", "AUD", "USD"]:
        result = aggregator.get_currency_sentiment(currency)
        print(f"{currency}: {result['signal']} (confidence: {result['confidence']:.2f}, "
              f"pairs: {result['pairs_analyzed']})")
