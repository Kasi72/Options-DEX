"""Recoverable local repository for immutable raw and normalized snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
import zlib
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal, NewType, Self, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, create_engine, event, insert, select

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
    raw_snapshots,
    schema_applied_at,
    schema_versions,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

IST: Final = ZoneInfo("Asia/Kolkata")
SnapshotId = NewType("SnapshotId", str)
SchemaContract = tuple[tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]], ...]


@lru_cache
def _expected_v1_schema_contract() -> SchemaContract:
    """Derive the v1 SQLite contract from the code-defined SQLAlchemy metadata."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            return _schema_contract(connection)
    finally:
        engine.dispose()


def _schema_contract(connection: Connection) -> SchemaContract:
    """Return stable SQLite PRAGMA metadata for all application tables."""
    tables: list[tuple[str, tuple[object, ...], tuple[object, ...], tuple[object, ...]]] = []
    table_names = sorted(metadata.tables)
    for table_name in table_names:
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
                    for index in connection.exec_driver_sql(f'PRAGMA index_list("{table_name}")')
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
        self._engine = create_engine(f"sqlite:///{database_path}", connect_args={"timeout": 30})
        event.listen(self._engine, "connect", self._configure_sqlite_connection)
        self._parquet = ParquetSnapshotStore(parquet_root)
        self._initialize_and_recover()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
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

    def save_normalized(self, snapshot_id: SnapshotId, snapshot: OptionChainSnapshot) -> None:
        """Publish Parquet and its SQLite index under one serialized writer boundary."""
        self.read_raw(snapshot_id)
        canonical = canonical_snapshot(snapshot)
        content_sha256 = snapshot_content_sha256(canonical)
        expected_path = self._parquet._relative_path(
            snapshot_id, canonical, canonical.source_timestamp.date()
        )

        with self._write_lock() as connection:
            existing = self._normalized_record(connection, snapshot_id, content_sha256)
            if existing is not None:
                self._verify_existing_publication(existing, snapshot_id, canonical, expected_path)
                return

            published = False
            try:
                relative_path = self._parquet.write(snapshot_id, canonical)
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
            except Exception:
                if published:
                    self._remove_attempt_publication(expected_path)
                raise

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
                self._parquet.verify(relative_path, cast(str, typed_row["raw_snapshot_id"]), snapshot)
                yield snapshot

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
            with self._engine.connect() as connection:
                self._assert_supported_existing_database(connection)
            with self._write_lock() as connection:
                self._assert_supported_existing_database(connection)
                self._recover_orphaned_parquet_locked(connection)
            return

        with self._engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            if str(journal_mode).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
        with self._write_lock() as connection:
            if self._user_table_names(connection):
                self._assert_supported_existing_database(connection)
            else:
                metadata.create_all(connection)
                connection.execute(
                    insert(schema_versions).values(version=SCHEMA_VERSION, applied_at=schema_applied_at())
                )
                self._assert_supported_existing_database(connection)
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
        if _schema_contract(connection) != _expected_v1_schema_contract():
            raise RuntimeError("unsupported existing database schema")

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
        for path_text in connection.execute(select(normalized_snapshots.c.parquet_path)).scalars():
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
        if (
            row["instrument"] != snapshot.instrument
            or row["session_date"] != snapshot.source_timestamp.date()
            or row["content_sha256"] != snapshot_content_sha256(snapshot)
        ):
            raise RuntimeError("normalized snapshot index is inconsistent")
        actual_path = self._safe_relative_path(cast(str, row["parquet_path"]))
        if actual_path != expected_path:
            raise RuntimeError("normalized snapshot index is inconsistent")
        self._parquet.verify(actual_path, snapshot_id, snapshot)

    def _validated_index_path(self, row: Mapping[str, Any], snapshot: OptionChainSnapshot) -> Path:
        try:
            indexed_instrument = cast(str, row["instrument"])
            indexed_date = cast(date, row["session_date"])
            expected_path = self._parquet._relative_path(
                cast(str, row["raw_snapshot_id"]), snapshot, snapshot.source_timestamp.date()
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError("normalized snapshot index is inconsistent") from error
        if (
            indexed_instrument != snapshot.instrument
            or indexed_date != snapshot.source_timestamp.date()
            or row["content_sha256"] != snapshot_content_sha256(snapshot)
        ):
            raise RuntimeError("normalized snapshot index is inconsistent")
        actual_path = self._safe_relative_path(cast(str, row["parquet_path"]))
        if actual_path != expected_path:
            raise RuntimeError("normalized snapshot index is inconsistent")
        return actual_path

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
        row = connection.execute(
            select(normalized_snapshots).where(
                normalized_snapshots.c.raw_snapshot_id == snapshot_id,
                normalized_snapshots.c.content_sha256 == content_sha256,
            )
        ).mappings().one_or_none()
        return cast(Mapping[str, Any] | None, row)

    def _snapshot_from_rows(
        self, row: Mapping[str, Any], quote_rows: Iterator[Mapping[str, Any]]
    ) -> OptionChainSnapshot:
        try:
            quotes = tuple(self._quote_from_row(quote_row) for quote_row in quote_rows)
            return OptionChainSnapshot(
                instrument=cast(Literal["NIFTY", "BANKNIFTY"], row["instrument"]),
                source_timestamp=self._as_ist(cast(datetime, row["source_timestamp"])),
                received_at=self._as_ist(cast(datetime, row["received_at"])),
                spot=cast(float, row["spot"]),
                expiry=cast(date, row["expiry"]),
                quotes=quotes,
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
            raise RuntimeError(f"raw snapshot {snapshot_id} failed integrity verification")
        return payload

    @staticmethod
    def _as_ist(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)

    def _quote_from_row(self, row: Mapping[str, Any]) -> OptionQuote:
        return OptionQuote(
            timestamp=self._as_ist(cast(datetime, row["timestamp"])),
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
