"""Versioned SQLite schema for local immutable market-data storage."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BLOB,
    JSON,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    TypeDecorator,
    UniqueConstraint,
)

SCHEMA_VERSION = 3
IST = ZoneInfo("Asia/Kolkata")


class ISTDateTime(TypeDecorator[datetime]):
    """Store ISO-8601 instants with an explicit Asia/Kolkata offset in SQLite."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, _dialect: object
    ) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(IST).isoformat(timespec="microseconds")

    def process_result_value(
        self, value: str | None, _dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            raise ValueError("stored timestamp is missing timezone information")
        return timestamp.astimezone(IST)


metadata = MetaData()

schema_versions = Table(
    "schema_versions",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", ISTDateTime(), nullable=False),
)

raw_snapshots = Table(
    "raw_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("compressed_payload", BLOB, nullable=False),
    Column("saved_at", ISTDateTime(), nullable=False),
)

normalized_snapshots = Table(
    "normalized_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "raw_snapshot_id",
        String(64),
        ForeignKey("raw_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("content_sha256", String(64), nullable=False),
    Column("instrument", String(16), nullable=False),
    Column("session_date", Date, nullable=False),
    Column("source_timestamp", ISTDateTime(), nullable=False),
    Column("received_at", ISTDateTime(), nullable=False),
    Column("spot", Float, nullable=False),
    Column("expiry", Date, nullable=False),
    Column(
        "source_time_authoritative",
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    ),
    Column(
        "serialization_version",
        Integer,
        nullable=False,
        default=3,
        server_default="3",
    ),
    Column("parquet_path", String, nullable=False),
    UniqueConstraint(
        "raw_snapshot_id", "content_sha256", name="uq_normalized_snapshot_content"
    ),
)

option_quotes = Table(
    "option_quotes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "normalized_snapshot_id",
        Integer,
        ForeignKey("normalized_snapshots.id"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("timestamp", ISTDateTime(), nullable=False),
    Column(
        "timestamp_authoritative",
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    ),
    Column("strike", Float, nullable=False),
    Column("option_type", String(2), nullable=False),
    Column("expiry", Date, nullable=True),
    Column("ltp", Float, nullable=True),
    Column("bid", Float, nullable=True),
    Column("ask", Float, nullable=True),
    Column("volume", Integer, nullable=False),
    Column("oi", Integer, nullable=False),
    Column("previous_oi", Integer, nullable=True),
    Column("iv", Float, nullable=True),
    Column("api_delta", Float, nullable=True),
    Column("api_gamma", Float, nullable=True),
    UniqueConstraint(
        "normalized_snapshot_id", "ordinal", name="uq_option_quote_ordinal"
    ),
)

collector_state = Table(
    "collector_state",
    metadata,
    Column("instrument", String(16), primary_key=True),
    Column("state", JSON, nullable=False),
    Column("updated_at", ISTDateTime(), nullable=False),
)

quality_decisions = Table(
    "quality_decisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "normalized_snapshot_id",
        Integer,
        ForeignKey("normalized_snapshots.id"),
        nullable=False,
    ),
    Column("tradable", Boolean, nullable=False),
    Column("codes", JSON, nullable=False),
    Column("details", JSON, nullable=False),
    Column("checked_at", ISTDateTime(), nullable=False),
)

research_signals = Table(
    "research_signals",
    metadata,
    Column("signal_id", String(128), primary_key=True),
    Column(
        "raw_snapshot_id",
        String(64),
        ForeignKey("raw_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("payload", JSON, nullable=False),
    Column("created_at", ISTDateTime(), nullable=False),
)

signal_events = Table(
    "signal_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "signal_id",
        String(128),
        ForeignKey("research_signals.signal_id"),
        nullable=False,
    ),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", ISTDateTime(), nullable=False),
)

experiment_runs = Table(
    "experiment_runs",
    metadata,
    Column("run_id", String(128), primary_key=True),
    Column("metadata", JSON, nullable=False),
    Column("created_at", ISTDateTime(), nullable=False),
    Column("completed", Boolean, nullable=False, default=False),
)


def schema_applied_at() -> datetime:
    """Provide a single testable boundary for schema-install timestamps."""
    return datetime.now(IST)
