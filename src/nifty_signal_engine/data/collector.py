"""Independent, retrying collection with immutable raw-first persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Protocol, TypeVar
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.dhan_client import BrokerPayloadError, RawSnapshot
from nifty_signal_engine.data.normalizer import normalize_option_chain
from nifty_signal_engine.data.repositories import SnapshotId, SnapshotRepository
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
    assess_snapshot,
)

Instrument = Literal["NIFTY", "BANKNIFTY"]
IST = ZoneInfo("Asia/Kolkata")


class OptionChainBroker(Protocol):
    """The minimal collection-only broker boundary; it exposes no order methods."""

    async def fetch_expiries(self, instrument: Instrument) -> tuple[date, ...]: ...

    async def fetch_option_chain(
        self, instrument: Instrument, expiry: date
    ) -> RawSnapshot: ...


class CollectionStatus(StrEnum):
    COLLECTED = "COLLECTED"
    FETCH_FAILED = "FETCH_FAILED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"


@dataclass(frozen=True, slots=True)
class CollectionResult:
    instrument: Instrument
    status: CollectionStatus
    raw_snapshot_id: SnapshotId | None
    snapshot: OptionChainSnapshot | None
    quality: DataQualityReport | None
    error_type: str | None = None


Normalizer = Callable[[RawSnapshot, Instrument, datetime], OptionChainSnapshot]
QualityAssessor = Callable[
    [OptionChainSnapshot, OptionChainSnapshot | None, datetime], DataQualityReport
]
AsyncSleep = Callable[[float], Awaitable[None]]
_Value = TypeVar("_Value")


class Collector:
    """Collect each instrument independently without UI or signal dependencies."""

    def __init__(
        self,
        *,
        broker: OptionChainBroker,
        repository: SnapshotRepository,
        normalizer: Normalizer = normalize_option_chain,
        quality_assessor: QualityAssessor = assess_snapshot,
        clock: Callable[[], datetime] | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        self._broker = broker
        self._repository = repository
        self._normalizer = normalizer
        self._quality_assessor = quality_assessor
        self._clock = clock or (lambda: datetime.now(IST))
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._previous: dict[Instrument, OptionChainSnapshot] = {}

    async def collect_once(self, instrument: Instrument) -> CollectionResult:
        """Persist raw bytes, strictly normalize, then record a quality decision."""
        try:
            expiries = await self._retry(
                lambda: self._broker.fetch_expiries(instrument)
            )
            if not expiries:
                raise BrokerPayloadError("broker returned no expiries")
            raw = await self._retry(
                lambda: self._broker.fetch_option_chain(instrument, expiries[0])
            )
        except Exception as error:  # noqa: BLE001 - external transport failures are a result.
            return CollectionResult(
                instrument=instrument,
                status=CollectionStatus.FETCH_FAILED,
                raw_snapshot_id=None,
                snapshot=None,
                quality=None,
                error_type=type(error).__name__,
            )

        raw_snapshot_id = self._repository.save_raw(raw.body)
        received_at = self._clock()
        try:
            snapshot = self._normalizer(raw, instrument, received_at)
        except (BrokerPayloadError, ValueError) as error:
            return CollectionResult(
                instrument=instrument,
                status=CollectionStatus.NORMALIZATION_FAILED,
                raw_snapshot_id=raw_snapshot_id,
                snapshot=None,
                quality=None,
                error_type=type(error).__name__,
            )

        self._repository.save_normalized(raw_snapshot_id, snapshot)
        quality = self._quality_assessor(
            snapshot, self._previous.get(instrument), received_at
        )
        if DataQualityCode.OUT_OF_ORDER not in quality.codes:
            self._previous[instrument] = snapshot
        return CollectionResult(
            instrument=instrument,
            status=CollectionStatus.COLLECTED,
            raw_snapshot_id=raw_snapshot_id,
            snapshot=snapshot,
            quality=quality,
        )

    async def _retry(self, operation: Callable[[], Awaitable[_Value]]) -> _Value:
        for attempt in range(self._max_attempts):
            try:
                return await operation()
            except Exception:
                if attempt + 1 == self._max_attempts:
                    raise
                await self._sleep(self._backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable retry exhaustion")
