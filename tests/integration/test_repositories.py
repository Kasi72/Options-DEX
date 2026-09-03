"""Integration contracts for immutable local market-data persistence."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path
from shutil import copyfile

import pytest

from nifty_signal_engine.data.parquet_store import (
    ParquetSnapshotStore,
    canonical_snapshot,
    snapshot_content_sha256,
)
from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)
from tests.factories import make_chain


@pytest.fixture
def repository(tmp_path: Path) -> SnapshotRepository:
    return SnapshotRepository(
        database_path=tmp_path / "market.sqlite3", parquet_root=tmp_path / "parquet"
    )


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


def test_schema_uses_wal_and_records_current_version(
    repository: SnapshotRepository,
) -> None:
    """Changing setup away from recoverable WAL or unversioned schema must fail here."""
    with sqlite3.connect(repository.database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        versions = connection.execute("SELECT version FROM schema_versions").fetchall()

    assert journal_mode == ("wal",)
    assert versions == [(3,)]


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

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        raw_id = repository.save_raw(raw_payload)
        repository.save_normalized(raw_id, later)
        repository.save_normalized(raw_id, earlier)

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
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

    workers = [
        threading.Thread(target=save, args=(repository,))
        for repository in (first, second)
    ]
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
            with SnapshotRepository(
                database_path=database_path, parquet_root=parquet_root
            ):
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
        connection.execute(
            "UPDATE normalized_snapshots SET parquet_path = '../outside.parquet'"
        )
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
        connection.execute(
            "UPDATE normalized_snapshots SET parquet_path = '../must-not-delete.parquet'"
        )
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
        connection.execute(
            "UPDATE normalized_snapshots SET session_date = '2026-08-29'"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="normalized snapshot index is inconsistent"):
        list(repository.iter_session("NIFTY", date(2026, 8, 29)))


def test_foreign_keys_are_enabled_on_every_pooled_connection(
    repository: SnapshotRepository,
) -> None:
    """Changing connection setup to configure only the bootstrap handle must fail here."""
    with repository._engine.connect() as first, repository._engine.connect() as second:
        first_foreign_keys = first.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        second_foreign_keys = second.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert first_foreign_keys == 1
    assert second_foreign_keys == 1


def test_existing_unversioned_database_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    """Changing startup to create tables in an unknown database must fail here."""
    database_path = tmp_path / "market.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE foreign_data (id INTEGER PRIMARY KEY)")
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database"):
        SnapshotRepository(
            database_path=database_path, parquet_root=tmp_path / "parquet"
        )

    assert database_path.read_bytes() == before


def test_existing_newer_schema_version_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
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
    monkeypatch.setattr(
        "nifty_signal_engine.data.repositories._payload_digest", lambda _raw: "a" * 64
    )
    repository.save_raw(b'{"source":"first"}')

    with pytest.raises(RuntimeError, match="digest collision"):
        repository.save_raw(b'{"source":"second"}')


def test_existing_schema_with_wrong_type_and_nullability_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    """Changing validation to accept nullable TEXT spot columns must fail here."""
    database_path, parquet_root = _create_v1_database(tmp_path)
    _rewrite_table_sql(
        database_path,
        "normalized_snapshots",
        "spot FLOAT NOT NULL",
        "spot TEXT",
    )
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database schema"):
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root)

    assert database_path.read_bytes() == before


def test_existing_schema_without_required_foreign_key_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    """Changing validation to accept unlinked normalized records must fail here."""
    database_path, parquet_root = _create_v1_database(tmp_path)
    _rewrite_table_sql(
        database_path,
        "normalized_snapshots",
        "FOREIGN KEY(raw_snapshot_id) REFERENCES raw_snapshots (snapshot_id)",
        "CHECK (1)",
    )
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database schema"):
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root)

    assert database_path.read_bytes() == before


def test_existing_schema_without_required_unique_index_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    """Changing validation to accept duplicate normalized identities must fail here."""
    database_path, parquet_root = _create_v1_database(tmp_path)
    _rewrite_table_sql(
        database_path,
        "normalized_snapshots",
        "CONSTRAINT uq_normalized_snapshot_content UNIQUE (raw_snapshot_id, content_sha256)",
        "CHECK (1)",
        remove_unique_index=True,
    )
    before = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported existing database schema"):
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root)

    assert database_path.read_bytes() == before


def test_simultaneous_first_initialization_installs_one_valid_v3_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing post-lock initialization to trust a stale pristine check must fail here."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    original_exists = Path.exists
    observed_missing = threading.Barrier(2)
    start = threading.Barrier(3)
    repositories: list[SnapshotRepository] = []
    failures: list[Exception] = []

    def synchronize_initial_exists(path: Path) -> bool:
        result = original_exists(path)
        if path == database_path:
            observed_missing.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "exists", synchronize_initial_exists)

    def initialize() -> None:
        try:
            start.wait(timeout=5)
            repositories.append(
                SnapshotRepository(
                    database_path=database_path, parquet_root=parquet_root
                )
            )
        except Exception as error:  # noqa: BLE001 - asserted after concurrent construction.
            failures.append(error)

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=10)
    for repository in repositories:
        repository.close()

    assert not failures
    assert len(repositories) == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_versions").fetchall() == [
            (3,)
        ]


def test_quality_decision_and_collector_baseline_survive_repository_restart(
    tmp_path: Path, raw_payload: bytes
) -> None:
    """Dropping audit/state writes must not make a restart silently baseline from nothing."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    snapshot = make_chain(
        timestamp="2026-08-28T10:00:00+05:30", call_volume=10, put_volume=10
    )
    report = DataQualityReport(
        tradable=False,
        codes=(),
        checked_at=snapshot.received_at,
        details={"baseline": "no_persisted_baseline"},
    )
    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        raw_id = repository.save_raw(raw_payload)
        repository.save_normalized(raw_id, snapshot)
        repository.record_quality_and_baseline(raw_id, snapshot, report, baseline=True)

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        restored = repository.load_collector_baseline("NIFTY")
        summary = repository.session_quality_summary("NIFTY", date(2026, 8, 28))

    assert restored == snapshot
    assert summary == {"snapshot_count": 1, "tradable_count": 0, "codes": {}}


