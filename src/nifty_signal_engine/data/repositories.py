"""Recoverable local repository for immutable raw and normalized snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
import zlib
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal, NewType, Self, cast
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Connection,
    MetaData,
    create_engine,
    delete,
    event,
    insert,
    select,
)

from nifty_signal_engine.data.parquet_store import (
    ParquetSnapshotStore,
    canonical_snapshot,
    snapshot_content_sha256,
)
from nifty_signal_engine.data.schema import (
    SCHEMA_VERSION,
    metadata,
    normalized_snapshots,
    option_quotes,
    quality_decisions,
    raw_snapshots,
    schema_applied_at,
    schema_versions,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)

IST: Final = ZoneInfo("Asia/Kolkata")
SnapshotId = NewType("SnapshotId", str)
SchemaContract = tuple[
    tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]], ...
]


@lru_cache
def _expected_v3_schema_contract() -> SchemaContract:
    """Derive the current SQLite contract from the code-defined metadata."""
    return _expected_schema_contract(metadata)


@lru_cache
def _expected_v1_schema_contract() -> SchemaContract:
    """Reconstruct the exact supported v1 schema before its locked migration."""
    legacy_tables: list[
        tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]]
    ] = []
    for table_name, columns, indexes, foreign_keys in _expected_v3_schema_contract():
        if table_name == "quality_decisions":
            continue
        if table_name == "normalized_snapshots":
            columns = tuple(
                column
                for column in columns
                if cast(tuple[object, ...], column)[0]
                not in {"source_time_authoritative", "serialization_version"}
            )
        if table_name == "option_quotes":
            columns = tuple(
                column
                for column in columns
                if cast(tuple[object, ...], column)[0] != "timestamp_authoritative"
            )
        legacy_tables.append((table_name, columns, indexes, foreign_keys))
    return tuple(legacy_tables)


@lru_cache
def _expected_v2_schema_contract(*, migrated_from_v1: bool = False) -> SchemaContract:
    """Reconstruct exact v2 layouts before the quote-provenance migration."""
    migrated: list[
        tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]]
    ] = []
    for table_name, columns, indexes, foreign_keys in _expected_v3_schema_contract():
        if table_name == "normalized_snapshots":
            source_column = next(
                cast(tuple[object, ...], column)
                for column in columns
                if cast(tuple[object, ...], column)[0] == "source_time_authoritative"
            )
            if migrated_from_v1:
                source_column = (*cast(tuple[object, ...], source_column)[:-1], "0")
            columns = tuple(
                column
                for column in columns
                if cast(tuple[object, ...], column)[0]
                not in {"source_time_authoritative", "serialization_version"}
            )
            if migrated_from_v1:
                columns += (source_column,)
            else:
                parquet_column = next(
                    column
                    for column in columns
                    if cast(tuple[object, ...], column)[0] == "parquet_path"
                )
                columns = tuple(column for column in columns if column != parquet_column)
                columns += (source_column, parquet_column)
        if table_name == "option_quotes":
            columns = tuple(
                column
                for column in columns
                if cast(tuple[object, ...], column)[0] != "timestamp_authoritative"
            )
        migrated.append((table_name, columns, indexes, foreign_keys))
    return tuple(migrated)


@lru_cache
def _expected_migrated_v3_schema_contract(
    source_version: int, migrated_v2: bool = False
) -> SchemaContract:
    """Accept only the deterministic ALTER TABLE layouts emitted by migrations."""
    if source_version == 1:
        tables = list(_expected_v1_schema_contract())
        final_quality = next(
            table for table in _expected_v3_schema_contract() if table[0] == "quality_decisions"
        )
        tables.append(final_quality)
    elif source_version == 2:
        tables = list(_expected_v2_schema_contract(migrated_from_v1=migrated_v2))
    else:
        raise ValueError("unsupported migration source version")
    final = _expected_v3_schema_contract()
    final_normalized = cast(
        tuple[str, tuple[tuple[object, ...], ...], tuple[object, ...], tuple[object, ...]],
        next(table for table in final if cast(str, table[0]) == "normalized_snapshots"),
    )
    final_quotes = cast(
        tuple[str, tuple[tuple[object, ...], ...], tuple[object, ...], tuple[object, ...]],
        next(table for table in final if cast(str, table[0]) == "option_quotes"),
    )
    source_column = next(
        column for column in final_normalized[1] if column[0] == "source_time_authoritative"
    )
    serialization_column = next(
        column for column in final_normalized[1] if column[0] == "serialization_version"
    )
    quote_authority_column = next(
        column for column in final_quotes[1] if column[0] == "timestamp_authoritative"
    )
    transformed: list[
        tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]]
    ] = []
    for table_name, columns, indexes, foreign_keys in tables:
        if table_name == "normalized_snapshots":
            if source_version == 1:
                columns += ((*source_column[:-1], "0"),)
            serialization_default = "1" if source_version == 1 else "2"
            columns += ((*serialization_column[:-1], serialization_default),)
        if table_name == "option_quotes":
            columns += ((*quote_authority_column[:-1], "0"),)
        transformed.append((table_name, columns, indexes, foreign_keys))
    return tuple(sorted(transformed, key=lambda table: table[0]))


def _expected_schema_contract(schema: MetaData) -> SchemaContract:
    """Materialize an expected contract in a private in-memory SQLite database."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            schema.create_all(connection)
            return _schema_contract(connection, schema)
    finally:
        engine.dispose()


