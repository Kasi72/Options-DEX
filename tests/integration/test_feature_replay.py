"""Saved audited inputs replay deterministically, including availability delays."""

import sqlite3
from datetime import date, timedelta

import pytest

from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.features.pipeline import FeaturePipeline
from tests.unit.test_feature_pipeline import quality, sample


def test_saved_audited_snapshots_replay_byte_identically(tmp_path):
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    live = FeaturePipeline()
    expected = []
    for second in range(0, 136, 15):
        chain = sample(second, volume=100 + second, spot=24300 + second)
        report = quality(chain, checked_at=chain.received_at + timedelta(seconds=2))
        raw = repository.save_raw(chain.model_dump_json().encode())
        repository.publish_normalized_with_quality_and_baseline(
            raw, chain, report, baseline=True
        )
        row = live.on_snapshot(chain, report)
        if row is not None:
            expected.append(row.to_json_bytes())
        expected.extend(r.to_json_bytes() for r in live.drain_completed())
        if second == 30:
            duplicate_report = repository.publish_normalized_with_quality_and_baseline(
                raw, chain, report, baseline=True
            )
            assert live.on_snapshot(chain, duplicate_report) is None
    # Duplicate observation rejection must never replace its original historical audit.
    repository.publish_normalized_with_quality_and_baseline(
        raw, chain, report, baseline=True
    )

    def replay():
        pipeline = FeaturePipeline()
        output = []
        for snapshot, audit in repository.iter_audited_session(
            "NIFTY", date(2026, 8, 26)
        ):
            row = pipeline.on_snapshot(snapshot, audit)
            if row is not None:
                output.append(row.to_json_bytes())
            output.extend(r.to_json_bytes() for r in pipeline.drain_completed())
        return output

    assert replay() == replay() == expected
    assert any(b"COMPLETED_MINUTE" in row for row in expected)


@pytest.mark.parametrize("corruption", ["missing", "malformed", "unknown_code"])
def test_audited_replay_fails_closed_for_missing_or_corrupt_original_quality(
    tmp_path, corruption
):
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    chain = sample()
    raw = repository.save_raw(b"audit-fixture")
    report = quality(chain)
    repository.publish_normalized_with_quality_and_baseline(
        raw, chain, report, baseline=True
    )
    repository.publish_normalized_with_quality_and_baseline(
        raw, chain, report, baseline=True
    )
    with sqlite3.connect(repository.database_path) as connection:
        if corruption == "missing":
            connection.execute("DELETE FROM quality_decisions WHERE id = 1")
        else:
            connection.execute(
                "UPDATE quality_decisions SET codes = ? WHERE id = 1",
                ("{}" if corruption == "malformed" else '["FORGED"]',),
            )
    with pytest.raises((RuntimeError, ValueError)):
        list(repository.iter_audited_session("NIFTY", date(2026, 8, 26)))


def test_audited_replay_includes_negative_decisions(tmp_path):
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    for second in [0, 15, 30, 45, 60]:
        chain = sample(second, volume=100 + second)
        raw = repository.save_raw(chain.model_dump_json().encode())
        repository.publish_normalized_with_quality_and_baseline(
            raw, chain, quality(chain, tradable=second != 30), baseline=False
        )
    pipeline = FeaturePipeline()
    pairs = list(repository.iter_audited_session("NIFTY", date(2026, 8, 26)))
    assert len(pairs) == 5 and not pairs[2][1].tradable
    for chain, report in pairs:
        pipeline.on_snapshot(chain, report)
    assert pipeline.drain_completed() == ()
