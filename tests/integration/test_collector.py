import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import httpx

from nifty_signal_engine.data.collector import CollectionStatus, Collector
from nifty_signal_engine.data.dhan_client import BrokerPayloadError, RawSnapshot
from nifty_signal_engine.data.normalizer import normalize_option_chain
from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)
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
            raise httpx.ConnectError("temporary transport failure")
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
    assert result.active_expiries == (date(2026, 9, 1),)
    assert result.selected_expiry == date(2026, 9, 1)
    assert result.raw_snapshot_id is not None
    assert repository.read_raw(result.raw_snapshot_id) == raw.body
    assert len(list(repository.iter_session("NIFTY", date(2026, 8, 30)))) == 1
    assert result.quality is not None
    assert result.quality.tradable is False
    repository.close()


def test_collector_uses_raw_capture_as_the_normalized_receipt_time(tmp_path: Path) -> None:
    """Sampling a second clock after persistence would mislabel HTTP receipt time."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    raw = _raw()
    collector = Collector(
        broker=_Broker(raw),
        repository=repository,
        clock=lambda: aware("2026-08-30T10:05:00+05:30"),
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.snapshot is not None
    assert result.snapshot.received_at == raw.captured_at
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
            make_chain(
                timestamp="2026-08-30T10:01:00+05:30", call_volume=100, put_volume=100
            ),
            make_chain(
                timestamp="2026-08-30T10:00:00+05:30", call_volume=90, put_volume=90
            ),
            make_chain(
                timestamp="2026-08-30T10:02:00+05:30", call_volume=95, put_volume=95
            ),
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
    assert (
        late.quality is not None and DataQualityCode.OUT_OF_ORDER in late.quality.codes
    )
    assert after_late.quality is not None
    assert DataQualityCode.COUNTER_RESET in after_late.quality.codes
    repository.close()


def test_collector_persists_an_explicit_quality_failure_after_normalization(
    tmp_path: Path,
) -> None:
    """Letting an assessor exception escape must not leave a normalized snapshot un-audited."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )

    def assessor(*_args: object):
        raise RuntimeError("internal assessor error")

    collector = Collector(
        broker=_Broker(_raw()),
        repository=repository,
        quality_assessor=assessor,
        clock=lambda: aware("2026-08-30T10:00:01+05:30"),
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.QUALITY_ASSESSMENT_FAILED
    assert result.quality is not None
    assert DataQualityCode.QUALITY_ASSESSMENT_FAILED in result.quality.codes
    assert repository.session_quality_summary("NIFTY", date(2026, 8, 30))["codes"] == {
        "QUALITY_ASSESSMENT_FAILED": 1
    }
    repository.close()


def test_collector_does_not_retry_a_payload_error(tmp_path: Path) -> None:
    """Classifying malformed broker payloads as transient must not multiply invalid requests."""
    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    broker = _Broker(_raw(), failures=1)

    async def malformed_expiries(_instrument: str):
        broker.calls += 1
        raise BrokerPayloadError("malformed")

    broker.fetch_expiries = malformed_expiries  # type: ignore[method-assign]
    collector = Collector(broker=broker, repository=repository, max_attempts=3)

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.FETCH_FAILED
    assert broker.calls == 1
    repository.close()


def test_collector_persists_transient_http_error_body_before_retrying(
    tmp_path: Path,
) -> None:
    """Retrying before raw persistence must not discard a broker's failed response evidence."""
    from nifty_signal_engine.data.dhan_client import BrokerHTTPError

    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )
    receipt = aware("2026-08-30T10:00:00+05:30")

    class ErrorThenRawBroker(_Broker):
        async def fetch_option_chain(
            self, instrument: str, expiry: date
        ) -> RawSnapshot:
            self.calls += 1
            if self.calls == 1:
                raise BrokerHTTPError(503, b'{"error":"busy"}', receipt)
            return self.raw

    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    collector = Collector(
        broker=ErrorThenRawBroker(_raw()),
        repository=repository,
        sleep=sleep,
        clock=lambda: aware("2026-08-30T10:00:01+05:30"),
    )

    result = asyncio.run(collector.collect_once("NIFTY"))

    assert result.status is CollectionStatus.COLLECTED
    assert delays == [0.5]
    error_id = repository.save_raw(b'{"error":"busy"}')
    assert repository.read_raw(error_id) == b'{"error":"busy"}'
    repository.close()


