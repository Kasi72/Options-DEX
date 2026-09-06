from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from nifty_signal_engine.backtesting.fills import ExecutableQuote, TradeCandidate
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
                chain.source_timestamp + timedelta(seconds=15),
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
        ReplaySession(tuple(event(second) for second in range(0, 61, 15)))
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
    ).run(ReplaySession(tuple(event(second) for second in range(0, 61, 15))))

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
                    chain.source_timestamp + timedelta(seconds=30), "contract", 99, 101
                ),
                ExecutableQuote(
                    chain.source_timestamp + timedelta(seconds=15), "contract", 99, 101
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