def _schema_contract(
    connection: Connection,
    schema: MetaData = metadata,
    *,
    table_names: Iterable[str] | None = None,
) -> SchemaContract:
    """Return stable SQLite PRAGMA metadata for all application tables."""
    tables: list[
        tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]]
    ] = []
    names = sorted(schema.tables if table_names is None else table_names)
    for table_name in names:
        columns = tuple(
            (
                cast(str, row[1]),
                cast(str, row[2]).upper(),
                int(row[3]),
                int(row[5]),
                None if row[4] is None else str(row[4]),
            )
            for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
        )
        indexes = tuple(
            sorted(
                (
                    (
                        cast(str, index[1]),
                        int(index[2]),
                        cast(str, index[3]),
                        int(index[4]),
                        tuple(
                            (
                                int(column[0]),
                                int(column[1]),
                                cast(str | None, column[2]),
                            )
                            for column in connection.exec_driver_sql(
                                f'PRAGMA index_info("{cast(str, index[1])}")'
                            )
                        ),
                    )
                    for index in connection.exec_driver_sql(
                        f'PRAGMA index_list("{table_name}")'
                    )
                ),
                key=repr,
            )
        )
        foreign_keys = tuple(
            sorted(
                (
                    (
                        int(foreign_key[0]),
                        int(foreign_key[1]),
                        cast(str, foreign_key[2]),
                        cast(str, foreign_key[3]),
                        cast(str, foreign_key[4]),
                        cast(str, foreign_key[5]),
                        cast(str, foreign_key[6]),
                        cast(str, foreign_key[7]),
                    )
                    for foreign_key in connection.exec_driver_sql(
                        f'PRAGMA foreign_key_list("{table_name}")'
                    )
                ),
                key=repr,
            )
        )
        tables.append((table_name, columns, indexes, foreign_keys))
    return tuple(tables)


def _payload_digest(payload: bytes) -> str:
    """Return the content-address used for immutable raw snapshot identity."""
    return hashlib.sha256(payload).hexdigest()