def test_generic_normalized_publication_is_explicitly_nontradable(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    raw_id = repository.save_raw(raw_payload)
    snapshot = make_chain(timestamp="2026-08-28T10:00:00+05:30")

    repository.save_normalized(raw_id, snapshot)

    assert repository.session_quality_summary("NIFTY", date(2026, 8, 28)) == {
        "snapshot_count": 1,
        "tradable_count": 0,
        "codes": {"NOT_ASSESSED": 1},
    }
    assert list(repository.iter_tradable_session("NIFTY", date(2026, 8, 28))) == []


def test_quote_provenance_survives_normalized_roundtrip(
    repository: SnapshotRepository, raw_payload: bytes
) -> None:
    """Snapshot authority must not overwrite a deliberately untrusted quote time."""
    raw_id = repository.save_raw(raw_payload)
    snapshot = make_chain(timestamp="2026-08-28T10:00:00+05:30").model_copy(
        update={"source_time_authoritative": True}
    )
    snapshot = snapshot.model_copy(
        update={
            "quotes": tuple(
                quote.model_copy(update={"timestamp_authoritative": False})
                for quote in snapshot.quotes
            )
        }
    )

    repository.save_normalized(raw_id, snapshot)
    restored = next(repository.iter_session("NIFTY", date(2026, 8, 28)))

    assert restored.source_time_authoritative is True
    assert {quote.timestamp_authoritative for quote in restored.quotes} == {False}


def test_atomic_publish_rolls_back_index_when_quality_audit_fails(
    repository: SnapshotRepository, raw_payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between Parquet write and audit insert cannot publish a usable index."""
    raw_id = repository.save_raw(raw_payload)
    snapshot = make_chain(timestamp="2026-08-28T10:00:00+05:30")
    report = DataQualityReport(
        tradable=False,
        codes=(DataQualityCode.QUALITY_ASSESSMENT_FAILED,),
        checked_at=snapshot.received_at,
    )

    def fail_audit(*_args: object) -> None:
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(repository, "_insert_quality_decision_locked", fail_audit)
    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        repository.publish_normalized_with_quality_and_baseline(
            raw_id, snapshot, report, baseline=False
        )

    assert list(repository.iter_session("NIFTY", date(2026, 8, 28))) == []
    database_path = repository.database_path
    parquet_root = repository.parquet_root
    repository.close()
    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
        pass
    assert list(parquet_root.rglob("*.parquet")) == []


def test_exact_v1_database_is_migrated_to_v3_under_the_writer_lock(
    tmp_path: Path,
) -> None:
    """Rejecting an otherwise exact v1 store must not strand prior immutable snapshots."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        raw_id = repository.save_raw(b'{"migration":"raw"}')
        repository.save_normalized(
            raw_id, make_chain(timestamp="2026-08-28T10:00:00+05:30")
        )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE quality_decisions")
        connection.execute(
            "ALTER TABLE option_quotes DROP COLUMN timestamp_authoritative"
        )
        connection.execute(
            "ALTER TABLE normalized_snapshots DROP COLUMN serialization_version"
        )
        connection.execute(
            "ALTER TABLE normalized_snapshots DROP COLUMN source_time_authoritative"
        )
        connection.execute("DELETE FROM schema_versions")
        connection.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (1, '2026-08-28T09:15:00+05:30')"
        )
        connection.commit()

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        summary = repository.session_quality_summary("NIFTY", date(2026, 8, 28))

    with sqlite3.connect(database_path) as connection:
        versions = connection.execute("SELECT version FROM schema_versions").fetchall()
        columns = connection.execute(
            "PRAGMA table_info(normalized_snapshots)"
        ).fetchall()
        quality_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'quality_decisions'"
        ).fetchone()

    assert versions == [(3,)]
    assert any(column[1] == "source_time_authoritative" for column in columns)
    assert any(column[1] == "serialization_version" for column in columns)
    assert quality_table == ("quality_decisions",)
    assert summary["codes"] == {"MIGRATED_UNASSESSED": 1}


