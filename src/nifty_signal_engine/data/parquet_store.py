"""Atomic partitioned Parquet storage for strike-level option snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from nifty_signal_engine.domain.market import OptionChainSnapshot

IST: Final = ZoneInfo("Asia/Kolkata")

PARQUET_SCHEMA: Final = pa.schema(
    [
        pa.field("raw_snapshot_id", pa.string(), nullable=False),
        pa.field("instrument", pa.string(), nullable=False),
        pa.field("session_date", pa.date32(), nullable=False),
        pa.field("source_timestamp", pa.timestamp("us", tz="Asia/Kolkata"), nullable=False),
        pa.field("received_at", pa.timestamp("us", tz="Asia/Kolkata"), nullable=False),
        pa.field("spot", pa.float64(), nullable=False),
        pa.field("snapshot_expiry", pa.date32(), nullable=False),
        pa.field("quote_timestamp", pa.timestamp("us", tz="Asia/Kolkata"), nullable=False),
        pa.field("strike", pa.float64(), nullable=False),
        pa.field("option_type", pa.string(), nullable=False),
        pa.field("expiry", pa.date32()),
        pa.field("ltp", pa.float64()),
        pa.field("bid", pa.float64()),
        pa.field("ask", pa.float64()),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("oi", pa.int64(), nullable=False),
        pa.field("previous_oi", pa.int64()),
        pa.field("iv", pa.float64()),
        pa.field("api_delta", pa.float64()),
        pa.field("api_gamma", pa.float64()),
    ]
)


def canonical_snapshot(snapshot: OptionChainSnapshot) -> OptionChainSnapshot:
    """Return the persistence boundary's canonical Asia/Kolkata representation."""
    return snapshot.model_copy(
        update={
            "source_timestamp": snapshot.source_timestamp.astimezone(IST),
            "received_at": snapshot.received_at.astimezone(IST),
            "quotes": tuple(
                quote.model_copy(update={"timestamp": quote.timestamp.astimezone(IST)})
                for quote in snapshot.quotes
            ),
        }
    )


def snapshot_content_sha256(snapshot: OptionChainSnapshot) -> str:
    """Hash the canonical normalized snapshot for idempotent persistence."""
    canonical = canonical_snapshot(snapshot).model_dump(mode="json")
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ParquetSnapshotStore:
    """Write one immutable full-strike snapshot per atomically published file."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, raw_snapshot_id: str, snapshot: OptionChainSnapshot) -> Path:
        """Atomically publish canonical strike records and return their relative path."""
        canonical = canonical_snapshot(snapshot)
        session_date = canonical.source_timestamp.date()
        relative_path = self._relative_path(raw_snapshot_id, canonical, session_date)
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return relative_path

        temporary = destination.with_suffix(".parquet.tmp")
        try:
            pq.write_table(self._table(raw_snapshot_id, canonical, session_date), temporary, compression="zstd")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return relative_path

    def verify(
        self, relative_path: Path, raw_snapshot_id: str, snapshot: OptionChainSnapshot
    ) -> None:
        """Reject missing, malformed, or substituted immutable Parquet artifacts."""
        destination = self.root / relative_path
        canonical = canonical_snapshot(snapshot)
        expected = self._table(raw_snapshot_id, canonical, canonical.source_timestamp.date())
        try:
            actual = pq.ParquetFile(destination).read()
        except (OSError, pa.ArrowException) as error:
            raise RuntimeError("Parquet artifact failed integrity verification") from error
        if not actual.equals(expected, check_metadata=False):
            raise RuntimeError("Parquet artifact failed integrity verification")

    def _relative_path(
        self, raw_snapshot_id: str, snapshot: OptionChainSnapshot, session_date: date
    ) -> Path:
        content_hash = snapshot_content_sha256(snapshot)
        return (
            Path(f"instrument={snapshot.instrument}")
            / f"session_date={session_date.isoformat()}"
            / f"snapshot-{raw_snapshot_id}-{content_hash}.parquet"
        )

    @staticmethod
    def _table(raw_snapshot_id: str, snapshot: OptionChainSnapshot, session_date: date) -> pa.Table:
        rows: list[dict[str, object]] = []
        for quote in snapshot.quotes:
            rows.append(
                {
                    "raw_snapshot_id": raw_snapshot_id,
                    "instrument": snapshot.instrument,
                    "session_date": session_date,
                    "source_timestamp": snapshot.source_timestamp,
                    "received_at": snapshot.received_at,
                    "spot": snapshot.spot,
                    "snapshot_expiry": snapshot.expiry,
                    "quote_timestamp": quote.timestamp,
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
            )
        return pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA)
