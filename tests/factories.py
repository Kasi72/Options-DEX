from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

IST = ZoneInfo("Asia/Kolkata")


def aware(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(IST)


def make_quote(
    *,
    timestamp: str,
    strike: float = 24_300,
    option_type: Literal["CE", "PE"] = "CE",
    volume: int = 0,
    oi: int = 100,
    iv: float = 0.15,
    bid: float = 99.0,
    ask: float = 101.0,
) -> OptionQuote:
    return OptionQuote(
        timestamp=aware(timestamp),
        strike=strike,
        option_type=option_type,
        expiry=date(2026, 9, 1),
        volume=volume,
        oi=oi,
        iv=iv,
        bid=bid,
        ask=ask,
    )


def make_chain(
    *,
    timestamp: str,
    call_volume: int = 0,
    put_volume: int = 0,
    spot: float = 24_300,
) -> OptionChainSnapshot:
    timestamp_ist = aware(timestamp)
    return OptionChainSnapshot(
        instrument="NIFTY",
        source_timestamp=timestamp_ist,
        received_at=timestamp_ist,
        spot=spot,
        expiry=date(2026, 9, 1),
        quotes=(
            make_quote(timestamp=timestamp, option_type="CE", volume=call_volume),
            make_quote(timestamp=timestamp, option_type="PE", volume=put_volume),
        ),
    )