class SnapshotRepository:
    """Persist immutable bytes first, then safely publish normalized snapshot indexes."""

    def __init__(self, *, database_path: Path, parquet_root: Path) -> None:
        self.database_path = database_path
        self.parquet_root = parquet_root
        self._database_existed = database_path.exists()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{database_path}", connect_args={"timeout": 30}
        )
        event.listen(self._engine, "connect", self._configure_sqlite_connection)
        self._parquet = ParquetSnapshotStore(parquet_root)
        self._initialize_and_recover()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, _exc_type: object, _exc_value: object, _traceback: object
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release SQLite resources; records remain durable for a later process."""
        self._engine.dispose()

    def save_raw(self, raw: bytes) -> SnapshotId:
        """Content-address raw bytes, rejecting any digest collision instead of overwriting."""
        payload = bytes(raw)
        snapshot_id = SnapshotId(_payload_digest(payload))
        with self._write_lock() as connection:
            existing = connection.execute(
                select(raw_snapshots.c.compressed_payload).where(
                    raw_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    insert(raw_snapshots).values(
                        snapshot_id=snapshot_id,
                        sha256=snapshot_id,
                        compressed_payload=zlib.compress(payload),
                        saved_at=datetime.now(IST),
                    )
                )
                return snapshot_id
            if self._decompress_raw(snapshot_id, existing) != payload:
                raise RuntimeError(f"raw snapshot digest collision for {snapshot_id}")
        return snapshot_id

    def read_raw(self, snapshot_id: SnapshotId) -> bytes:
        """Read and verify exact immutable raw bytes, failing closed on corruption."""
        with self._engine.connect() as connection:
            compressed_payload = connection.execute(
                select(raw_snapshots.c.compressed_payload).where(
                    raw_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        if compressed_payload is None:
            raise KeyError(snapshot_id)
        return self._decompress_raw(snapshot_id, compressed_payload)

    def save_normalized(
        self, snapshot_id: SnapshotId, snapshot: OptionChainSnapshot
    ) -> None:
        """Publish only with an explicit fail-closed pending quality decision."""
        self.read_raw(snapshot_id)
        with self._write_lock() as connection:
            normalized_id = self._publish_normalized_locked(
                connection, snapshot_id, snapshot
            )
            if not self._has_quality_decision(connection, normalized_id):
                self._insert_quality_decision_locked(
                    connection,
                    normalized_id,
                    DataQualityReport(
                        tradable=False,
                        codes=(DataQualityCode.NOT_ASSESSED,),
                        checked_at=snapshot.received_at,
                        details={"quality": "pending_explicit_assessment"},
                    ),
                )

    def publish_normalized_with_quality_and_baseline(
        self,
        snapshot_id: SnapshotId,
        snapshot: OptionChainSnapshot,
        report: DataQualityReport,
        *,
        baseline: bool,
    ) -> DataQualityReport:
        """Atomically publish normalized data, exactly one audit, and baseline state."""
        self.read_raw(snapshot_id)
        with self._write_lock() as connection:
            normalized_id = self._publish_normalized_locked(
                connection, snapshot_id, snapshot
            )
            if self._has_quality_decision(connection, normalized_id):
                # Exact re-observation is idempotent: retain the original audit
                # and never move a baseline backwards.
                return self._stored_quality_report(connection, normalized_id)
            self._insert_quality_decision_locked(connection, normalized_id, report)
            if baseline:
                self._update_baseline_locked(
                    connection, snapshot.instrument, normalized_id, report.checked_at
                )
            return report

    def _publish_normalized_locked(
        self,
        connection: Connection,
        snapshot_id: SnapshotId,
        snapshot: OptionChainSnapshot,
    ) -> int:
        """Write Parquet and its indexes while the caller owns the writer transaction."""
        canonical = canonical_snapshot(snapshot)
        serialization_version = 3
        content_sha256 = snapshot_content_sha256(
            canonical, serialization_version=serialization_version
        )
        expected_path = self._parquet._relative_path(
            snapshot_id,
            canonical,
            canonical.source_timestamp.date(),
            serialization_version=serialization_version,
        )
        existing = self._normalized_record(connection, snapshot_id, content_sha256)
        if existing is not None:
            self._verify_existing_publication(
                existing, snapshot_id, canonical, expected_path
            )
            return cast(int, existing["id"])

        published = False
        try:
            relative_path = self._parquet.write(
                snapshot_id, canonical, serialization_version=serialization_version
            )
            if relative_path != expected_path:
                raise RuntimeError("Parquet writer produced an unexpected partition path")
            published = True
            result = connection.execute(
                insert(normalized_snapshots)
                .values(
                    raw_snapshot_id=snapshot_id,
                    content_sha256=content_sha256,
                    instrument=canonical.instrument,
                    session_date=canonical.source_timestamp.date(),
                    source_timestamp=canonical.source_timestamp,
                    received_at=canonical.received_at,
                    spot=canonical.spot,
                    expiry=canonical.expiry,
                    source_time_authoritative=canonical.source_time_authoritative,
                    serialization_version=serialization_version,
                    parquet_path=relative_path.as_posix(),
                )
                .returning(normalized_snapshots.c.id)
            )
            normalized_id = cast(int, result.scalar_one())
            connection.execute(
                insert(option_quotes),
                [
                    {
                        "normalized_snapshot_id": normalized_id,
                        "ordinal": ordinal,
                        "timestamp": quote.timestamp,
                        "timestamp_authoritative": quote.timestamp_authoritative,
                        "strike": quote.strike,
                        "option_type": quote.option_type,
                        "expiry": quote.expiry,
                        "ltp": quote.ltp,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "volume": quote.volume,
                        "oi": quote.oi,
                        "previous_oi": quote.previous_oi,
                        "iv": quote.iv,
                        "api_delta": quote.api_delta,
                        "api_gamma": quote.api_gamma,
                    }
                    for ordinal, quote in enumerate(canonical.quotes)
                ],
            )
            return normalized_id
        except Exception:
            if published:
                self._remove_attempt_publication(expected_path)
            raise

    @staticmethod
    def _has_quality_decision(connection: Connection, normalized_id: int) -> bool:
        return (
            connection.execute(
                select(quality_decisions.c.id).where(
                    quality_decisions.c.normalized_snapshot_id == normalized_id
                )
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _stored_quality_report(
        connection: Connection, normalized_id: int
    ) -> DataQualityReport:
        rows = list(
            connection.execute(
                select(
                    quality_decisions.c.tradable,
                    quality_decisions.c.codes,
                    quality_decisions.c.details,
                ).where(quality_decisions.c.normalized_snapshot_id == normalized_id)
            ).mappings()
        )
        if len(rows) != 1:
            raise RuntimeError("normalized snapshot has inconsistent quality decisions")
        checked_at = connection.execute(
            select(quality_decisions.c.checked_at).where(
                quality_decisions.c.normalized_snapshot_id == normalized_id
            )
        ).scalar_one()
        recorded_codes = rows[0]["codes"]
        if not isinstance(recorded_codes, list) or not isinstance(
            rows[0]["details"], dict
        ):
            raise RuntimeError("normalized snapshot has invalid quality decision")
        return DataQualityReport(
            tradable=bool(rows[0]["tradable"]),
            codes=tuple(DataQualityCode(code) for code in recorded_codes),
            checked_at=cast(datetime, checked_at),
            details=cast(dict[str, str], rows[0]["details"]),
        )

    @staticmethod
    def _insert_quality_decision_locked(
        connection: Connection, normalized_id: int, report: DataQualityReport
    ) -> None:
        connection.execute(
            insert(quality_decisions).values(
                normalized_snapshot_id=normalized_id,
                tradable=report.tradable,
                codes=[str(code) for code in report.codes],
                details=report.details,
                checked_at=report.checked_at,
            )
        )

    @staticmethod
    def _update_baseline_locked(
        connection: Connection, instrument: str, normalized_id: int, checked_at: datetime
    ) -> None:
        state_table = metadata.tables["collector_state"]
        connection.execute(
            delete(state_table).where(state_table.c.instrument == instrument)
        )
        connection.execute(
            insert(state_table).values(
                instrument=instrument,
                state={"normalized_snapshot_id": normalized_id},
                updated_at=checked_at,
            )
        )

    def iter_session(
        self, instrument: str, session_date: date
    ) -> Iterator[OptionChainSnapshot]:
        """Yield full snapshots in deterministic chronological order for one instrument session."""
        if instrument not in {"NIFTY", "BANKNIFTY"}:
            raise ValueError(f"unsupported instrument: {instrument}")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(normalized_snapshots)
                .where(
                    normalized_snapshots.c.instrument == instrument,
                    normalized_snapshots.c.session_date == session_date,
                )
                .order_by(
                    normalized_snapshots.c.source_timestamp,
                    normalized_snapshots.c.received_at,
                    normalized_snapshots.c.id,
                )
            ).mappings()
            for row in rows:
                typed_row = cast(Mapping[str, Any], row)
                if not self._has_quality_decision(connection, cast(int, typed_row["id"])):
                    raise RuntimeError("normalized snapshot is missing a quality decision")
                quote_rows = connection.execute(
                    select(option_quotes)
                    .where(option_quotes.c.normalized_snapshot_id == typed_row["id"])
                    .order_by(option_quotes.c.ordinal)
                ).mappings()
                snapshot = self._snapshot_from_rows(
                    typed_row,
                    (cast(Mapping[str, Any], quote_row) for quote_row in quote_rows),
                )
                relative_path = self._validated_index_path(typed_row, snapshot)
                self._parquet.verify(
                    relative_path,
                    cast(str, typed_row["raw_snapshot_id"]),
                    snapshot,
                    serialization_version=self._serialization_version(typed_row),
                )
                yield snapshot

    def iter_tradable_session(
        self, instrument: str, session_date: date
    ) -> Iterator[OptionChainSnapshot]:
        """Yield only snapshots with an explicit positive audit decision."""
        if instrument not in {"NIFTY", "BANKNIFTY"}:
            raise ValueError(f"unsupported instrument: {instrument}")
        with self._engine.connect() as connection:
            tradable_ids = set(
                connection.execute(
                    select(quality_decisions.c.normalized_snapshot_id).where(
                        quality_decisions.c.tradable.is_(True)
                    )
                ).scalars()
            )
            rows = connection.execute(
                select(normalized_snapshots)
                .where(
                    normalized_snapshots.c.instrument == instrument,
                    normalized_snapshots.c.session_date == session_date,
                    normalized_snapshots.c.id.in_(tradable_ids),
                )
                .order_by(
                    normalized_snapshots.c.source_timestamp,
                    normalized_snapshots.c.received_at,
                    normalized_snapshots.c.id,
                )
            ).mappings()
            for row in rows:
                typed_row = cast(Mapping[str, Any], row)
                quote_rows = connection.execute(
                    select(option_quotes)
                    .where(option_quotes.c.normalized_snapshot_id == typed_row["id"])
                    .order_by(option_quotes.c.ordinal)
                ).mappings()
                snapshot = self._snapshot_from_rows(
                    typed_row,
                    (cast(Mapping[str, Any], quote_row) for quote_row in quote_rows),
                )
                relative_path = self._validated_index_path(typed_row, snapshot)
                self._parquet.verify(
                    relative_path,
                    cast(str, typed_row["raw_snapshot_id"]),
                    snapshot,
                    serialization_version=self._serialization_version(typed_row),
                )
                yield snapshot

    def record_quality_and_baseline(
        self,
        snapshot_id: SnapshotId,
        snapshot: OptionChainSnapshot,
        report: DataQualityReport,
        *,
        baseline: bool,
    ) -> None:
        """Compatibility path that replaces only an explicit pending assessment."""
        canonical = canonical_snapshot(snapshot)
        content_sha256 = snapshot_content_sha256(canonical, serialization_version=3)
        with self._write_lock() as connection:
            record = self._normalized_record(connection, snapshot_id, content_sha256)
            if record is None:
                raise RuntimeError("quality requires a persisted normalized snapshot")
            normalized_id = cast(int, record["id"])
            existing = list(
                connection.execute(
                    select(quality_decisions.c.codes).where(
                        quality_decisions.c.normalized_snapshot_id == normalized_id
                    )
                ).scalars()
            )
            if existing and existing != [[str(DataQualityCode.NOT_ASSESSED)]]:
                raise RuntimeError("normalized snapshot already has a quality decision")
            if existing:
                connection.execute(
                    delete(quality_decisions).where(
                        quality_decisions.c.normalized_snapshot_id == normalized_id
                    )
                )
            self._insert_quality_decision_locked(connection, normalized_id, report)
            if baseline:
                self._update_baseline_locked(
                    connection, snapshot.instrument, normalized_id, report.checked_at
                )

    def load_collector_baseline(self, instrument: str) -> OptionChainSnapshot | None:
        """Restore the last persisted in-order baseline for exactly one instrument."""
        if instrument not in {"NIFTY", "BANKNIFTY"}:
            raise ValueError(f"unsupported instrument: {instrument}")
        state_table = metadata.tables["collector_state"]
        with self._engine.connect() as connection:
            state = connection.execute(
                select(state_table.c.state).where(
                    state_table.c.instrument == instrument
                )
            ).scalar_one_or_none()
            if not isinstance(state, dict) or not isinstance(
                state.get("normalized_snapshot_id"), int
            ):
                return None
            record = (
                connection.execute(
                    select(normalized_snapshots).where(
                        normalized_snapshots.c.id == state["normalized_snapshot_id"]
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                raise RuntimeError(
                    "collector state references a missing normalized snapshot"
                )
            row = cast(Mapping[str, Any], record)
            if row["instrument"] != instrument:
                raise RuntimeError("collector state instrument mismatch")
            normalized_id = cast(int, row["id"])
            if not self._has_quality_decision(connection, normalized_id):
                raise RuntimeError("collector state references unassessed normalized snapshot")
            quotes = connection.execute(
                select(option_quotes)
                .where(option_quotes.c.normalized_snapshot_id == row["id"])
                .order_by(option_quotes.c.ordinal)
            ).mappings()
            snapshot = self._snapshot_from_rows(
                row, (cast(Mapping[str, Any], quote) for quote in quotes)
            )
            relative_path = self._validated_index_path(row, snapshot)
            self._parquet.verify(
                relative_path,
                cast(str, row["raw_snapshot_id"]),
                snapshot,
                serialization_version=self._serialization_version(row),
            )
            return snapshot

    def session_quality_summary(
        self, instrument: str, selected_date: date
    ) -> dict[str, object]:
        """Return audit counts for inspection without exposing raw payloads or credentials."""
        with self._engine.connect() as connection:
            snapshot_ids = list(
                connection.execute(
                    select(normalized_snapshots.c.id).where(
                        normalized_snapshots.c.instrument == instrument,
                        normalized_snapshots.c.session_date == selected_date,
                    )
                ).scalars()
            )
            if not snapshot_ids:
                return {"snapshot_count": 0, "tradable_count": 0, "codes": {}}
            decisions = connection.execute(
                select(quality_decisions.c.tradable, quality_decisions.c.codes).where(
                    quality_decisions.c.normalized_snapshot_id.in_(snapshot_ids)
                )
            )
            tradable_count = 0
            codes: dict[str, int] = {}
            for tradable, recorded_codes in decisions:
                tradable_count += int(bool(tradable))
                if isinstance(recorded_codes, list):
                    for code in recorded_codes:
                        if isinstance(code, str):
                            codes[code] = codes.get(code, 0) + 1
            # There should be exactly one decision per snapshot. If storage has
            # been externally damaged, surface it as an explicit fail-closed code.
            decision_count = len(
                list(
                    connection.execute(
                        select(quality_decisions.c.id).where(
                            quality_decisions.c.normalized_snapshot_id.in_(snapshot_ids)
                        )
                    ).scalars()
                )
            )
            if decision_count != len(snapshot_ids):
                codes[str(DataQualityCode.QUALITY_MISSING)] = max(
                    0, len(snapshot_ids) - decision_count
                )
        return {
            "snapshot_count": len(snapshot_ids),
            "tradable_count": tradable_count,
            "codes": codes,
        }

    @staticmethod
    def _configure_sqlite_connection(
        dbapi_connection: sqlite3.Connection, _connection_record: object
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    def _initialize_and_recover(self) -> None:
        if self._database_existed:
            with self._write_lock() as connection:
                self._migrate_schema_if_needed(connection)
                self._assert_supported_existing_database(connection)
                self._backfill_missing_quality_locked(connection)
                self._recover_orphaned_parquet_locked(connection)
            return

        with self._engine.connect() as connection:
            journal_mode = connection.exec_driver_sql(
                "PRAGMA journal_mode=WAL"
            ).scalar_one()
            if str(journal_mode).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
        with self._write_lock() as connection:
            if self._user_table_names(connection):
                self._migrate_schema_if_needed(connection)
                self._assert_supported_existing_database(connection)
            else:
                metadata.create_all(connection)
                connection.execute(
                    insert(schema_versions).values(
                        version=SCHEMA_VERSION, applied_at=schema_applied_at()
                    )
                )
                self._assert_supported_existing_database(connection)
            self._backfill_missing_quality_locked(connection)
            self._recover_orphaned_parquet_locked(connection)

    @contextmanager
    def _write_lock(self) -> Generator[Connection, None, None]:
        """Hold SQLite's cross-process writer lock over publication, indexing, and recovery."""
        connection = self._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _assert_supported_existing_database(self, connection: Connection) -> None:
        if self._user_table_names(connection) != set(metadata.tables):
            raise RuntimeError("unsupported existing database schema")
        versions = list(connection.execute(select(schema_versions.c.version)).scalars())
        if versions != [SCHEMA_VERSION]:
            raise RuntimeError("unsupported existing database schema version")
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        if str(journal_mode).lower() != "wal":
            raise RuntimeError("unsupported existing database journal mode")
        if _schema_contract(connection) not in {
            _expected_v3_schema_contract(),
            _expected_migrated_v3_schema_contract(1),
            _expected_migrated_v3_schema_contract(2),
            _expected_migrated_v3_schema_contract(2, migrated_v2=True),
        }:
            raise RuntimeError("unsupported existing database schema")

    def _migrate_schema_if_needed(self, connection: Connection) -> None:
        """Upgrade only exact historical layouts inside the existing writer lock."""
        if "schema_versions" not in self._user_table_names(connection):
            return
        versions = list(connection.execute(select(schema_versions.c.version)).scalars())
        if versions == [SCHEMA_VERSION]:
            return
        if versions == [1]:
            if (
                _schema_contract(connection, table_names=self._user_table_names(connection))
                != _expected_v1_schema_contract()
            ):
                raise RuntimeError("unsupported existing database schema")
            connection.exec_driver_sql(
                "ALTER TABLE normalized_snapshots "
                "ADD COLUMN source_time_authoritative BOOLEAN NOT NULL DEFAULT 0"
            )
            quality_decisions.create(connection)
            connection.exec_driver_sql(
                "ALTER TABLE option_quotes "
                "ADD COLUMN timestamp_authoritative BOOLEAN NOT NULL DEFAULT 0"
            )
            connection.exec_driver_sql(
                "ALTER TABLE normalized_snapshots "
                "ADD COLUMN serialization_version INTEGER NOT NULL DEFAULT 1"
            )
            self._backfill_missing_quality_locked(
                connection, code=DataQualityCode.MIGRATED_UNASSESSED
            )
        elif versions == [2]:
            contract = _schema_contract(
                connection, table_names=self._user_table_names(connection)
            )
            if contract not in {
                _expected_v2_schema_contract(),
                _expected_v2_schema_contract(migrated_from_v1=True),
            }:
                raise RuntimeError("unsupported existing database schema")
            connection.exec_driver_sql(
                "ALTER TABLE option_quotes "
                "ADD COLUMN timestamp_authoritative BOOLEAN NOT NULL DEFAULT 0"
            )
            connection.exec_driver_sql(
                "ALTER TABLE normalized_snapshots "
                "ADD COLUMN serialization_version INTEGER NOT NULL DEFAULT 2"
            )
        else:
            raise RuntimeError("unsupported existing database schema version")
        connection.execute(delete(schema_versions))
        connection.execute(
            insert(schema_versions).values(
                version=SCHEMA_VERSION, applied_at=schema_applied_at()
            )
        )

    def _backfill_missing_quality_locked(
        self,
        connection: Connection,
        *,
        code: DataQualityCode = DataQualityCode.QUALITY_MISSING,
    ) -> None:
        """Make old/corrupt index rows explicitly non-tradable rather than usable."""
        missing = connection.execute(
            select(normalized_snapshots.c.id, normalized_snapshots.c.received_at)
            .where(
                ~normalized_snapshots.c.id.in_(
                    select(quality_decisions.c.normalized_snapshot_id)
                )
            )
        )
        for normalized_id, received_at in missing:
            self._insert_quality_decision_locked(
                connection,
                cast(int, normalized_id),
                DataQualityReport(
                    tradable=False,
                    codes=(code,),
                    checked_at=cast(datetime, received_at),
                    details={"quality": "migration_or_recovery_backfill"},
                ),
            )

    @staticmethod
    def _user_table_names(connection: Connection) -> set[str]:
        return {
            cast(str, row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _recover_orphaned_parquet_locked(self, connection: Connection) -> None:
        """Remove only contained, unindexed files after obtaining the writer lock."""
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        root = self.parquet_root.resolve()
        known_paths: set[Path] = set()
        for path_text in connection.execute(
            select(normalized_snapshots.c.parquet_path)
        ).scalars():
            try:
                relative_path = self._safe_relative_path(cast(str, path_text))
            except RuntimeError:
                continue
            known_paths.add(relative_path)
        for candidate in self.parquet_root.rglob("*"):
            if not candidate.is_file() or candidate.suffix not in {".parquet", ".tmp"}:
                continue
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
                relative_path = candidate.relative_to(self.parquet_root)
            except (OSError, ValueError):
                continue
            if candidate.suffix == ".tmp" or relative_path not in known_paths:
                candidate.unlink(missing_ok=True)

    def _verify_existing_publication(
        self,
        row: Mapping[str, Any],
        snapshot_id: SnapshotId,
        snapshot: OptionChainSnapshot,
        expected_path: Path,
    ) -> None:
        serialization_version = self._serialization_version(row)
        if (
            row["instrument"] != snapshot.instrument
            or row["session_date"] != snapshot.source_timestamp.date()
            or row["content_sha256"]
            != snapshot_content_sha256(
                snapshot, serialization_version=serialization_version
            )
        ):
            raise RuntimeError("normalized snapshot index is inconsistent")
        actual_path = self._safe_relative_path(cast(str, row["parquet_path"]))
        if actual_path != expected_path:
            raise RuntimeError("normalized snapshot index is inconsistent")
        self._parquet.verify(
            actual_path,
            snapshot_id,
            snapshot,
            serialization_version=serialization_version,
        )

    def _validated_index_path(
        self, row: Mapping[str, Any], snapshot: OptionChainSnapshot
    ) -> Path:
        try:
            indexed_instrument = cast(str, row["instrument"])
            indexed_date = cast(date, row["session_date"])
            serialization_version = self._serialization_version(row)
            expected_path = self._parquet._relative_path(
                cast(str, row["raw_snapshot_id"]),
                snapshot,
                snapshot.source_timestamp.date(),
                serialization_version=serialization_version,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError("normalized snapshot index is inconsistent") from error
        if (
            indexed_instrument != snapshot.instrument
            or indexed_date != snapshot.source_timestamp.date()
            or row["content_sha256"]
            != snapshot_content_sha256(
                snapshot, serialization_version=serialization_version
            )
        ):
            raise RuntimeError("normalized snapshot index is inconsistent")
        actual_path = self._safe_relative_path(cast(str, row["parquet_path"]))
        if actual_path != expected_path:
            raise RuntimeError("normalized snapshot index is inconsistent")
        return actual_path

    @staticmethod
    def _serialization_version(row: Mapping[str, Any]) -> int:
        value = row.get("serialization_version")
        if not isinstance(value, int) or value not in {1, 2, 3}:
            raise RuntimeError("normalized snapshot serialization is inconsistent")
        return value

    def _safe_relative_path(self, path_text: str) -> Path:
        """Accept only a contained, non-traversing relative path beneath parquet_root."""
        if not path_text:
            raise RuntimeError("unsafe Parquet index path")
        windows_path = PureWindowsPath(path_text)
        posix_path = PurePosixPath(path_text)
        if (
            windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or posix_path.is_absolute()
            or any(part == ".." for part in windows_path.parts + posix_path.parts)
        ):
            raise RuntimeError("unsafe Parquet index path")
        relative_path = Path(*posix_path.parts)
        root = self.parquet_root.resolve()
        try:
            (root / relative_path).resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as error:
            raise RuntimeError("unsafe Parquet index path") from error
        return relative_path

    def _remove_attempt_publication(self, relative_path: Path) -> None:
        """Remove only this locked attempt's canonical destination after a failed index write."""
        destination = self.parquet_root / relative_path
        if self._safe_relative_path(relative_path.as_posix()) != relative_path:
            raise RuntimeError("unsafe Parquet index path")
        destination.unlink(missing_ok=True)

    def _normalized_record(
        self, connection: Connection, snapshot_id: SnapshotId, content_sha256: str
    ) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(normalized_snapshots).where(
                    normalized_snapshots.c.raw_snapshot_id == snapshot_id,
                    normalized_snapshots.c.content_sha256 == content_sha256,
                )
            )
            .mappings()
            .one_or_none()
        )
        return cast(Mapping[str, Any] | None, row)

    def _snapshot_from_rows(
        self, row: Mapping[str, Any], quote_rows: Iterator[Mapping[str, Any]]
    ) -> OptionChainSnapshot:
        try:
            source_time_authoritative = cast(
                bool, row["source_time_authoritative"]
            )
            quotes = tuple(
                self._quote_from_row(
                    quote_row,
                    timestamp_authoritative=cast(
                        bool, quote_row["timestamp_authoritative"]
                    ),
                )
                for quote_row in quote_rows
            )
            return OptionChainSnapshot(
                instrument=cast(Literal["NIFTY", "BANKNIFTY"], row["instrument"]),
                source_timestamp=self._as_ist(cast(datetime, row["source_timestamp"])),
                received_at=self._as_ist(cast(datetime, row["received_at"])),
                spot=cast(float, row["spot"]),
                expiry=cast(date, row["expiry"]),
                quotes=quotes,
                source_time_authoritative=source_time_authoritative,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError("normalized snapshot index is inconsistent") from error

    @staticmethod
    def _decompress_raw(snapshot_id: SnapshotId, compressed_payload: bytes) -> bytes:
        try:
            payload = zlib.decompress(compressed_payload)
        except zlib.error as error:
            raise RuntimeError(f"raw snapshot {snapshot_id} is corrupt") from error
        if _payload_digest(payload) != snapshot_id:
            raise RuntimeError(
                f"raw snapshot {snapshot_id} failed integrity verification"
            )
        return payload

    @staticmethod
    def _as_ist(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)

    def _quote_from_row(
        self, row: Mapping[str, Any], *, timestamp_authoritative: bool
    ) -> OptionQuote:
        return OptionQuote(
            timestamp=self._as_ist(cast(datetime, row["timestamp"])),
            timestamp_authoritative=timestamp_authoritative,
            strike=cast(float, row["strike"]),
            option_type=cast(Literal["CE", "PE"], row["option_type"]),
            expiry=cast(date | None, row["expiry"]),
            ltp=cast(float | None, row["ltp"]),
            bid=cast(float | None, row["bid"]),
            ask=cast(float | None, row["ask"]),
            volume=cast(int, row["volume"]),
            oi=cast(int, row["oi"]),
            previous_oi=cast(int | None, row["previous_oi"]),
            iv=cast(float | None, row["iv"]),
            api_delta=cast(float | None, row["api_delta"]),
            api_gamma=cast(float | None, row["api_gamma"]),
        )
