"""Deterministic replay using the live feature and signal collaborators."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Protocol

from nifty_signal_engine.backtesting.costs import (
    CostBreakdown,
    CostedTrade,
    CostSchedule,
)
from nifty_signal_engine.backtesting.fills import (
    ExecutableQuote,
    Fill,
    FillSimulator,
    Rejection,
    RejectionCode,
    TradeCandidate,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.features.pipeline import FeaturePipeline, FeatureRow
from nifty_signal_engine.monitoring.data_quality import DataQualityReport


class SignalService(Protocol):
    """Production signal collaborator used by live mode and replay alike."""

    def on_feature(
        self, row: FeatureRow, quality: DataQualityReport
    ) -> TradeCandidate | Iterable[TradeCandidate] | object | None: ...


type SignalCallable = Callable[
    [FeatureRow, DataQualityReport],
    TradeCandidate | Iterable[TradeCandidate] | object | None,
]


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    snapshot: OptionChainSnapshot
    quality: DataQualityReport
    quotes: tuple[ExecutableQuote, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.quality, DataQualityReport):
            raise TypeError("quality must be DataQualityReport")
        if self.snapshot.source_timestamp.tzinfo is None:
            raise ValueError("snapshot source timestamp must be timezone-aware")
        if any(
            quote.timestamp < self.snapshot.source_timestamp for quote in self.quotes
        ):
            raise ValueError("executable quote predates its replay event")
        quote_times = tuple(quote.timestamp for quote in self.quotes)
        if any(
            current < previous for previous, current in pairwise(quote_times)
        ):
            raise ValueError("executable quotes must be chronological")


@dataclass(frozen=True, slots=True)
class ReplaySession:
    """Persisted audited events in their original order; they are never sorted."""

    events: tuple[ReplayEvent, ...]
    candidates: tuple[TradeCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayResult:
    feature_rows: tuple[FeatureRow, ...]
    signals: tuple[object, ...]
    fills: tuple[Fill, ...]
    rejections: tuple[Rejection, ...]
    costs: tuple[CostBreakdown, ...]
    cost_schedule: object | None


@dataclass(slots=True)
class ReplayEngine:
    """Replay a session in source order without alternate feature logic.

    ``pipeline`` and ``signal_service`` are injected production collaborators.
    This class deliberately has no feature calculation of its own.  A missing
    signal service is a valid feature-only replay, not a synthetic strategy.
    """

    pipeline: FeaturePipeline = field(default_factory=FeaturePipeline)
    signal_service: SignalService | SignalCallable | None = None
    fill_simulator: FillSimulator = field(default_factory=FillSimulator)
    cost_schedule: CostSchedule | None = None

    def run(self, session: ReplaySession) -> ReplayResult:
        if not isinstance(session, ReplaySession):
            raise TypeError("session must be ReplaySession")
        self._validate_chronology(session.events)
        pending = list(session.candidates)
        feature_rows: list[FeatureRow] = []
        signals: list[object] = []
        fills: list[Fill] = []
        rejections: list[Rejection] = []
        costs: list[CostBreakdown] = []

        for event in session.events:
            self.pipeline.on_snapshot(event.snapshot, event.quality)
            rows = self.pipeline.drain_completed()
            feature_rows.extend(rows)
            for row in rows:
                outcome = self._signal(row, event.quality)
                if outcome is not None:
                    signals.append(outcome)
                    pending.extend(self._candidates(outcome))
            pending = self._fill_pending(
                pending, event.quotes, fills, rejections, costs
            )

        rejections.extend(
            Rejection(candidate, RejectionCode.NO_EXECUTABLE_QUOTE)
            for candidate in pending
        )
        return ReplayResult(
            feature_rows=tuple(feature_rows),
            signals=tuple(signals),
            fills=tuple(fills),
            rejections=tuple(rejections),
            costs=tuple(costs),
            cost_schedule=(self.cost_schedule.report() if self.cost_schedule else None),
        )

    @staticmethod
    def _validate_chronology(events: tuple[ReplayEvent, ...]) -> None:
        previous: datetime | None = None
        for event in events:
            timestamp = event.snapshot.source_timestamp
            if previous is not None and timestamp <= previous:
                raise ValueError("replay events must be strictly chronological")
            previous = timestamp

    def _signal(self, row: FeatureRow, quality: DataQualityReport) -> object | None:
        service = self.signal_service
        if service is None:
            return None
        on_feature = getattr(service, "on_feature", None)
        if callable(on_feature):
            return on_feature(row, quality)
        evaluate = getattr(service, "evaluate", None)
        if callable(evaluate):
            return evaluate(row, quality)
        if callable(service):
            return service(row, quality)
        raise TypeError(
            "signal_service must implement on_feature, evaluate, or be callable"
        )

    @staticmethod
    def _candidates(outcome: object) -> tuple[TradeCandidate, ...]:
        if isinstance(outcome, TradeCandidate):
            return (outcome,)
        if isinstance(outcome, (str, bytes)):
            return ()
        try:
            values: tuple[object, ...] = tuple(outcome)  # type: ignore[arg-type]
        except TypeError:
            return ()
        if not all(isinstance(value, TradeCandidate) for value in values):
            return ()
        return tuple(value for value in values if isinstance(value, TradeCandidate))

    def _fill_pending(
        self,
        pending: list[TradeCandidate],
        quotes: tuple[ExecutableQuote, ...],
        fills: list[Fill],
        rejections: list[Rejection],
        costs: list[CostBreakdown],
    ) -> list[TradeCandidate]:
        remaining: list[TradeCandidate] = []
        for candidate in pending:
            quote = next(
                (
                    item
                    for item in quotes
                    if item.contract_id == candidate.contract_id
                    and item.timestamp > candidate.signal_time
                ),
                None,
            )
            if quote is None:
                remaining.append(candidate)
                continue
            result = self.fill_simulator.enter_long(candidate, quote)
            if isinstance(result, Fill):
                fills.append(result)
                if self.cost_schedule is not None:
                    costs.append(self.cost_schedule.estimate(CostedTrade(entry=result)))
            else:
                rejections.append(result)
        return remaining
