"""Immutable market-data contracts."""

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, model_validator


class OptionQuote(BaseModel, frozen=True):
    timestamp: datetime
    strike: float
    option_type: Literal["CE", "PE"]
    expiry: date | None = None
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int = 0
    oi: int = 0
    previous_oi: int | None = None
    iv: float | None = None
    api_delta: float | None = None
    api_gamma: float | None = None

    @model_validator(mode="after")
    def validate_timestamp(self) -> Self:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class OptionChainSnapshot(BaseModel, frozen=True):
    instrument: Literal["NIFTY", "BANKNIFTY"]
    source_timestamp: datetime
    received_at: datetime
    spot: float
    expiry: date
    quotes: tuple[OptionQuote, ...]

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.source_timestamp.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return self
