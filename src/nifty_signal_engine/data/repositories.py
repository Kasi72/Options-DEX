"""Recoverable local repository for immutable raw and normalized snapshots."""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, NewType, Self, cast
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

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


class SnapshotRepository:
    """Persist immutable bytes first, then normalized snapshots without live execution."""

    def __init__(self, *, database_path: Path, parquet_root: Path) -> None:
        self.database_path = database_path
        self.parquet_root = parquet_root
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{database_path}", connect_args={"timeout": 30})
        self._initialize()
        self._parquet = ParquetSnapshotStore(parquet_root)
        self._recover_orphaned_parquet()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Release SQLite resources; records remain durable for a later process."""
        self._engine.dispose()

    def save_raw(self, raw: bytes) -> SnapshotId:
        """Content-address raw payload bytes and never mutate an existing row."""
        payload = bytes(raw)
        snapshot_id = SnapshotId(hashlib.sha256(payload).hexdigest())
        with self._engine.begin() as connection:
            connection.execute(
                sqlite_insert(raw_snapshots)
                .values(
                    snapshot_id=snapshot_id,
                    sha256=snapshot_id,
                    compressed_payload=zlib.compress(payload),
                    saved_at=datetime.now(IST),
                )
                .on_conflict_do_nothing(index_elements=[raw_snapshots.c.snapshot_id])
            )
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
        try:
            payload = zlib.decompress(compressed_payload)
        except zlib.error as error:
            raise RuntimeError(f"raw snapshot {snapshot_id} is corrupt") from error
        if hashlib.sha256(payload).hexdigest() != snapshot_id:
            raise RuntimeError(f"raw snapshot {snapshot_id} failed integrity verification")
        return payload

    def save_normalized(self, snapshot_id: SnapshotId, snapshot: OptionChainSnapshot) -> None:
        """Atomically publish full-strike Parquet before committing its SQLite index."""
        self.read_raw(snapshot_id)
        canonical = canonical_snapshot(snapshot)
        content_sha256 = snapshot_content_sha256(canonical)
        existing = self._normalized_record(snapshot_id, content_sha256)
        if existing is not None:
            if not (self.parquet_root / existing.parquet_path).is_file():
                raise RuntimeError("normalized snapshot index references a missing Parquet file")
            return

        relative_path = self._write_parquet_after_removing_orphan(snapshot_id, canonical)
        try:
            with self._engine.begin() as connection:
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
                        parquet_path=str(relative_path).replace("\\", "/"),
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
        except IntegrityError:
            winner = self._normalized_record(snapshot_id, content_sha256)
            if winner is not None and winner["parquet_path"] == str(relative_path).replace("\\", "/"):
                return
            raise
        except Exception:
            (self.parquet_root / relative_path).unlink(missing_ok=True)
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
                relative_path = Path(row["parquet_path"])
                if not (self.parquet_root / relative_path).is_file():
                    raise RuntimeError("normalized snapshot index references a missing Parquet file")
                quote_rows = connection.execute(
                    select(option_quotes)
                    .where(option_quotes.c.normalized_snapshot_id == row["id"])
                    .order_by(option_quotes.c.ordinal)
                ).mappings()
                quotes = tuple(
                    self._quote_from_row(cast(Mapping[str, Any], quote_row))
                    for quote_row in quote_rows
                )
                snapshot = OptionChainSnapshot(
                    instrument=row["instrument"],
                    source_timestamp=self._as_ist(row["source_timestamp"]),
                    received_at=self._as_ist(row["received_at"]),
                    spot=row["spot"],
                    expiry=row["expiry"],
                    quotes=quotes,
                )
                self._parquet.verify(relative_path, row["raw_snapshot_id"], snapshot)
                yield snapshot

    def _initialize(self) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            metadata.create_all(connection)
            connection.execute(
                sqlite_insert(schema_versions)
                .values(version=SCHEMA_VERSION, applied_at=schema_applied_at())
                .on_conflict_do_nothing(index_elements=[schema_versions.c.version])
            )

    def _recover_orphaned_parquet(self) -> None:
        """Remove incomplete/unindexed writes so a restart never replays partial data."""
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        with self._engine.connect() as connection:
            known_paths = {
                Path(path)
                for path in connection.execute(select(normalized_snapshots.c.parquet_path)).scalars()
            }
        for temporary in self.parquet_root.rglob("*.tmp"):
            temporary.unlink(missing_ok=True)
        for parquet_file in self.parquet_root.rglob("*.parquet"):
            if parquet_file.relative_to(self.parquet_root) not in known_paths:
                parquet_file.unlink(missing_ok=True)

    def _write_parquet_after_removing_orphan(
        self, snapshot_id: SnapshotId, snapshot: OptionChainSnapshot
    ) -> Path:
        relative_path = self._parquet._relative_path(
            snapshot_id, snapshot, snapshot.source_timestamp.date()
        )
        destination = self.parquet_root / relative_path
        if destination.exists():
            try:
                self._parquet.verify(relative_path, snapshot_id, snapshot)
            except RuntimeError:
                destination.unlink()
            else:
                return relative_path
        return self._parquet.write(snapshot_id, snapshot)

    def _normalized_record(self, snapshot_id: SnapshotId, content_sha256: str):
        with self._engine.connect() as connection:
            return connection.execute(
                select(normalized_snapshots).where(
                    normalized_snapshots.c.raw_snapshot_id == snapshot_id,
                    normalized_snapshots.c.content_sha256 == content_sha256,
                )
            ).mappings().one_or_none()

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