def test_historical_v1_parquet_hash_and_path_replay_after_migration(
    tmp_path: Path,
) -> None:
    """A real v1 serialization omits provenance fields yet remains strictly verifiable."""
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    snapshot = canonical_snapshot(make_chain(timestamp="2026-08-28T10:00:00+05:30"))
    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        raw_id = repository.save_raw(b'{"historical":"v1"}')

    legacy_store = ParquetSnapshotStore(parquet_root)
    legacy_path = legacy_store.write(raw_id, snapshot, serialization_version=1)
    legacy_hash = snapshot_content_sha256(snapshot, serialization_version=1)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE quality_decisions")
        connection.execute(
            "ALTER TABLE option_quotes DROP COLUMN timestamp_authoritative"
        )
        connection.execute(
            "ALTER TABLE normalized_snapshots DROP COLUMN serialization_version"
        )
        connection.execute(
            "ALTER TABLE normalized_snapshots DROP COLUMN source_time_authoritative"
        )
        connection.execute(
            "INSERT INTO normalized_snapshots "
            "(raw_snapshot_id, content_sha256, instrument, session_date, "
            "source_timestamp, received_at, spot, expiry, parquet_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                raw_id,
                legacy_hash,
                snapshot.instrument,
                snapshot.source_timestamp.date().isoformat(),
                snapshot.source_timestamp.isoformat(),
                snapshot.received_at.isoformat(),
                snapshot.spot,
                snapshot.expiry.isoformat(),
                legacy_path.as_posix(),
            ),
        )
        normalized_id = connection.execute(
            "SELECT id FROM normalized_snapshots"
        ).fetchone()[0]
        for ordinal, quote in enumerate(snapshot.quotes):
            connection.execute(
                "INSERT INTO option_quotes "
                "(normalized_snapshot_id, ordinal, timestamp, strike, option_type, expiry, "
                "ltp, bid, ask, volume, oi, previous_oi, iv, api_delta, api_gamma) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    normalized_id,
                    ordinal,
                    quote.timestamp.isoformat(),
                    quote.strike,
                    quote.option_type,
                    quote.expiry.isoformat() if quote.expiry else None,
                    quote.ltp,
                    quote.bid,
                    quote.ask,
                    quote.volume,
                    quote.oi,
                    quote.previous_oi,
                    quote.iv,
                    quote.api_delta,
                    quote.api_gamma,
                ),
            )
        connection.execute("DELETE FROM schema_versions")
        connection.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (1, ?) ",
            (snapshot.received_at.isoformat(),),
        )
        connection.commit()

    with SnapshotRepository(
        database_path=database_path, parquet_root=parquet_root
    ) as repository:
        restored = next(repository.iter_session("NIFTY", date(2026, 8, 28)))
        summary = repository.session_quality_summary("NIFTY", date(2026, 8, 28))

    assert restored == snapshot
    assert summary["codes"] == {"MIGRATED_UNASSESSED": 1}


def test_exact_v2_database_gets_a_versioned_quote_provenance_migration(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
        pass
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE option_quotes DROP COLUMN timestamp_authoritative"
        )
        connection.execute(
            "ALTER TABLE normalized_snapshots DROP COLUMN serialization_version"
        )
        connection.execute("DELETE FROM schema_versions")
        connection.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (2, ?) ",
            ("2026-08-28T09:15:00+05:30",),
        )
        connection.commit()

    with (
        SnapshotRepository(database_path=database_path, parquet_root=parquet_root) as repository,
        sqlite3.connect(repository.database_path) as connection,
    ):
        version = connection.execute("SELECT version FROM schema_versions").fetchall()
        quote_columns = connection.execute("PRAGMA table_info(option_quotes)").fetchall()
        snapshot_columns = connection.execute(
            "PRAGMA table_info(normalized_snapshots)"
        ).fetchall()

    assert version == [(3,)]
    assert any(column[1] == "timestamp_authoritative" for column in quote_columns)
    assert any(column[1] == "serialization_version" for column in snapshot_columns)


def _create_v1_database(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "market.sqlite3"
    parquet_root = tmp_path / "parquet"
    with SnapshotRepository(database_path=database_path, parquet_root=parquet_root):
        pass
    return database_path, parquet_root


def _rewrite_table_sql(
    database_path: Path,
    table_name: str,
    expected_fragment: str,
    replacement: str,
    *,
    remove_unique_index: bool = False,
) -> None:
    """Create an incompatible DB fixture without invoking application migration code."""
    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        assert table_sql is not None
        assert expected_fragment in table_sql[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (table_sql[0].replace(expected_fragment, replacement), table_name),
        )
        if remove_unique_index:
            unique_indexes = connection.execute(
                f'PRAGMA index_list("{table_name}")'
            ).fetchall()
            unique_index = next(
                index for index in unique_indexes if index[2] == 1 and index[3] == "u"
            )
            connection.execute(
                "DELETE FROM sqlite_master WHERE type = 'index' AND name = ?",
                (unique_index[1],),
            )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()
        assert schema_version is not None
        connection.execute(f"PRAGMA schema_version = {schema_version[0] + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
