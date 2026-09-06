"""Task 11 interoperability against real feature-pipeline output."""

from datetime import timedelta

from nifty_signal_engine.features.pipeline import FeaturePipeline
from nifty_signal_engine.monitoring.data_quality import DataQualityReport
from nifty_signal_engine.signals.baselines import RuleBaseline
from tests.factories import aware, make_chain


def snapshot_at(seconds: int):
    timestamp = aware("2026-08-26T09:15:00+05:30") + timedelta(seconds=seconds)
    chain = make_chain(
        timestamp=timestamp.isoformat(),
        call_volume=100 + seconds,
        put_volume=200 + 2 * seconds,
        spot=24_300 + seconds,
    )
    return chain.model_copy(
        update={
            "source_time_authoritative": True,
            "quotes": tuple(
                quote.model_copy(
                    update={
                        "timestamp_authoritative": True,
                        "api_delta": 0.5 if quote.option_type == "CE" else -0.5,
                        "api_gamma": 0.001,
                        "ltp": 100.0,
                    }
                )
                for quote in chain.quotes
            ),
        }
    )


def test_rule_baseline_predicts_deterministically_from_real_completed_row() -> None:
    pipeline = FeaturePipeline()
    for seconds in (0, 15, 30, 45, 60):
        snapshot = snapshot_at(seconds)
        pipeline.on_snapshot(
            snapshot,
            DataQualityReport(
                tradable=True,
                codes=(),
                checked_at=snapshot.received_at,
            ),
        )
    (row,) = pipeline.drain_completed()

    first = RuleBaseline().predict(row)
    second = RuleBaseline().predict(row)

    assert first == second
    assert first.instrument == "NIFTY"
    assert first.feature_available_at == row.available_at
    assert first.feature_schema_version == row.schema_version
    assert first.model_version == "rule-v1"
    assert first.up + first.down + first.no_move == 1.0
