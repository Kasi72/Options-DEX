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

## Independent-review correction

- A failed quality gate or `FeaturePipeline` rejection now cancels every pending candidate with an explicit rejection; no quote from that event can fill an intent.
- Replay events reject future quotes, validate quote order both within and across events, and reset feature-pipeline state at the start of each run so the same engine/session produces the same result twice.
- Candidates emitted before their source feature row becomes available are rejected as `LOOKAHEAD_SIGNAL_TIME`.
- `CostBreakdown` component and total fields now carry explicit `_rupees` names and an `INR` currency contract. Schedule reports state `currency=INR` and `monetary_unit=rupees`.
- Candidates and fills require integral positive quantities; fill prices must remain within their recorded bid/ask range.

Correction verification:

- `py -m pytest tests/unit/test_costs.py tests/unit/test_fills.py tests/integration/test_event_replay.py -v` — 18 passed.
- `py -m pytest -v` — 279 passed.
- `py -m ruff check .` — passed.
- `py -m mypy src` — passed (38 source files).
