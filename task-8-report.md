# Task 8 report — independent collection and data-quality gate

## Implementation

- `Collector` performs independent NIFTY/BANKNIFTY collection, selecting broker-discovered expiries and retrying transient boundary failures with bounded exponential backoff.
- Immutable raw response bytes are content-addressed through `SnapshotRepository.save_raw` before normalization. A strict-normalization failure returns `NORMALIZATION_FAILED` while retaining the raw snapshot for forensics. Normalized persistence occurs only after normalization succeeds.
- `assess_snapshot` returns an immutable `DataQualityReport` with explicit non-tradable codes for freshness, session phase, empty/incomplete chains, zero cumulative volume, invalid spot/quotes/IV/Greeks, expiry disagreement, clock skew, out-of-order data, and cumulative counter resets.
- Per-instrument state remains separate. An out-of-order snapshot is persisted for replay/audit but does not replace the in-order collection baseline; counter resets rebaseline subsequent observations.
- The standard-library CLI has no import-time effects and supports `collect-once`, finite `collect`, and `inspect-session`. Fixture collection is deterministic and always labeled `research_only`; live collection uses only `DHAN_ACCESS_TOKEN` and `DHAN_CLIENT_ID` from the environment. There is no order-submission path.

## TDD evidence

- RED: new data-quality and collector tests failed at collection because `monitoring.data_quality` and `data.collector` did not exist.
- GREEN: `py -m pytest tests/unit/test_data_quality.py tests/integration/test_collector.py tests/integration/test_cli.py -v` passed 8 tests.
- State-regression RED: the late-snapshot baseline test failed because an out-of-order snapshot replaced the prior baseline and concealed a subsequent counter reset.
- State-regression GREEN: `py -m pytest tests/integration/test_collector.py::test_collector_does_not_replace_an_instrument_baseline_with_out_of_order_data -v` passed.

## Verification

- `nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY --captured-at 2026-08-30T10:00:00+05:30 --data-dir C:\Users\drkkr\AppData\Local\Temp\nifty-task8-cli-smoke-20260901` returned a persisted `COLLECTED` snapshot explicitly marked `research_only: true` and `tradable: false` with quality codes.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 24 source files.
- `py -m pytest tests/unit -v` — 89 passed.
- `py -m pytest tests/integration -v` — 30 passed.

## Dependency and safety note

No runtime dependency metadata changed. The CLI deliberately uses the Python standard library, avoiding an otherwise unnecessary Typer metadata addition. Credentials are read only at an explicitly invoked live-collection boundary and are never emitted in results or tests.

## Concerns

None. Live requests were not made; all collection tests and smoke verification used the supplied sanitized fixture or mock transport.

## Critical correction round 1

### Provenance and quality contract

- `RawSnapshot` now distinguishes local HTTP capture from an optional authoritative broker timestamp. The Dhan boundary extracts supported `data.timestamp` values without pre-empting raw persistence when payload bytes are malformed; normalizing without an authoritative source timestamp is explicit and non-tradable.
- Normalized snapshots carry `source_time_authoritative`, and source, receipt, future-skew, receipt-before-source, quote-time, spread, paired-strike depth, active/expired expiry, baseline/session-reset, weekend, holiday, and unavailable-calendar failures are all explicit quality codes.
- `TradingCalendar` is an injectable authoritative-date mechanism. Its absence fails closed. The fixture carries explicit capture/source/expiry/calendar metadata, so fixture collection never uses the wall clock or a hard-coded expiry.

### Persistence and recovery

- Schema v2 adds `normalized_snapshots.source_time_authoritative` and append-only `quality_decisions`, while reusing `collector_state` for per-instrument baseline references. A locked v1-to-v2 migration accepts only the exact prior v1 contract, adds the provenance field, creates the audit table, and updates the schema version atomically.
- Fresh v2 databases and migrated v2 databases have separately exact, whitelisted SQLite contracts because SQLite appends an `ALTER TABLE` column; validation remains fail-closed for every other layout.
- `record_quality_and_baseline` appends the decision and updates one instrument's state in the same writer transaction. Collector restart restores that baseline; an assessor exception is persisted as `QUALITY_ASSESSMENT_FAILED` and cannot become tradable.

### Retry, CLI, and forensic handling

- Retries are limited to transport/timeout and HTTP 429/5xx errors. Payload/configuration failures do not retry. Every option-chain HTTP error body is content-addressed before retry or final failure, and the final failure returns the persisted raw ID.
- Collection results retain `active_expiries` and `selected_expiry`; `inspect-session` reports snapshot, tradable, and quality-code counts. Failed fetch/normalization/assessment commands exit nonzero without rendering credentials or raw body text.

