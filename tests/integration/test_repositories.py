"""Integration contracts for immutable local market-data persistence."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest

from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.domain.market import OptionChainSnapshot
from tests.factories import make_chain


@pytest.fixture
def repository(tmp_path: Path) -> SnapshotRepository:
    return SnapshotRepository(database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet")


@pytest.fixture
def raw_payload() -> bytes:
    return b'{"data":{"oc":[]},"status":"success"}'


def test_raw_snapshot_is_content_addressed_and_immutable(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Changing duplicate handling must not rewrite the captured source bytes."""
    first = repository.save_raw(raw_payload)
    second = repository.save_raw(raw_payload)

    assert first == second
    assert repository.read_raw(first) == raw_payload


def test_schema_uses_wal_and_records_current_version(repository: SnapshotRepository) -> None:
    """Changing setup away from recoverable WAL or unversioned schema must fail here."""
    with sqlite3.connect(repository.database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        versions = connection.execute("SELECT version FROM schema_versions").fetchall()

    assert journal_mode == ("wal",)
    assert versions == [(1,)]


def test_sqlite_timestamps_preserve_the_required_ist_offset(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Changing SQLite binding to discard timezone offsets must fail here."""
    raw_id = repository.save_raw(raw_payload)

    with sqlite3.connect(repository.database_path) as connection:
        saved_at = connection.execute(
            "SELECT saved_at FROM raw_snapshots WHERE snapshot_id = ?", (raw_id,)
        ).fetchone()

    assert saved_at is not None
    assert saved_at[0].endswith("+05:30")


def test_normalized_snapshot_is_read_in_chronological_order_after_reopen(
    tmp_path: Path, raw_payload: bytes
) -> None:
    """Changing read ordering or losing durable records during reopen must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    later = make_chain(timestamp="2026-08-28T09:16:00+05:30")
    earlier = make_chain(timestamp="2026-08-28T09:15:00+05:30")

    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root) as repository:
        raw_id = repository.save_raw(raw_payload)
        repository.save_normalized(raw_id, later)
        repository.save_normalized(raw_id, earlier)

    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root) as repository:
        snapshots = list(repository.iter_session("NIFTY", date(2026, 8, 28)))

    assert [snapshot.source_timestamp for snapshot in snapshots] == [
        earlier.source_timestamp,
        later.source_timestamp,
    ]


def test_normalized_snapshots_are_partitioned_by_instrument_and_session_date(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Changing Parquet layout or schema from canonical strike records must fail here."""
    snapshot: OptionChainSnapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = repository.save_raw(raw_payload)

    repository.save_normalized(raw_id, snapshot)

    partition = repository.parquet_root / "instrument=NIFTY" / "session_date=2026-08-28"
    parquet_files = list(partition.glob("*.parquet"))
    assert len(parquet_files) == 1


def test_session_read_fails_closed_when_indexed_parquet_is_corrupt(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Changing replay to trust a corrupt immutable artifact must fail here."""
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = repository.save_raw(raw_payload)
    repository.save_normalized(raw_id, snapshot)
    parquet_file = next(repository.parquet_root.rglob("*.parquet"))
    parquet_file.write_bytes(b"not a parquet file")

    with pytest.raises(RuntimeError, match="failed integrity verification"):
        list(repository.iter_session("NIFTY", date(2026, 8, 28)))


def test_concurrent_duplicate_normalized_write_keeps_one_replayable_artifact(
    tmp_path: Path, raw_payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing duplicate-conflict cleanup to remove a winner's file must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    first = SnapshotRepository(database_path=database_path, parquet_root=parquet_root)
    second = SnapshotRepository(database_path=database_path, parquet_root=parquet_root)
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = first.save_raw(raw_payload)
    second.save_raw(raw_payload)
    barrier = threading.Barrier(2)
    failures: list[Exception] = []

    def synchronize_write(repository: SnapshotRepository) -> None:
        original_write = repository._parquet.write

        def write_after_both_lookups(*args: object, **kwargs: object) -> Path:
            barrier.wait(timeout=5)
            return original_write(*args, **kwargs)

        monkeypatch.setattr(repository._parquet, "write", write_after_both_lookups)

    synchronize_write(first)
    synchronize_write(second)

    def save(repository: SnapshotRepository) -> None:
        try:
            repository.save_normalized(raw_id, snapshot)
        except Exception as error:  # noqa: BLE001 - assertion below inspects worker failures.
            failures.append(error)

    workers = [threading.Thread(target=save, args=(repository,)) for repository in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert len(list(first.iter_session("NIFTY", date(2026, 8, 28)))) == 1
