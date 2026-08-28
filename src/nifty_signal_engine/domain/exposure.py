"""Immutable results and statuses for exposure calculations."""

from enum import StrEnum

from pydantic import BaseModel


class DataQualityStatus(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    OUT_OF_RANGE = "OUT_OF_RANGE"


class ZeroLevelStatus(StrEnum):
    VALID = "VALID"
    NO_SIGN_CHANGE = "NO_SIGN_CHANGE"
    ALL_ZERO = "ALL_ZERO"
    NUMERICAL_ERROR = "NUMERICAL_ERROR"


class ExposureResult(BaseModel, frozen=True):
    call_gex_rupees: float = 0.0
    put_gex_rupees: float = 0.0
    net_gex_rupees: float = 0.0
    call_dex_rupees: float = 0.0
    put_dex_rupees: float = 0.0
    net_dex_rupees: float = 0.0

    @property
    def gex_crore(self) -> float:
        return self.net_gex_rupees / 10_000_000

    @property
    def dex_crore(self) -> float:
        return self.net_dex_rupees / 10_000_000


class ZeroLevelResult(BaseModel, frozen=True):
    level: float | None
    status: ZeroLevelStatus
