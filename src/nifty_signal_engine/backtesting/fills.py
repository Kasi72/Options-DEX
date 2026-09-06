"""Executable quote validation and conservative long-entry fills."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nifty_signal_engine.config.market_hours import IST


def _timestamp(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(IST)


def _finite_nonnegative(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return float(value)


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    """A research-only intent, never an order instruction."""

    signal_time: datetime
    contract_id: str
    quantity: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "signal_time", _timestamp(self.signal_time, "signal_time")
        )
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("contract_id must not be blank")
        if isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    """A timestamped bid/ask observation that can support a simulated fill."""

    timestamp: datetime
    contract_id: str
    bid: float | None
    ask: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp, "timestamp"))
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("contract_id must not be blank")
        object.__setattr__(self, "bid", _finite_nonnegative(self.bid, "bid"))
        object.__setattr__(self, "ask", _finite_nonnegative(self.ask, "ask"))


class RejectionCode(StrEnum):
    """Explicit reasons an intent did not become a simulated fill."""

    INVALID_QUOTE = "INVALID_QUOTE"
    NOT_NEXT_QUOTE = "NOT_NEXT_QUOTE"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    NO_EXECUTABLE_QUOTE = "NO_EXECUTABLE_QUOTE"


@dataclass(frozen=True, slots=True)
class Fill:
    candidate: TradeCandidate
    executed_at: datetime
    price: float
    quantity: int
    bid: float
    ask: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "executed_at", _timestamp(self.executed_at, "executed_at")
        )
        if (
            not math.isfinite(self.price)
            or self.price < 0
            or not math.isfinite(self.bid)
            or not math.isfinite(self.ask)
            or self.bid < 0
            or self.ask < self.bid
        ):
            raise ValueError("fill must contain a valid executable quote")
        if self.executed_at <= self.candidate.signal_time:
            raise ValueError("fill must follow its signal")
        if self.quantity != self.candidate.quantity:
            raise ValueError("fill quantity must equal candidate quantity")


@dataclass(frozen=True, slots=True)
class Rejection:
    candidate: TradeCandidate
    code: RejectionCode
    quote: ExecutableQuote | None = None


class FillSimulator:
    """Fill long option entries at the first valid quote's ask.

    The simulator intentionally does not infer a price from LTP or a midpoint.
    A quote that is not strictly after the signal is historical information, not
    a next executable quote.
    """

    def enter_long(
        self, candidate: TradeCandidate, next_quote: ExecutableQuote
    ) -> Fill | Rejection:
        if next_quote.contract_id != candidate.contract_id:
            return Rejection(candidate, RejectionCode.CONTRACT_MISMATCH, next_quote)
        if next_quote.timestamp <= candidate.signal_time:
            return Rejection(candidate, RejectionCode.NOT_NEXT_QUOTE, next_quote)
        if (
            next_quote.bid is None
            or next_quote.ask is None
            or next_quote.ask < next_quote.bid
        ):
            return Rejection(candidate, RejectionCode.INVALID_QUOTE, next_quote)
        return Fill(
            candidate=candidate,
            executed_at=next_quote.timestamp,
            price=next_quote.ask,
            quantity=candidate.quantity,
            bid=next_quote.bid,
            ask=next_quote.ask,
        )
