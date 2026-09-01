# Task 7 report — immutable market snapshot persistence

## Implementation

- `SnapshotRepository` content-addresses and zlib-compresses immutable raw payload bytes before any normalized record can be written.
- SQLite is initialized in WAL mode with `synchronous=FULL`, an explicitly versioned schema, and IST-offset-preserving timestamps.
- Normalized snapshot and quote records are unique and durable; session reads are deterministic by source time, receipt time, then record ID.
- Full strike snapshots are atomically published to Zstandard-compressed Parquet files partitioned by `instrument=.../session_date=YYYY-MM-DD/` using a fixed Arrow schema.
- Restart recovery removes incomplete or unindexed Parquet artifacts. Indexed Parquet is verified against SQLite reconstruction before replay; missing, malformed, or substituted files fail closed.
- NIFTY and BANKNIFTY remain independently partitioned and filtered. This task has no live API or order-execution path.

## TDD evidence

- RED: `py -m pytest tests/integration/test_repositories.py -v` initially failed because `SnapshotRepository` did not exist.
- GREEN: idempotent raw bytes, WAL/schema version, IST storage offset, crash/reopen chronology, canonical partitions, corrupt-artifact fail-closed behavior, and concurrent duplicate normalized-write recovery pass in `7` repository integration tests.
- The corruption regression was first observed failing because the reader only checked file existence; it now validates the actual immutable Parquet table.

## Verification

- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 20 source files.
- `py -m pytest tests/unit -v` — passed, 85 tests.
- `py -m pytest tests/integration/test_repositories.py -v` — passed, 7 tests.
- `py -m pip install --dry-run .` — resolves `sqlalchemy>=2.0` and `pyarrow>=18.0.0`; generated `*.egg-info` metadata was removed afterwards.

## Dependency metadata

The plan’s prescribed SQLAlchemy 2 and PyArrow runtime dependencies were absent from the original project metadata. The root agent authorized this narrow `pyproject.toml` update before implementation.

## Concerns

None.

## Review correction round 1

- Normalized publication, SQLite indexing, and orphan recovery now share a cross-process `BEGIN IMMEDIATE` writer-lock boundary. A startup cannot remove an artifact while its writer is paused after atomic publication and before index commit.
- Parquet temporary names are writer-unique. Duplicate writers recheck the committed index while holding the lock; failure cleanup can remove only the current attempt's canonical publication.
- Indexed paths are treated as hostile input: absolute, traversal, drive/root, symlink-root escape, and unexpected partition paths are rejected. Indexed instrument, session date, snapshot hash, and canonical partition are checked before replay.
- SQLite enables foreign keys and full synchronous mode on every SQLAlchemy connection. Existing databases are read-only checked for the exact v1 schema/version/WAL contract before recovery; unknown, newer, or missing-version DBs are rejected without schema mutation.
- Raw digest conflicts now decompress and byte-compare before accepting idempotence; a mismatched payload fails as a collision.

### Correction-round evidence

- RED: deterministic paused-after-publication startup, traversal, wrong-session, pooled-foreign-key, incompatible-schema, and digest-collision tests failed against commit `7d41d91`.
- GREEN: `py -m pytest tests/integration/test_repositories.py -v` passed `16` tests.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 20 source files.
- `py -m pytest tests/unit -v` — passed, 85 tests.
