"""Sampled-price statistics from contiguous completed minutes only."""

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from nifty_signal_engine.config.market_hours import (
    IST,
    REGULAR_CLOSE,
    REGULAR_OPEN,
    market_phase,
)
from nifty_signal_engine.features.option_structure import FeatureValue


def price_values(
    spots: Sequence[float],
    ends: Sequence[datetime],
    closes: Sequence[float],
    current_end: datetime,
    session_open: float,
    session_open_observed: bool,
    ranges: Sequence[tuple[float, float]],
) -> dict[str, FeatureValue]:
    contiguous = bool(ends and ends[-1] == current_end - timedelta(minutes=1))
    returns = [math.log(b / a) for a, b in pairwise(closes[-20:])]
    # Histories are reset on every gap by the pipeline. Include current completed return.
    if contiguous:
        returns.append(math.log(spots[-1] / closes[-1]))
    volatility = None
    if contiguous and len(returns) >= 2:
        mean = sum(returns) / len(returns)
        volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
    opening_end = datetime.combine(current_end.date(), REGULAR_OPEN, IST) + timedelta(
        minutes=15
    )
    all_ends = [*ends, current_end]
    all_ranges = [*ranges, (max(spots), min(spots))]
    opening_valid = (
        len(all_ends) >= 15
        and all_ends[0] == opening_end - timedelta(minutes=14)
        and all_ends[14] == opening_end
    )
    high = max(item[0] for item in all_ranges[:15]) if opening_valid else None
    low = min(item[1] for item in all_ranges[:15]) if opening_valid else None
    return {
        "spot_open": spots[0],
        "spot_high": max(spots),
        "spot_low": min(spots),
        "spot_close": spots[-1],
        "observed_session_return": spots[-1] / session_open - 1,
        "observed_session_return_status": (
            "SESSION_OPEN_OBSERVED" if session_open_observed else "OBSERVED_WINDOW"
        ),
        "spot_return_1m": spots[-1] / closes[-1] - 1 if contiguous else None,
        "realized_volatility": volatility,
        "opening_range_high": high,
        "opening_range_low": low,
        "opening_range_status": (
            "VALID"
            if opening_valid
            else "WARMUP"
            if current_end < opening_end
            else "INCOMPLETE"
        ),
        "opening_range_breakout": (
            1 if spots[-1] > high else -1 if spots[-1] < low else 0
        )
        if high is not None and low is not None
        else None,
        "vwap_distance": None,
        "vwap_slope": None,
        "vwap_status": "MISSING_INPUT",
        "futures_basis": None,
        "futures_status": "MISSING_INPUT",
        "cross_index_return": None,
        "cross_index_status": "MISSING_INPUT",
    }


def time_values(source_at: datetime, available_at: datetime) -> dict[str, FeatureValue]:
    """Return fixed-session IST times; the current contract has no early closes."""
    source = _as_ist(source_at)
    available = _as_ist(available_at)
    open_at = datetime.combine(source.date(), REGULAR_OPEN, IST)
    close_at = datetime.combine(source.date(), REGULAR_CLOSE, IST)
    return {
        "minutes_since_open": (source - open_at).total_seconds() / 60,
        "minutes_to_close": (close_at - available).total_seconds() / 60,
        "session_time_status": market_phase(source).value,
    }


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