### Correction-round TDD evidence

- RED: missing `QualityConfig`/calendar imports, absent quality/state repository APIs, uncaught assessment failure, indiscriminate retries, zero CLI failure exit, absent raw provenance, hard-coded fixture expiry, omitted final forensic raw ID, and omitted inspection quality counts each failed their targeted tests.
- GREEN: provenance/quality tests passed 20; repository, collector, CLI, Dhan, and normalizer correction tests passed 61 before the final import-side-effect addition. The exact v1 migration test and sorted-expiry test passed after their respective fixes.

### Final correction verification

- `py -m pytest tests/unit -v` — 95 passed.
- `py -m pytest tests/integration -q` — 42 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — no issues in 24 source files.
- Exact deterministic smoke: `nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY --data-dir C:\Users\drkkr\AppData\Local\Temp\nifty-task8-correction-smoke-20260902` returned `COLLECTED`, an immutable raw ID, explicit active/selected expiry metadata, and `research_only: true`, `tradable: false` quality gates.

### Correction concerns

The supplied sanitized fixture is dated on a weekend and has only one paired strike, so it is intentionally non-tradable. No live request, order, credential, or network side effect was used.

## Critical correction round 2

### Honest time provenance

- The Dhan option-chain adapter no longer parses or trusts an undocumented payload timestamp. Its `RawSnapshot` contains the actual local HTTP capture only, with no authoritative source time. The normalizer likewise never promotes payload data to source provenance.
- `OptionQuote.timestamp_authoritative` makes quote-time provenance explicit. Dhan placeholders inherit `False`, and quality rejects them with `QUOTE_TIME_UNAVAILABLE`; a trusted fixture or separately joined feed may provide explicit external source metadata. `RawSnapshot.captured_at` is persisted as `received_at` exactly, not replaced with a later collector clock sample.
- Consequently, Dhan-only chain snapshots cannot become tradable until they are joined to an authoritative timestamped feed. Fixture mode remains a deterministic research-only path.

### Atomic audit and recovery boundary

- `publish_normalized_with_quality_and_baseline` holds the Task 7 writer transaction across Parquet publication, normalized indexes/quotes, one quality decision, and any approved per-instrument baseline update. An audit failure rolls back the normalized index; the next repository startup removes the unindexed Parquet orphan.
- Generic `save_normalized` now atomically attaches one explicit non-tradable `NOT_ASSESSED` decision. `iter_tradable_session` is the opt-in usable replay iterator; `iter_session` rejects externally damaged rows missing an audit decision, and summaries expose `QUALITY_MISSING`.
- The exact v1-to-v2 locked migration now backfills every preexisting normalized row with `MIGRATED_UNASSESSED`, so migration cannot create a quality-audit gap.

### Baseline integrity

- Restored collector state now checks the requested instrument, referenced normalized row, audit presence, canonical index/path/content, and Parquet artifact before returning a baseline. Instrument mismatch or corruption raises at the repository boundary; collector converts it to explicit non-tradable `BASELINE_CORRUPT` without substituting another instrument's state.
- Direct cross-instrument assessment returns `BASELINE_INSTRUMENT_MISMATCH`.

### Round-2 TDD and verification

- RED/GREEN regressions cover ignored payload timestamps, external trusted provenance, capture-time receipt, quote-time unavailability, cross-instrument baselines, generic pending quality, atomic audit-write rollback, v1 quality backfill, and corrupted state references.
- `py -m pytest tests/unit tests/integration -q` — 143 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — no issues in 24 source files.
- Deterministic fixture smoke and `inspect-session` passed with `COLLECTED`, `research_only: true`, `tradable: false`, and explicit quality codes. No live request, order submission, or credential emission occurred.

## Critical correction round 3

- Receipt provenance and assessment timing are separate: normalized `received_at` remains the original `RawSnapshot.captured_at`, while freshness, report `checked_at`, and quality decisions use a new clock sample taken after normalization/persistence work.
- Schema v3 adds persisted per-quote `timestamp_authoritative` state to SQLite and Parquet. Canonicalization and content identity preserve each quote's flag independently; a trusted snapshot source cannot make an untrusted quote timestamp tradable on replay.
- The locked migration explicitly supports exact v1 and v2 contracts. `serialization_version` records the historical content/path contract (v1, v2, or v3), so immutable v1 Parquet files are verified against their original hash/table contract rather than silently rehashed. Legacy quote provenance remains `false` and v1 audit rows remain quarantined as `MIGRATED_UNASSESSED`.
- Exact duplicate reobservations reuse their existing immutable normalized artifact and audit decision without appending an audit or moving collector state backward. Content-distinct observations remain separate immutable snapshots.

