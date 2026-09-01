"""Integration contracts for immutable local market-data persistence."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path
from shutil import copyfile

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
    tmp_path: Path, raw_payload: bytes
) -> None:
    """Changing write locking to allow duplicate publications must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    first = SnapshotRepository(database_path=database_path, parquet_root=parquet_root)
    second = SnapshotRepository(database_path=database_path, parquet_root=parquet_root)
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = first.save_raw(raw_payload)
    second.save_raw(raw_payload)
    barrier = threading.Barrier(3)
    failures: list[Exception] = []

    def save(repository: SnapshotRepository) -> None:
        try:
            barrier.wait(timeout=5)
            repository.save_normalized(raw_id, snapshot)
        except Exception as error:  # noqa: BLE001 - assertion below inspects worker failures.
            failures.append(error)

    workers = [threading.Thread(target=save, args=(repository,)) for repository in (first, second)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert len(list(first.iter_session("NIFTY", date(2026, 8, 28)))) == 1


def test_startup_recovery_waits_for_a_writer_that_has_published_but_not_indexed(
    tmp_path: Path, raw_payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing recovery to race an active publisher must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    writer = SnapshotRepository(database_path=database_path, parquet_root=parquet_root)
    raw_id = writer.save_raw(raw_payload)
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    published = threading.Event()
    release_writer = threading.Event()
    startup_finished = threading.Event()
    failures: list[Exception] = []
    original_write = writer._parquet.write

    def pause_after_publication(*args: object, **kwargs: object) -> Path:
        result = original_write(*args, **kwargs)
        published.set()
        assert release_writer.wait(timeout=5)
        return result

    monkeypatch.setattr(writer._parquet, "write", pause_after_publication)

    def save() -> None:
        try:
            writer.save_normalized(raw_id, snapshot)
        except Exception as error:  # noqa: BLE001 - asserted after joining worker.
            failures.append(error)

    def reopen() -> None:
        try:
            with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
                startup_finished.set()
        except Exception as error:  # noqa: BLE001 - asserted after joining worker.
            failures.append(error)

    save_thread = threading.Thread(target=save)
    save_thread.start()
    assert published.wait(timeout=5)
    reopen_thread = threading.Thread(target=reopen)
    reopen_thread.start()
    assert not startup_finished.wait(timeout=0.2)
    release_writer.set()
    save_thread.join(timeout=10)
    reopen_thread.join(timeout=10)

    assert not failures
    assert len(list(writer.iter_session("NIFTY", date(2026, 8, 28)))) == 1


def test_replay_rejects_an_index_path_that_escapes_the_parquet_root(
    repository: SnapshotRepository, raw_payload: bytes, tmp_path: Path
) -> None:
    """Changing index-path validation to trust traversal text must fail here."""
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = repository.save_raw(raw_payload)
    repository.save_normalized(raw_id, snapshot)
    external_copy = tmp_path / "outside.parquet"
    copyfile(next(repository.parquet_root.rglob("*.parquet")), external_copy)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("UPDATE normalized_snapshots SET parquet_path = '../outside.parquet'")
        connection.commit()

    with pytest.raises(RuntimeError, match="unsafe Parquet index path"):
        list(repository.iter_session("NIFTY", date(2026, 8, 28)))

    assert external_copy.is_file()


def test_recovery_never_deletes_a_database_referenced_path_outside_its_root(
    repository: SnapshotRepository, raw_payload: bytes, tmp_path: Path
) -> None:
    """Recovery may clean contained orphans but must never follow a corrupt DB path."""
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = repository.save_raw(raw_payload)
    repository.save_normalized(raw_id, snapshot)
    external_file = tmp_path / "must-not-delete.parquet"
    external_file.write_bytes(b"protected")
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("UPDATE normalized_snapshots SET parquet_path = '../must-not-delete.parquet'")
        connection.commit()

    with SnapshotRepository(
        database_path=repository.database_path, parquet_root=repository.parquet_root
    ):
        pass

    assert external_file.read_bytes() == b"protected"


def test_replay_rejects_indexed_session_that_disagrees_with_source_timestamp(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Changing replay to trust a wrong indexed session must fail here."""
    snapshot = make_chain(timestamp="2026-08-28T09:15:00+05:30")
    raw_id = repository.save_raw(raw_payload)
    repository.save_normalized(raw_id, snapshot)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("UPDATE normalized_snapshots SET session_date = '2026-08-29'")
        connection.commit()

    with pytest.raises(RuntimeError, match="normalized snapshot index is inconsistent"):
        list(repository.iter_session("NIFTY", date(2026, 8, 29)))


def test_foreign_keys_are_enabled_on_every_pooled_connection(repository: SnapshotRepository) -> None:
    """Changing connection setup to configure only the bootstrap handle must fail here."""
    with repository._engine.connect() as first, repository._engine.connect() as second:
        first_foreign_keys = first.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        second_foreign_keys = second.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert first_foreign_keys == 1
    assert second_foreign_keys == 1


def test_existing_unversioned_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    """Changing startup to create tables in an unknown database must fail here."""
    database_path = tmp_path / "market.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE foreign_data (id INTEGER PRIMARY KEY)")
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database"):
        SnapshotRepository(database_path=database_path, parquet_root=tmp_path / "parquet")

    assert database_path.read_bytes() == before


def test_existing_newer_schema_version_is_rejected_without_mutation(tmp_path: Path) -> None:
    """Changing startup to accept an unknown newer schema must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (999, '2026-08-28T09:15:00+05:30')"
        )
        connection.commit()
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database"):
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root)

    assert database_path.read_bytes() == before


def test_existing_database_without_a_schema_version_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    """Changing startup to stamp an unknown existing schema as v1 must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM schema_versions")
        connection.commit()
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database"):
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root)

    assert database_path.read_bytes() == before


def test_raw_digest_collision_is_rejected_after_byte_comparison(
    repository: SnapshotRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing raw upsert to accept a digest collision must fail here."""
    monkeypatch.setattr("nifty_signal_engine.data.repositories._payload_digest", lambda _raw: "a" * 64)
    repository.save_raw(b'{"source":"first"}')

    with pytest.raises(RuntimeError, match="digest collision"):
        repository.save_raw(b'{"source":"second"}')
