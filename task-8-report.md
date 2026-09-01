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
