import asyncio
import sqlite3
from datetime import date
from pathlib import Path

from nifty_signal_engine.data.collector import CollectionStatus, Collector
from nifty_signal_engine.data.dhan_client import BrokerPayloadError, RawSnapshot
from nifty_signal_engine.data.normalizer import normalize_option_chain
from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.monitoring.data_quality import DataQualityCode
from tests.factories import aware, make_chain

FIXTURES = Path(__file__).parents[1] / "fixtures"


class _Broker:
    def __init__(self, raw: RawSnapshot, failures: int = 0) -> None:
        self.raw = raw
        self.failures = failures
        self.calls = 0

    async def fetch_expiries(self, instrument: str) -> tuple[date, ...]:
        return (date(2026, 9, 1),)

    async def fetch_option_chain(self, instrument: str, expiry: date) -> RawSnapshot:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary transport failure")
        return self.raw


def _raw() -> RawSnapshot:
    return RawSnapshot(
        body=(FIXTURES / "dhan_option_chain.json").read_bytes(),
        captured_at=aware("2026-08-30T10:00:00+05:30"),
        expiry=date(2026, 9, 1),
    )


def test_collector_saves_immutable_raw_before_normalization_and_persists_research_snapshot(
    tmp_path: Path,
) -> None:
    """Moving raw persistence below normalization must lose a captured malformed response."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    raw = _raw()

    def normalizer(raw_snapshot: RawSnapshot, instrument: str, received_at):
        with sqlite3.connect(repository.database_path) as connection:
            saved = connection.execute(
                "SELECT compressed_payload FROM raw_snapshots"
            ).fetchall()
        assert len(saved) == 1
        return normalize_option_chain(raw_snapshot, instrument, received_at)

    collector = Collector(
        broker=_Broker(raw),
        repository=repository,
        normalizer=normalizer,
        clock=lambda: aware("2026-08-30T10:00:01+05:30"),
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.COLLECTED
    assert result.raw_snapshot_id is not None
    assert repository.read_raw(result.raw_snapshot_id) == raw.body
    assert len(list(repository.iter_session("NIFTY", date(2026, 8, 30)))) == 1
    assert result.quality is not None
    assert result.quality.tradable is False
    repository.close()


def test_collector_keeps_raw_bytes_when_strict_normalization_rejects_them(
    tmp_path: Path,
) -> None:
    """Catching normalization failures before raw persistence must erase forensic evidence."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    raw = _raw()

    def reject(*_args: object) -> object:
        raise BrokerPayloadError("invalid payload")

    collector = Collector(
        broker=_Broker(raw),
        repository=repository,
        normalizer=reject,
        clock=lambda: aware("2026-08-30T10:00:01+05:30"),
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.NORMALIZATION_FAILED
    assert result.raw_snapshot_id is not None
    assert repository.read_raw(result.raw_snapshot_id) == raw.body
    assert list(repository.iter_session("NIFTY", date(2026, 8, 30))) == []
    repository.close()


def test_collector_retries_transient_fetches_with_bounded_exponential_backoff(
    tmp_path: Path,
) -> None:
    """Removing bounded retries must make a transient client failure discard a later capture."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    delays: list[float] = []
    broker = _Broker(_raw(), failures=2)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    collector = Collector(
        broker=broker,
        repository=repository,
        clock=lambda: aware("2026-08-30T10:00:01+05:30"),
        sleep=sleep,
        max_attempts=3,
        backoff_seconds=0.25,
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.COLLECTED
    assert broker.calls == 3
    assert delays == [0.25, 0.5]
    assert result.raw_snapshot_id is not None
    assert repository.read_raw(result.raw_snapshot_id) == _raw().body
    repository.close()


def test_collector_does_not_replace_an_instrument_baseline_with_out_of_order_data(
    tmp_path: Path,
) -> None:
    """Replacing state with late data must not hide a later cumulative-counter reset."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    snapshots = iter(
        (
            make_chain(timestamp="2026-08-30T10:01:00+05:30", call_volume=100, put_volume=100),
            make_chain(timestamp="2026-08-30T10:00:00+05:30", call_volume=90, put_volume=90),
            make_chain(timestamp="2026-08-30T10:02:00+05:30", call_volume=95, put_volume=95),
        )
    )

    def normalizer(_raw: RawSnapshot, _instrument: str, _received_at):
        return next(snapshots)

    collector = Collector(
        broker=_Broker(_raw()),
        repository=repository,
        normalizer=normalizer,
        clock=lambda: aware("2026-08-30T10:02:01+05:30"),
    )

    first, late, after_late = [
        asyncio.run(collector.collect_once("NIFTY")) for _ in range(3)
    ]

    assert first.quality is not None
    assert late.quality is not None and DataQualityCode.OUT_OF_ORDER in late.quality.codes
    assert after_late.quality is not None
    assert DataQualityCode.COUNTER_RESET in after_late.quality.codes
    repository.close()
