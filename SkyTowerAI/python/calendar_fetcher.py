"""
Economic Calendar Data Fetcher
Fetches upcoming economic events from multiple free sources
"""
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd
import json
import time
from loguru import logger
from dataclasses import dataclass, asdict
import pytz
import re


@dataclass
class EconomicEvent:
    """Represents an economic calendar event"""
    datetime_utc: datetime
    currency: str
    event_name: str
    impact: str  # HIGH, MEDIUM, LOW
    forecast: Optional[str]
    previous: Optional[str]
    actual: Optional[str] = None
    source: str = ""

    def to_dict(self):
        d = asdict(self)
        d['datetime_utc'] = self.datetime_utc.isoformat()
        return d

    @property
    def is_high_impact(self) -> bool:
        return self.impact.upper() == "HIGH"


class TradingEconomicsCalendar:
    """
    Fetches calendar from Trading Economics (no API key needed for basic data)
    Uses their public JSON endpoint
    """

    BASE_URL = "https://tradingeconomics.com/calendar"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tradingeconomics.com/calendar",
    }

    # Currency code mapping
    COUNTRY_TO_CURRENCY = {
        "United States": "USD",
        "Euro Area": "EUR",
        "United Kingdom": "GBP",
        "Japan": "JPY",
        "Canada": "CAD",
        "Australia": "AUD",
        "New Zealand": "NZD",
        "Switzerland": "CHF",
        "Sweden": "SEK",
        "Norway": "NOK",
    }

    def fetch_events(self, days_ahead: int = 7) -> List[EconomicEvent]:
        """Fetch economic events"""
        events = []

        try:
            # Use the JSON API endpoint
            today = datetime.now()
            end_date = today + timedelta(days=days_ahead)

            url = f"https://tradingeconomics.com/calendar?f=json"

            response = requests.get(url, headers=self.HEADERS, timeout=30)

            if response.status_code != 200:
                logger.warning(f"TradingEconomics returned {response.status_code}")
                return events

            # Try to parse as JSON
            try:
                data = response.json()
            except:
                logger.warning("Could not parse TradingEconomics response as JSON")
                return events

            for item in data:
                try:
                    # Parse date
                    date_str = item.get('Date', '')
                    if not date_str:
                        continue

                    event_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))

                    # Get currency from country
                    country = item.get('Country', '')
                    currency = self.COUNTRY_TO_CURRENCY.get(country, '')
                    if not currency:
                        continue

                    # Determine impact
                    importance = item.get('Importance', 1)
                    impact = "HIGH" if importance >= 3 else "MEDIUM" if importance == 2 else "LOW"

                    events.append(EconomicEvent(
                        datetime_utc=event_dt,
                        currency=currency,
                        event_name=item.get('Event', ''),
                        impact=impact,
                        forecast=str(item.get('Forecast', '')) if item.get('Forecast') else None,
                        previous=str(item.get('Previous', '')) if item.get('Previous') else None,
                        actual=str(item.get('Actual', '')) if item.get('Actual') else None,
                        source="tradingeconomics"
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing TE event: {e}")
                    continue

            logger.info(f"Fetched {len(events)} events from TradingEconomics")

        except Exception as e:
            logger.error(f"Error fetching TradingEconomics: {e}")

        return events


class ForexFactoryCalendar:
    """
    Fetches calendar from ForexFactory
    Uses their XML feed
    """

    # ForexFactory provides weekly calendar XML
    BASE_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

    def fetch_events(self, days_ahead: int = 7) -> List[EconomicEvent]:
        """Fetch events from ForexFactory XML feed"""
        events = []

        try:
            response = requests.get(self.BASE_URL, timeout=30)
            response.raise_for_status()

            # Parse XML
            from xml.etree import ElementTree as ET
            root = ET.fromstring(response.content)

            for event in root.findall('.//event'):
                try:
                    # Get date and time
                    date_str = event.find('date').text if event.find('date') is not None else ''
                    time_str = event.find('time').text if event.find('time') is not None else ''

                    if not date_str:
                        continue

                    # Parse datetime
                    if time_str and time_str != 'All Day':
                        dt_str = f"{date_str} {time_str}"
                        event_dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                    else:
                        event_dt = datetime.strptime(date_str, "%m-%d-%Y")

                    # Make timezone aware (ForexFactory uses ET)
                    eastern = pytz.timezone('US/Eastern')
                    event_dt = eastern.localize(event_dt).astimezone(pytz.UTC)

                    # Get other fields
                    currency = event.find('country').text if event.find('country') is not None else ''
                    title = event.find('title').text if event.find('title') is not None else ''
                    impact_str = event.find('impact').text if event.find('impact') is not None else ''
                    forecast = event.find('forecast').text if event.find('forecast') is not None else None
                    previous = event.find('previous').text if event.find('previous') is not None else None
                    actual = event.find('actual').text if event.find('actual') is not None else None

                    # Map impact
                    impact_map = {'High': 'HIGH', 'Medium': 'MEDIUM', 'Low': 'LOW', 'Holiday': 'LOW'}
                    impact = impact_map.get(impact_str, 'LOW')

                    events.append(EconomicEvent(
                        datetime_utc=event_dt,
                        currency=currency,
                        event_name=title,
                        impact=impact,
                        forecast=forecast,
                        previous=previous,
                        actual=actual,
                        source="forexfactory"
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing FF event: {e}")
                    continue

            logger.info(f"Fetched {len(events)} events from ForexFactory")

        except Exception as e:
            logger.error(f"Error fetching ForexFactory: {e}")

        return events


class FinnhubCalendar:
    """Fetcher for Finnhub economic calendar (requires free API key)"""

    BASE_URL = "https://finnhub.io/api/v1/calendar/economic"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def fetch_events(self, days_ahead: int = 7) -> List[EconomicEvent]:
        """Fetch events from Finnhub API"""
        events = []

        if not self.api_key:
            logger.debug("Finnhub API key not configured")
            return events

        try:
            today = datetime.now()
            end_date = today + timedelta(days=days_ahead)

            params = {
                "from": today.strftime("%Y-%m-%d"),
                "to": end_date.strftime("%Y-%m-%d"),
                "token": self.api_key,
            }

            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()

            for item in data.get('economicCalendar', []):
                try:
                    event_dt = datetime.fromisoformat(item['time'].replace('Z', '+00:00'))

                    # Map Finnhub impact to our format
                    impact_map = {3: "HIGH", 2: "MEDIUM", 1: "LOW"}
                    impact = impact_map.get(item.get('impact', 1), "LOW")

                    # Map country to currency
                    country_map = {
                        'US': 'USD', 'EU': 'EUR', 'GB': 'GBP', 'JP': 'JPY',
                        'CA': 'CAD', 'AU': 'AUD', 'NZ': 'NZD', 'CH': 'CHF',
                        'SE': 'SEK', 'NO': 'NOK'
                    }
                    currency = country_map.get(item.get('country', ''), item.get('country', ''))

                    events.append(EconomicEvent(
                        datetime_utc=event_dt,
                        currency=currency,
                        event_name=item.get('event', ''),
                        impact=impact,
                        forecast=str(item.get('estimate')) if item.get('estimate') else None,
                        previous=str(item.get('prev')) if item.get('prev') else None,
                        actual=str(item.get('actual')) if item.get('actual') else None,
                        source="finnhub"
                    ))
                except Exception as e:
                    logger.debug(f"Finnhub parse error: {e}")
                    continue

            logger.info(f"Fetched {len(events)} events from Finnhub")

        except Exception as e:
            logger.error(f"Error fetching Finnhub calendar: {e}")

        return events


class StaticCalendarData:
    """
    Fallback: Returns known recurring high-impact events
    Useful when APIs are unavailable
    """

    # Major recurring events (approximate schedule)
    RECURRING_EVENTS = {
        "USD": [
            {"name": "Non-Farm Payrolls", "day_of_month": [1, 7], "weekday": 4, "time": "13:30"},
            {"name": "CPI m/m", "day_of_month": [10, 15], "time": "13:30"},
            {"name": "Interest Rate Decision", "day_of_month": [1, 15], "time": "19:00"},
            {"name": "Retail Sales m/m", "day_of_month": [14, 18], "time": "13:30"},
            {"name": "GDP q/q", "day_of_month": [25, 30], "time": "13:30"},
        ],
        "CAD": [
            {"name": "Interest Rate Decision", "day_of_month": [1, 15], "time": "15:00"},
            {"name": "Employment Change", "day_of_month": [5, 10], "weekday": 4, "time": "13:30"},
            {"name": "CPI m/m", "day_of_month": [15, 20], "time": "13:30"},
            {"name": "Retail Sales m/m", "day_of_month": [20, 25], "time": "13:30"},
            {"name": "GDP m/m", "day_of_month": [28, 31], "time": "13:30"},
        ],
        "AUD": [
            {"name": "Interest Rate Decision", "day_of_month": [1, 7], "time": "03:30"},
            {"name": "Employment Change", "day_of_month": [15, 20], "time": "00:30"},
            {"name": "CPI q/q", "day_of_month": [25, 28], "time": "00:30"},
            {"name": "Retail Sales m/m", "day_of_month": [1, 5], "time": "00:30"},
            {"name": "GDP q/q", "day_of_month": [1, 7], "time": "00:30"},
        ],
        "NZD": [
            {"name": "Interest Rate Decision", "day_of_month": [1, 15], "time": "01:00"},
            {"name": "Employment Change q/q", "day_of_month": [1, 7], "time": "21:45"},
            {"name": "CPI q/q", "day_of_month": [15, 20], "time": "21:45"},
            {"name": "Retail Sales q/q", "day_of_month": [20, 25], "time": "21:45"},
            {"name": "GDP q/q", "day_of_month": [15, 20], "time": "21:45"},
        ],
        "GBP": [
            {"name": "Interest Rate Decision", "day_of_month": [1, 7], "time": "12:00"},
            {"name": "CPI y/y", "day_of_month": [15, 20], "time": "07:00"},
            {"name": "Employment Change", "day_of_month": [10, 15], "time": "07:00"},
            {"name": "Retail Sales m/m", "day_of_month": [20, 25], "time": "07:00"},
            {"name": "GDP m/m", "day_of_month": [10, 15], "time": "07:00"},
        ],
    }

    def fetch_events(self, days_ahead: int = 30) -> List[EconomicEvent]:
        """Generate events based on typical schedule"""
        events = []
        today = datetime.now(pytz.UTC)

        for currency, event_list in self.RECURRING_EVENTS.items():
            for event_info in event_list:
                # Find next occurrence in the time window
                for day_offset in range(days_ahead):
                    check_date = today + timedelta(days=day_offset)
                    day_of_month = check_date.day

                    # Check if this day matches the event schedule
                    if day_of_month in event_info.get('day_of_month', []):
                        # Check weekday constraint if present
                        if 'weekday' in event_info and check_date.weekday() != event_info['weekday']:
                            continue

                        # Create event datetime
                        time_parts = event_info['time'].split(':')
                        event_dt = check_date.replace(
                            hour=int(time_parts[0]),
                            minute=int(time_parts[1]),
                            second=0,
                            microsecond=0
                        )

                        if event_dt > today:
                            events.append(EconomicEvent(
                                datetime_utc=event_dt,
                                currency=currency,
                                event_name=event_info['name'],
                                impact="HIGH",
                                forecast=None,
                                previous=None,
                                source="static"
                            ))
                            break  # Only add first occurrence

        logger.info(f"Generated {len(events)} events from static calendar")
        return events


class CalendarAggregator:
    """
    Aggregates economic calendar data from multiple sources
    and provides a unified interface
    """

    def __init__(self, finnhub_api_key: str = ""):
        self.sources = [
            ForexFactoryCalendar(),  # Most reliable free source
            TradingEconomicsCalendar(),
        ]
        if finnhub_api_key:
            self.sources.append(FinnhubCalendar(finnhub_api_key))

        # Fallback to static data if no events found
        self.static_calendar = StaticCalendarData()

        self._cache = {}
        self._cache_time = None
        self._cache_duration = timedelta(minutes=15)

    def get_upcoming_events(
            self,
            currencies: List[str] = None,
            impact_filter: str = "HIGH",
            hours_ahead: int = 24
    ) -> List[EconomicEvent]:
        """
        Get upcoming high-impact events

        Args:
            currencies: Filter by currency codes (e.g., ['NZD', 'CAD'])
            impact_filter: Minimum impact level ('HIGH', 'MEDIUM', 'LOW')
            hours_ahead: How many hours ahead to look

        Returns:
            List of EconomicEvent sorted by datetime
        """
        # Check cache
        cache_key = f"{currencies}_{impact_filter}_{hours_ahead}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        all_events = []

        # Fetch from all sources
        for source in self.sources:
            try:
                events = source.fetch_events(days_ahead=max(7, hours_ahead // 24 + 1))
                all_events.extend(events)
                time.sleep(0.5)  # Rate limiting
            except Exception as e:
                logger.error(f"Error fetching from {source.__class__.__name__}: {e}")

        # Check how many future events we have
        now = datetime.now(pytz.UTC)
        future_events = [e for e in all_events if e.datetime_utc > now]

        # If no future events found, supplement with static calendar
        if len(future_events) == 0:
            logger.warning("No future events from APIs, supplementing with static calendar")
            static_events = self.static_calendar.fetch_events(days_ahead=max(30, hours_ahead // 24 + 1))
            all_events.extend(static_events)

        # Deduplicate by datetime + currency + event name
        unique_events = self._deduplicate(all_events)

        # Filter by time window
        now = datetime.now(pytz.UTC)
        cutoff = now + timedelta(hours=hours_ahead)
        filtered = [e for e in unique_events if now <= e.datetime_utc <= cutoff]

        # Filter by impact
        impact_levels = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        min_impact = impact_levels.get(impact_filter.upper(), 1)
        filtered = [
            e for e in filtered
            if impact_levels.get(e.impact, 1) >= min_impact
        ]

        # Filter by currencies
        if currencies:
            currencies_upper = [c.upper() for c in currencies]
            filtered = [e for e in filtered if e.currency.upper() in currencies_upper]

        # Sort by datetime
        filtered.sort(key=lambda x: x.datetime_utc)

        # Cache results
        self._cache[cache_key] = filtered
        self._cache_time = datetime.now()

        return filtered

    def get_next_tradeable_event(
            self,
            event_keywords: List[str] = None,
            currencies: List[str] = None
    ) -> Optional[EconomicEvent]:
        """
        Get the next event that matches SkyTower-FX criteria.
        BUG-7 FIX: Post-filters cached results to exclude passed events.

        Args:
            event_keywords: Keywords to match in event name
            currencies: Currencies to consider

        Returns:
            Next matching event or None
        """
        from config import HIGH_IMPACT_EVENTS, CURRENCY_PAIRS

        if event_keywords is None:
            event_keywords = HIGH_IMPACT_EVENTS
        if currencies is None:
            currencies = list(CURRENCY_PAIRS.keys())

        events = self.get_upcoming_events(
            currencies=currencies,
            impact_filter="MEDIUM",
            hours_ahead=168  # 1 week
        )

        # BUG-7 FIX: Post-filter passed events from cache
        now = datetime.now(pytz.UTC)
        for event in events:
            # Skip events that already passed (cache may be stale)
            if event.datetime_utc < now:
                continue
            # Check if event matches any keyword
            event_lower = event.event_name.lower()
            for keyword in event_keywords:
                if keyword.lower() in event_lower:
                    return event

        return None

    def get_tradeable_events(
            self,
            event_keywords: List[str] = None,
            currencies: List[str] = None
    ) -> List[EconomicEvent]:
        """
        Get ALL upcoming events matching SkyTower-FX criteria.
        Unlike get_next_tradeable_event(), returns the full list.
        Post-filters cached results to exclude passed events.

        Args:
            event_keywords: Keywords to match in event name
            currencies: Currencies to consider

        Returns:
            List of matching future events sorted by time
        """
        from config import HIGH_IMPACT_EVENTS, CURRENCY_PAIRS

        if event_keywords is None:
            event_keywords = HIGH_IMPACT_EVENTS
        if currencies is None:
            currencies = list(CURRENCY_PAIRS.keys())

        events = self.get_upcoming_events(
            currencies=currencies,
            impact_filter="MEDIUM",
            hours_ahead=168  # 1 week
        )

        now = datetime.now(pytz.UTC)
        result = []
        for event in events:
            if event.datetime_utc < now:
                continue
            event_lower = event.event_name.lower()
            if any(kw.lower() in event_lower for kw in event_keywords):
                result.append(event)
        return result

    def _deduplicate(self, events: List[EconomicEvent]) -> List[EconomicEvent]:
        """Remove duplicate events from multiple sources"""
        seen = set()
        unique = []

        for event in events:
            # Create a key based on rounded time + currency + event name start
            time_key = event.datetime_utc.strftime("%Y-%m-%d %H:%M")
            name_key = event.event_name[:30].lower()
            key = (time_key, event.currency.upper(), name_key)

            if key not in seen:
                seen.add(key)
                unique.append(event)

        return unique

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cache is still valid"""
        if cache_key not in self._cache:
            return False
        if self._cache_time is None:
            return False
        return datetime.now() - self._cache_time < self._cache_duration


# =============================================================================
# TESTING
# =============================================================================
if __name__ == "__main__":
    from loguru import logger
    import sys

    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    print("Testing Calendar Aggregator...")
    print("=" * 60)

    aggregator = CalendarAggregator()

    # Get high impact events for key currencies
    events = aggregator.get_upcoming_events(
        currencies=["NZD", "CAD", "AUD", "USD"],
        impact_filter="HIGH",
        hours_ahead=168  # 1 week
    )

    print(f"\nFound {len(events)} high-impact events in next 7 days:")
    print("-" * 60)

    for event in events[:10]:  # Show first 10
        print(f"{event.datetime_utc.strftime('%Y-%m-%d %H:%M')} UTC | "
              f"{event.currency:4} | {event.impact:6} | {event.event_name[:40]}")
        if event.forecast:
            print(f"    Forecast: {event.forecast} | Previous: {event.previous}")

    # Get next tradeable event
    print("\n" + "=" * 60)
    print("Next Tradeable Event (SkyTower-FX criteria):")
    next_event = aggregator.get_next_tradeable_event()
    if next_event:
        print(f"  {next_event.datetime_utc} - {next_event.currency} - {next_event.event_name}")
    else:
        print("  No matching events found")