Round-3 RED/GREEN coverage includes delayed assessment, mixed source/quote provenance roundtrip, genuine v1-format Parquet replay after restart, v2-to-v3 migration, and repeated fixture collection. Verification: `py -m pytest tests/unit tests/integration -q` — 148 passed; `py -m ruff check src tests` and `py -m mypy src` passed. No live request, order, or credential output was used.

## Critical correction round 4 — current observation versus historical audit

### Root cause and semantics

- Supersedes round 3's duplicate-audit-return behavior: collection freshly assessed a duplicate at the current clock, but the repository discarded that assessment and returned the original historical report. A once-tradable snapshot therefore remained tradable in the returned current result after freshness expired.
- `CollectionResult.quality` now always describes the current observation. Duplicate normalized content produces a persisted, fail-closed `DUPLICATE_OBSERVATION` decision retaining the current `checked_at`, assessment codes (including stale source/receipt and equal/out-of-order timestamps), and details. Assessor exceptions likewise retain their current failure code and time.
- Duplicate decisions are appended to the existing `quality_decisions` table and linked by its existing normalized-snapshot foreign key. Repository-controlled details label `decision_scope: duplicate_observation` and reference the stable `historical_quality_decision_id`; callers cannot forge those reserved fields. Every duplicate invocation is audited, even when its deterministic report is identical to an earlier invocation.
- The first-publication audit remains immutable and is selected explicitly by historical replay/summary APIs. Historical tradability is a replay property, never a current permission. Observation rows cannot inflate historical counts or replace a missing original audit. No schema migration is needed: the existing version-3 audit table already supports multiple rows per normalized snapshot, so exact v1/v2/v3 schema compatibility is unchanged.
- Raw bytes and normalized/Parquet content remain idempotent. Duplicate publication never changes persisted baseline state, and collector in-memory state now honors that same rule even with an injected permissive assessor.
- CLI output includes `quality_checked_at` and `quality_details`, exposing the current decision's time and durable historical reference rather than hiding them behind a `COLLECTED` status.

### TDD evidence

- RED: `py -m pytest tests/integration/test_collector.py -k duplicate -q` — 3 failed, 12 deselected. The real quality assessor first established a trusted baseline and a fully tradable snapshot; equal-time (`10:00:01`), fresh (`10:00:10`), and stale (`10:01:00`) reobservations all incorrectly returned `tradable=True` from the original `10:00:01` audit.
- GREEN: the same command — 3 passed, 12 deselected. The stale case retains `STALE_SOURCE`, `STALE_RECEIPT`, a 60-second source age, and the current checked time. Reopening verifies persisted duplicate decisions, unchanged original audits and baseline, unchanged Parquet bytes, and exactly two historical snapshots with only the original positive decision replayable.
- Additional RED: repeated CLI test failed on missing `quality_checked_at`; missing-original-audit test failed because publication silently recreated an original audit despite surviving observation rows. Both pass after the corresponding corrections.
- Covering tests also exercise repository-owned audit fields, a newer persisted baseline, collector in-memory baseline protection with a permissive assessor, current assessment exceptions on duplicates, deterministic repeated CLI output, and the existing legacy-schema migrations.

### Final verification

- `py -m pytest tests/integration/test_collector.py tests/integration/test_repositories.py tests/integration/test_cli.py -q` — 52 passed.
- `py -m pytest tests/unit tests/integration -q` — 156 passed.
- `py -m ruff check src/nifty_signal_engine/data/collector.py src/nifty_signal_engine/data/repositories.py src/nifty_signal_engine/monitoring/data_quality.py src/nifty_signal_engine/cli.py tests/integration/test_collector.py tests/integration/test_repositories.py tests/integration/test_cli.py` — All checks passed; no command-line rule ignores. Two existing touched-path lint findings were corrected without suppression.
- `py -m mypy src` — Success: no issues found in 24 source files.
- `git diff --check` — no whitespace errors (Git reported only configured LF-to-CRLF working-copy conversion warnings).

### Scope and concerns

No live network calls, orders, secrets, original reference-file edits, or schema changes. The intentional append-only observation history grows with collection attempts; normalized artifacts remain deduplicated. No remaining concern for this finding.
