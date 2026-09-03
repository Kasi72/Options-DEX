"""Independent, retrying collection with immutable raw-first persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Protocol, TypeVar
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.dhan_client import (
    BrokerHTTPError,
    BrokerPayloadError,
    RawSnapshot,
    is_transient_error,
)
from nifty_signal_engine.data.normalizer import normalize_option_chain
from nifty_signal_engine.data.repositories import SnapshotId, SnapshotRepository
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
    QualityConfig,
    TradingCalendar,
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
    QUALITY_ASSESSMENT_FAILED = "QUALITY_ASSESSMENT_FAILED"


@dataclass(frozen=True, slots=True)
class CollectionResult:
    instrument: Instrument
    status: CollectionStatus
    raw_snapshot_id: SnapshotId | None
    snapshot: OptionChainSnapshot | None
    quality: DataQualityReport | None
    error_type: str | None = None
    active_expiries: tuple[date, ...] = ()
    selected_expiry: date | None = None


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
        calendar: TradingCalendar | None = None,
        quality_config: QualityConfig | None = None,
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
        self._baseline_loaded: set[Instrument] = set()
        self._calendar = calendar
        self._quality_config = quality_config or QualityConfig()
        self._last_error_raw_id: SnapshotId | None = None

    async def collect_once(self, instrument: Instrument) -> CollectionResult:
        """Persist raw bytes, strictly normalize, then record a quality decision."""
        self._last_error_raw_id = None
        try:
            expiries = await self._retry(
                lambda: self._broker.fetch_expiries(instrument)
            )
            active_expiries = tuple(
                sorted(
                    {expiry for expiry in expiries if expiry >= self._clock().date()}
                )
            )
            if not active_expiries:
                raise BrokerPayloadError("broker returned no expiries")
            raw = await self._retry(
                lambda: self._broker.fetch_option_chain(instrument, active_expiries[0]),
                on_error=lambda error: self._persist_http_error_body(error),
            )
        except Exception as error:  # noqa: BLE001 - external transport failures are a result.
            return CollectionResult(
                instrument=instrument,
                status=CollectionStatus.FETCH_FAILED,
                raw_snapshot_id=self._last_error_raw_id,
                snapshot=None,
                quality=None,
                error_type=type(error).__name__,
            )

        raw_snapshot_id = self._repository.save_raw(raw.body)
        # RawSnapshot captures the actual HTTP receipt. Do not resample a later
        # clock after raw publication and misrepresent it as broker receipt time.
        received_at = raw.captured_at
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
                active_expiries=active_expiries,
                selected_expiry=active_expiries[0],
            )

        baseline_error = False
        try:
            previous = self._previous_for(instrument)
        except RuntimeError:
            previous = None
            baseline_error = True
        # Receipt provenance remains the raw capture; assessment freshness is
        # evaluated at the independently sampled time work actually completed.
        assessed_at = self._clock()
        try:
            if self._quality_assessor is assess_snapshot:
                quality = assess_snapshot(
                    snapshot,
                    previous,
                    assessed_at,
                    calendar=self._calendar,
                    active_expiries=active_expiries,
                    config=self._quality_config,
                )
            else:
                quality = self._quality_assessor(snapshot, previous, assessed_at)
        except Exception as error:  # noqa: BLE001 - must persist a fail-closed audit decision.
            quality = DataQualityReport(
                tradable=False,
                codes=(DataQualityCode.QUALITY_ASSESSMENT_FAILED,),
                checked_at=assessed_at,
                details={"error_type": type(error).__name__},
            )
            quality = self._repository.publish_normalized_with_quality_and_baseline(
                raw_snapshot_id, snapshot, quality, baseline=False
            )
            return CollectionResult(
                instrument=instrument,
                status=CollectionStatus.QUALITY_ASSESSMENT_FAILED,
                raw_snapshot_id=raw_snapshot_id,
                snapshot=snapshot,
                quality=quality,
                error_type=type(error).__name__,
                active_expiries=active_expiries,
                selected_expiry=active_expiries[0],
            )
        if baseline_error:
            quality = quality.model_copy(
                update={
                    "tradable": False,
                    "codes": tuple((*quality.codes, DataQualityCode.BASELINE_CORRUPT)),
                    "details": {**quality.details, "baseline": "repository_corrupt"},
                }
            )
        baseline = (
            DataQualityCode.OUT_OF_ORDER not in quality.codes and not baseline_error
        )
        quality = self._repository.publish_normalized_with_quality_and_baseline(
            raw_snapshot_id, snapshot, quality, baseline=baseline
        )
        if baseline:
            self._previous[instrument] = snapshot
        return CollectionResult(
            instrument=instrument,
            status=CollectionStatus.COLLECTED,
            raw_snapshot_id=raw_snapshot_id,
            snapshot=snapshot,
            quality=quality,
            active_expiries=active_expiries,
            selected_expiry=active_expiries[0],
        )

    def _previous_for(self, instrument: Instrument) -> OptionChainSnapshot | None:
        if instrument not in self._baseline_loaded:
            restored = self._repository.load_collector_baseline(instrument)
            if restored is not None:
                self._previous[instrument] = restored
            self._baseline_loaded.add(instrument)
        return self._previous.get(instrument)

    def _persist_http_error_body(self, error: Exception) -> None:
        if isinstance(error, BrokerHTTPError):
            self._last_error_raw_id = self._repository.save_raw(error.body)

    async def _retry(
        self,
        operation: Callable[[], Awaitable[_Value]],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> _Value:
        for attempt in range(self._max_attempts):
            try:
                return await operation()
            except Exception as error:
                if on_error is not None:
                    on_error(error)
                if not is_transient_error(error) or attempt + 1 == self._max_attempts:
                    raise
                await self._sleep(self._backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable retry exhaustion")
