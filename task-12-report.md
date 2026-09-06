# Task 12 verification report

Implemented the cost-aware, deterministic event replay boundary.

- `backtesting/fills.py` records only post-signal long entries at the next valid ask. Missing, crossed, mismatched, stale, and absent quotes have explicit rejection codes.
- `backtesting/costs.py` requires a versioned, caller-configured rate card. It reports brokerage, exchange charges, taxes, GST, regulatory fees, stamp duty, bid/ask spread, and extra slippage as named components. Replay output preserves the complete versioned rate-card report.
- `backtesting/replay.py` accepts only chronological events and quotes, reuses its injected production `FeaturePipeline` and signal-service collaborator, and never sorts/recalculates events through a backtest-only feature path. Pending intents without an eligible quote become explicit `NO_EXECUTABLE_QUOTE` rejections.
- All simulated behaviour remains research-only; no order-submission path was added.

Verification completed:

- `py -m pytest tests/unit/test_costs.py tests/unit/test_fills.py tests/integration/test_event_replay.py -v` — 10 passed.
- `py -m pytest -v` — 271 passed.
- `py -m ruff check .` — passed.
- `py -m mypy src` — passed (38 source files).

`py -m mypy src tests` remains blocked by the repository's existing duplicate-module discovery of `tests/factories.py` as both `factories` and `tests.factories`; this task introduces no mypy errors in the backtesting package.
