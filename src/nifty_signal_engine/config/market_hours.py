"""IST trading-session helpers."""

from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
REGULAR_OPEN = time(9, 15)
REGULAR_CLOSE = time(15, 30)


class MarketPhase(StrEnum):
    PREOPEN = "PREOPEN"
    REGULAR = "REGULAR"
    POST_CLOSE = "POST_CLOSE"


def _as_ist(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(IST)


def session_date(timestamp: datetime) -> date:
    """Return the IST calendar date for an aware timestamp."""
    return _as_ist(timestamp).date()


def market_phase(timestamp: datetime) -> MarketPhase:
    """Classify an aware timestamp using the regular NSE session boundaries."""
    session_time = _as_ist(timestamp).time()
    if session_time < REGULAR_OPEN:
        return MarketPhase.PREOPEN
    if session_time < REGULAR_CLOSE:
        return MarketPhase.REGULAR
    return MarketPhase.POST_CLOSE
