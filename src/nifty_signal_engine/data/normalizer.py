"""Strict conversion from untrusted Dhan bytes to immutable domain models."""

import json
import math
from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.dhan_client import BrokerPayloadError, RawSnapshot
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

Instrument = Literal["NIFTY", "BANKNIFTY"]
IST = ZoneInfo("Asia/Kolkata")
_SIDES: tuple[tuple[str, Literal["CE", "PE"]], ...] = (("ce", "CE"), ("pe", "PE"))


def normalize_option_chain(
    raw: RawSnapshot, instrument: Instrument, received_at: datetime
) -> OptionChainSnapshot:
    """Normalize one response only when every present contract is unambiguous."""
    _require_aware_timestamp(received_at, "received_at")
    _require_aware_timestamp(raw.captured_at, "captured_at")
    if received_at < raw.captured_at:
        raise BrokerPayloadError("received_at cannot precede captured_at")
    received_at_ist = received_at.astimezone(IST)
    if raw.expiry is None:
        raise BrokerPayloadError("raw option chain must retain its requested expiry")
    try:
        payload = json.loads(raw.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerPayloadError("raw option chain is not valid JSON") from error
    if not isinstance(payload, dict):
        raise BrokerPayloadError("option chain root must be an object")
    data = _object(payload.get("data"), "data")
    source_timestamp, source_time_authoritative = _source_provenance(raw, data)
    spot = _number(data.get("last_price"), "data.last_price", positive=True)
    strikes = _object(data.get("oc"), "data.oc")
    if not strikes:
        raise BrokerPayloadError("option chain has no strikes")

    quotes: list[OptionQuote] = []
    contract_identities: set[tuple[float, Literal["CE", "PE"]]] = set()
    for raw_strike, raw_sides in strikes.items():
        strike = _strike(raw_strike)
        sides = _object(raw_sides, f"strike {raw_strike}")
        present = 0
        for key, option_type in _SIDES:
            if key in sides:
                present += 1
                identity = (strike, option_type)
                if identity in contract_identities:
                    raise BrokerPayloadError("duplicate option contract")
                contract_identities.add(identity)
                quotes.append(
                    _quote(
                        _object(sides[key], f"{raw_strike}.{key}"),
                        strike,
                        option_type,
                        raw,
                        source_timestamp,
                    )
                )
        if present == 0:
            raise BrokerPayloadError(f"strike {raw_strike} has no option side")
    return OptionChainSnapshot(
        instrument=instrument,
        source_timestamp=source_timestamp,
        received_at=received_at_ist,
        spot=spot,
        expiry=raw.expiry,
        quotes=tuple(quotes),
        source_time_authoritative=source_time_authoritative,
    )


def _source_provenance(
    raw: RawSnapshot, data: dict[str, object]
) -> tuple[datetime, bool]:
    if raw.source_time_authoritative:
        if raw.broker_source_timestamp is None:
            raise BrokerPayloadError("raw source-time provenance is inconsistent")
        return raw.broker_source_timestamp.astimezone(IST), True
    value = data.get("timestamp")
    if value is None:
        return raw.captured_at.astimezone(IST), False
    if not isinstance(value, str):
        raise BrokerPayloadError("data.timestamp must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BrokerPayloadError(
            "data.timestamp must be an ISO-8601 timestamp"
        ) from error
    _require_aware_timestamp(timestamp, "data.timestamp")
    return timestamp.astimezone(IST), True


def _quote(
    side: dict[str, object],
    strike: float,
    option_type: Literal["CE", "PE"],
    raw: RawSnapshot,
    timestamp: datetime,
) -> OptionQuote:
    bid = _number(side.get("top_bid_price"), "top_bid_price", nonnegative=True)
    ask = _number(side.get("top_ask_price"), "top_ask_price", nonnegative=True)
    if bid > ask:
        raise BrokerPayloadError("bid cannot exceed ask")
    greeks = _object(side.get("greeks"), "greeks")
    iv_percent = _number(
        side.get("implied_volatility"), "implied_volatility", positive=True
    )
    if iv_percent > 100:
        raise BrokerPayloadError("implied_volatility must be a percentage in (0, 100]")
    return OptionQuote(
        timestamp=timestamp,
        strike=strike,
        option_type=option_type,
        expiry=raw.expiry,
        ltp=_number(side.get("last_price"), "last_price", nonnegative=True),
        bid=bid,
        ask=ask,
        volume=_integer(side.get("volume"), "volume", nonnegative=True),
        oi=_integer(side.get("oi"), "oi", nonnegative=True),
        previous_oi=_integer(side.get("previous_oi"), "previous_oi", nonnegative=True),
        iv=iv_percent / 100,
        api_delta=_number(greeks.get("delta"), "greeks.delta"),
        api_gamma=_number(greeks.get("gamma"), "greeks.gamma", nonnegative=True),
    )


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BrokerPayloadError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _strike(value: object) -> float:
    if not isinstance(value, str):
        raise BrokerPayloadError("strike keys must be strings")
    return _number(value, "strike", positive=True)


def _number(
    value: object, field: str, *, positive: bool = False, nonnegative: bool = False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BrokerPayloadError(f"{field} must be numeric")
    try:
        number = float(value)
    except ValueError as error:
        raise BrokerPayloadError(f"{field} must be numeric") from error
    if (
        not math.isfinite(number)
        or (positive and number <= 0)
        or (nonnegative and number < 0)
    ):
        raise BrokerPayloadError(f"{field} is out of range")
    return number


def _integer(value: object, field: str, *, nonnegative: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerPayloadError(f"{field} must be an integer")
    if nonnegative and value < 0:
        raise BrokerPayloadError(f"{field} is out of range")
    return value


def _require_aware_timestamp(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise BrokerPayloadError(f"{field} must be timezone-aware")
    if value.fold:
        raise BrokerPayloadError(
            f"{field} must be an unambiguous timezone-aware timestamp"
        )