def test_final_http_failure_returns_the_persisted_forensic_raw_id(
    tmp_path: Path,
) -> None:
    """Dropping the final HTTP error digest must make captured forensic bytes unreachable."""
    from nifty_signal_engine.data.dhan_client import BrokerHTTPError

    repository = SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )

    class AlwaysBusyBroker(_Broker):
        async def fetch_option_chain(
            self, instrument: str, expiry: date
        ) -> RawSnapshot:
            raise BrokerHTTPError(
                503, b'{"error":"busy"}', aware("2026-08-30T10:00:00+05:30")
            )

    async def sleep(_delay: float) -> None:
        return None

    result = asyncio.run(
        Collector(
            broker=AlwaysBusyBroker(_raw()),
            repository=repository,
            sleep=sleep,
            max_attempts=2,
            clock=lambda: aware("2026-08-30T10:00:01+05:30"),
        ).collect_once("NIFTY")
    )

    assert result.status is CollectionStatus.FETCH_FAILED
    assert result.raw_snapshot_id is not None
    assert repository.read_raw(result.raw_snapshot_id) == b'{"error":"busy"}'
    repository.close()


def test_collector_restores_per_instrument_baseline_after_restart(
    tmp_path: Path,
) -> None:
    """Ignoring persisted state on restart must not classify the next snapshot as an initial baseline."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    first = make_chain(
        timestamp="2026-08-30T10:00:00+05:30", call_volume=10, put_volume=10
    ).model_copy(update={"source_time_authoritative": True})
    second = make_chain(
        timestamp="2026-08-30T10:01:00+05:30", call_volume=20, put_volume=20
    ).model_copy(update={"source_time_authoritative": True})

    def make_collector(repository: SnapshotRepository, snapshot: object) -> Collector:
        return Collector(
            broker=_Broker(_raw()),
            repository=repository,
            normalizer=lambda *_args: snapshot,
            quality_assessor=lambda current, previous, now: __import__(
                "nifty_signal_engine.monitoring.data_quality",
                fromlist=["assess_snapshot"],
            ).assess_snapshot(
                current,
                previous,
                now,
                calendar=__import__(
                    "nifty_signal_engine.monitoring.data_quality",
                    fromlist=["TradingCalendar"],
                ).TradingCalendar({date(2026, 8, 30)}),
                active_expiries=(date(2026, 9, 1),),
                config=__import__(
                    "nifty_signal_engine.monitoring.data_quality",
                    fromlist=["QualityConfig"],
                ).QualityConfig(minimum_paired_strikes=1),
            ),
            clock=lambda: aware("2026-08-30T10:01:01+05:30"),
        )

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        asyncio.run(make_collector(repository, first).collect_once("NIFTY"))
    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        result = asyncio.run(make_collector(repository, second).collect_once("NIFTY"))

    assert result.quality is not None
    assert DataQualityCode.BASELINE_UNAVAILABLE not in result.quality.codes


def test_corrupt_persisted_baseline_is_explicitly_nontradable(tmp_path: Path) -> None:
    """A damaged state reference must not be replaced by another instrument's data."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    first = make_chain(timestamp="2026-08-30T10:00:00+05:30")
    second = make_chain(timestamp="2026-08-30T10:01:00+05:30")

    def assessor(current, _previous, now):
        return DataQualityReport(tradable=True, codes=(), checked_at=now)

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        first_result = asyncio.run(
            Collector(
                broker=_Broker(_raw()),
                repository=repository,
                normalizer=lambda *_args: first,
                quality_assessor=assessor,
                clock=lambda: aware("2026-08-30T10:00:01+05:30"),
            ).collect_once("NIFTY")
        )
        assert first_result.quality is not None

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE collector_state SET state = ? WHERE instrument = ?",
            ('{"normalized_snapshot_id": 999999}', "NIFTY"),
        )
        connection.commit()

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        result = asyncio.run(
            Collector(
                broker=_Broker(_raw()),
                repository=repository,
                normalizer=lambda *_args: second,
                quality_assessor=assessor,
                clock=lambda: aware("2026-08-30T10:01:01+05:30"),
            ).collect_once("NIFTY")
        )

    assert result.quality is not None
    assert DataQualityCode.BASELINE_CORRUPT in result.quality.codes
