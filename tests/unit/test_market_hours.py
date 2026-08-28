from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nifty_signal_engine.config.market_hours import (
    MarketPhase,
    market_phase,
    session_date,
)

IST = ZoneInfo("Asia/Kolkata")


def test_market_phase_distinguishes_preopen_and_regular() -> None:
    """Moving the opening boundary must not classify 09:10 or 09:16 alike."""
    assert market_phase(datetime(2026, 8, 26, 9, 10, tzinfo=IST)) is MarketPhase.PREOPEN
    assert market_phase(datetime(2026, 8, 26, 9, 16, tzinfo=IST)) is MarketPhase.REGULAR


def test_market_phase_applies_open_and_close_boundaries() -> None:
    """A fencepost error around regular trading hours must be visible."""
    assert market_phase(datetime(2026, 8, 26, 9, 15, tzinfo=IST)) is MarketPhase.REGULAR
    assert market_phase(datetime(2026, 8, 26, 15, 29, 59, tzinfo=IST)) is MarketPhase.REGULAR
    assert market_phase(datetime(2026, 8, 26, 15, 30, tzinfo=IST)) is MarketPhase.POST_CLOSE


def test_session_helpers_reject_naive_timestamps_and_normalize_to_ist() -> None:
    """A collector timestamp without a timezone must not silently select a session."""
    with pytest.raises(ValueError, match="timezone-aware"):
        session_date(datetime.fromisoformat("2026-08-26T09:16:00"))

    utc = datetime(2026, 8, 25, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert session_date(utc).isoformat() == "2026-08-26"
