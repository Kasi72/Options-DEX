"""Fail-closed, deterministic market snapshot quality gates."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from nifty_signal_engine.config.market_hours import (
    MarketPhase,
    market_phase,
    session_date,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

IST = ZoneInfo("Asia/Kolkata")
MAX_SOURCE_AGE = timedelta(seconds=30)


class DataQualityCode(StrEnum):
    """Reasons a snapshot is unsuitable for feature or signal generation."""

    STALE_SOURCE = "STALE_SOURCE"
    PREOPEN = "PREOPEN"
    POST_CLOSE = "POST_CLOSE"
    EMPTY_CHAIN = "EMPTY_CHAIN"
    INCOMPLETE_STRIKES = "INCOMPLETE_STRIKES"
    ZERO_VOLUME = "ZERO_VOLUME"
    INVALID_QUOTE = "INVALID_QUOTE"
    INVALID_IV = "INVALID_IV"
    INVALID_GREEKS = "INVALID_GREEKS"
    EXPIRY_MISMATCH = "EXPIRY_MISMATCH"
    INVALID_SPOT = "INVALID_SPOT"
    CLOCK_SKEW = "CLOCK_SKEW"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    COUNTER_RESET = "COUNTER_RESET"


class DataQualityReport(BaseModel, frozen=True):
    """The complete, non-secret explanation of a tradeability decision."""

    tradable: bool
    codes: tuple[DataQualityCode, ...]
    checked_at: datetime
    details: dict[str, str] = Field(default_factory=dict)


def assess_snapshot(
    current: OptionChainSnapshot,
    previous: OptionChainSnapshot | None,
    now: datetime,
) -> DataQualityReport:
    """Return all observed quality failures; no failure is silently downgraded."""
    checked_at = _as_ist(now, "now")
    source_timestamp = _as_ist(current.source_timestamp, "source_timestamp")
    received_at = _as_ist(current.received_at, "received_at")
    codes: list[DataQualityCode] = []
    details: dict[str, str] = {}

    def add(
        code: DataQualityCode, key: str | None = None, value: str | None = None
    ) -> None:
        if code not in codes:
            codes.append(code)
        if key is not None and value is not None:
            details[key] = value

    age = checked_at - source_timestamp
    if age > MAX_SOURCE_AGE:
        add(
            DataQualityCode.STALE_SOURCE,
            "source_age_seconds",
            str(int(age.total_seconds())),
        )
    if source_timestamp > checked_at or received_at < source_timestamp:
        add(DataQualityCode.CLOCK_SKEW, "clock", "source_timestamp_after_local_clock")

    phase = market_phase(source_timestamp)
    if phase is MarketPhase.PREOPEN:
        add(DataQualityCode.PREOPEN, "session", "preopen")
    elif phase is MarketPhase.POST_CLOSE:
        add(DataQualityCode.POST_CLOSE, "session", "post_close")

    if not math.isfinite(current.spot) or current.spot <= 0:
        add(DataQualityCode.INVALID_SPOT, "spot", "non_positive_or_non_finite")
    if not current.quotes:
        add(DataQualityCode.EMPTY_CHAIN, "coverage", "no_quotes")
    elif _has_incomplete_strikes(current.quotes):
        add(DataQualityCode.INCOMPLETE_STRIKES, "coverage", "missing_call_or_put_side")
    if sum(quote.volume for quote in current.quotes) <= 0:
        add(DataQualityCode.ZERO_VOLUME, "volume", "zero_cumulative_volume")
    if any(not _quote_is_valid(quote) for quote in current.quotes):
        add(DataQualityCode.INVALID_QUOTE, "quote", "missing_or_invalid_bid_ask")
    if any(not _iv_is_valid(quote) for quote in current.quotes):
        add(DataQualityCode.INVALID_IV, "iv", "missing_or_out_of_range")
    if any(not _greeks_are_valid(quote) for quote in current.quotes):
        add(DataQualityCode.INVALID_GREEKS, "greeks", "missing_or_non_finite")
    if any(quote.expiry != current.expiry for quote in current.quotes):
        add(
            DataQualityCode.EXPIRY_MISMATCH,
            "expiry",
            "quote_expiry_differs_from_snapshot",
        )

    if previous is not None and previous.instrument == current.instrument:
        previous_timestamp = _as_ist(
            previous.source_timestamp, "previous.source_timestamp"
        )
        if session_date(previous_timestamp) == session_date(source_timestamp):
            if source_timestamp <= previous_timestamp:
                add(
                    DataQualityCode.OUT_OF_ORDER,
                    "order",
                    "source_timestamp_not_after_previous",
                )
            if _has_counter_reset(current, previous):
                add(
                    DataQualityCode.COUNTER_RESET,
                    "counter_reset",
                    "cumulative_volume_decreased",
                )

    return DataQualityReport(
        tradable=not codes,
        codes=tuple(codes),
        checked_at=checked_at,
        details=details,
    )


def _as_ist(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(IST)


def _has_incomplete_strikes(quotes: tuple[OptionQuote, ...]) -> bool:
    sides: dict[float, set[str]] = {}
    for quote in quotes:
        sides.setdefault(quote.strike, set()).add(quote.option_type)
    return any(option_types != {"CE", "PE"} for option_types in sides.values())


def _quote_is_valid(quote: OptionQuote) -> bool:
    return (
        quote.bid is not None
        and quote.ask is not None
        and math.isfinite(quote.bid)
        and math.isfinite(quote.ask)
        and quote.bid >= 0
        and quote.ask >= quote.bid
    )


def _iv_is_valid(quote: OptionQuote) -> bool:
    return quote.iv is not None and math.isfinite(quote.iv) and 0 < quote.iv <= 1


def _greeks_are_valid(quote: OptionQuote) -> bool:
    return (
        quote.api_delta is not None
        and quote.api_gamma is not None
        and math.isfinite(quote.api_delta)
        and math.isfinite(quote.api_gamma)
        and -1 <= quote.api_delta <= 1
        and quote.api_gamma >= 0
    )


def _has_counter_reset(
    current: OptionChainSnapshot, previous: OptionChainSnapshot
) -> bool:
    prior_volume = {
        (quote.strike, quote.option_type): quote.volume for quote in previous.quotes
    }
    return any(
        quote.volume < prior_volume[quote.strike, quote.option_type]
        for quote in current.quotes
        if (quote.strike, quote.option_type) in prior_volume
    )
