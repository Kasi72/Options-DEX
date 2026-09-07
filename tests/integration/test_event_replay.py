from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from nifty_signal_engine.backtesting.fills import (
    ExecutableQuote,
    RejectionCode,
    TradeCandidate,
)
from nifty_signal_engine.backtesting.replay import (
    ReplayEngine,
    ReplayEvent,
    ReplaySession,
)
from nifty_signal_engine.features.pipeline import FeaturePipeline, FeatureRow
from tests.unit.test_feature_pipeline import quality, sample


@dataclass
class ProductionSignalSpy:
    rows: list[FeatureRow] = field(default_factory=list)

    def on_feature(self, row: FeatureRow, _quality: object) -> TradeCandidate | None:
        self.rows.append(row)
        return TradeCandidate(row.available_at, "NIFTY-20260901-24300-CE")


@dataclass
class EvaluateSignalSpy:
    calls: int = 0

    def evaluate(self, row: FeatureRow, _quality: object) -> TradeCandidate:
        self.calls += 1
        return TradeCandidate(row.available_at, "NIFTY-20260901-24300-CE")


def event(second: int) -> ReplayEvent:
    chain = sample(second, volume=100 + second)
    return ReplayEvent(
        snapshot=chain,
        quality=quality(chain),
        quotes=(
            ExecutableQuote(
                chain.source_timestamp,
                "NIFTY-20260901-24300-CE",
                bid=99,
                ask=101,
            ),
        ),
    )


def test_replay_uses_injected_production_feature_pipeline_and_signal_service() -> None:
    # The four polls complete the first minute; the fifth supplies the next quote.
    signal_service = ProductionSignalSpy()
    pipeline = FeaturePipeline()
    result = ReplayEngine(pipeline=pipeline, signal_service=signal_service).run(
        ReplaySession(tuple(event(second) for second in range(0, 76, 15)))
    )

    assert len(signal_service.rows) == 1
    assert signal_service.rows == list(result.feature_rows)
    assert len(result.fills) == 1
    assert result.fills[0].price == 101


def test_replay_rejects_out_of_order_events_instead_of_sorting_them() -> None:
    later = event(15)
    earlier = event(0)

    with pytest.raises(ValueError, match="chronological"):
        ReplayEngine(
            pipeline=FeaturePipeline(), signal_service=ProductionSignalSpy()
        ).run(ReplaySession((later, earlier)))


def test_replay_reuses_an_evaluate_style_production_signal_service() -> None:
    signal_service = EvaluateSignalSpy()

    result = ReplayEngine(
        pipeline=FeaturePipeline(), signal_service=signal_service
    ).run(ReplaySession(tuple(event(second) for second in range(0, 76, 15))))

    assert signal_service.calls == 1
    assert len(result.fills) == 1


def test_event_rejects_out_of_order_executable_quotes() -> None:
    chain = sample(0, volume=100)

    with pytest.raises(ValueError, match="chronological"):
        ReplayEvent(
            snapshot=chain,
            quality=quality(chain),
            quotes=(
                ExecutableQuote(
                    chain.source_timestamp, "contract", 99, 101
                ),
                ExecutableQuote(
                    chain.source_timestamp - timedelta(seconds=15), "contract", 99, 101
                ),
            ),
        )


def test_replay_leaves_unfilled_candidate_as_explicit_rejection() -> None:
    first = event(0)
    candidate = TradeCandidate(first.snapshot.source_timestamp, "missing-contract")
    session = ReplaySession((first,), candidates=(candidate,))

    result = ReplayEngine(
        pipeline=FeaturePipeline(), signal_service=ProductionSignalSpy()
    ).run(session)

    assert result.fills == ()
    assert result.rejections[0].candidate == candidate


def test_event_rejects_quote_timestamp_that_is_future_to_its_snapshot() -> None:
    chain = sample(0, volume=100)

    with pytest.raises(ValueError, match="future"):
        ReplayEvent(
            snapshot=chain,
            quality=quality(chain),
            quotes=(
                ExecutableQuote(
                    chain.source_timestamp + timedelta(seconds=1), "contract", 99, 101
                ),
            ),
        )


def test_replay_rejects_global_quote_time_regression_across_events() -> None:
    first = event(15)
    second = event(30)
    second = ReplayEvent(
        snapshot=second.snapshot,
        quality=second.quality,
        quotes=(
            ExecutableQuote(
                first.snapshot.source_timestamp - timedelta(seconds=1), "contract", 99, 101
            ),
        ),
    )

    with pytest.raises(ValueError, match="quote chronology"):
        ReplayEngine(pipeline=FeaturePipeline()).run(ReplaySession((first, second)))


def test_non_tradable_event_cancels_pending_candidate_without_filling_it() -> None:
    first = event(0)
    candidate = TradeCandidate(first.snapshot.source_timestamp, "NIFTY-20260901-24300-CE")
    rejected = event(15)
    rejected = ReplayEvent(
        snapshot=rejected.snapshot,
        quality=quality(rejected.snapshot, tradable=False),
        quotes=rejected.quotes,
    )

    result = ReplayEngine(pipeline=FeaturePipeline()).run(
        ReplaySession((first, rejected), candidates=(candidate,))
    )

    assert result.fills == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].code is RejectionCode.NON_TRADABLE_EVENT


def test_pipeline_rejected_event_cancels_pending_candidate_without_filling_it() -> None:
    first = event(0)
    candidate = TradeCandidate(first.snapshot.source_timestamp, "NIFTY-20260901-24300-CE")
    rejected = event(15)
    rejected = ReplayEvent(
        snapshot=rejected.snapshot.model_copy(
            update={"source_time_authoritative": False}
        ),
        quality=rejected.quality,
        quotes=rejected.quotes,
    )

    result = ReplayEngine(pipeline=FeaturePipeline()).run(
        ReplaySession((first, rejected), candidates=(candidate,))
    )

    assert result.fills == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].code is RejectionCode.REJECTED_EVENT


def test_candidate_before_its_feature_availability_is_rejected_as_lookahead() -> None:
    class LookaheadService:
        def on_feature(self, row: FeatureRow, _quality: object) -> TradeCandidate:
            return TradeCandidate(
                row.available_at - timedelta(seconds=1), "NIFTY-20260901-24300-CE"
            )

    result = ReplayEngine(
        pipeline=FeaturePipeline(), signal_service=LookaheadService()
    ).run(ReplaySession(tuple(event(second) for second in range(0, 76, 15))))

    assert result.fills == ()
    assert result.rejections[0].code is RejectionCode.LOOKAHEAD_SIGNAL_TIME


def test_same_engine_replays_the_same_session_byte_identically() -> None:
    service = lambda row, _quality: TradeCandidate(
        row.available_at, "NIFTY-20260901-24300-CE"
    )
    engine = ReplayEngine(pipeline=FeaturePipeline(), signal_service=service)
    session = ReplaySession(tuple(event(second) for second in range(0, 76, 15)))

    assert engine.run(session) == engine.run(session)
