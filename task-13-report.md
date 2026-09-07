# Task 13 verification report

Implemented chronological purged walk-forward validation and direction-specific
governance reporting.

- `backtesting/splits.py` emits timezone-aware `Fold` objects with strictly
  ordered train, validation, calibration, and untouched test blocks. Label
  windows are purged at each boundary and explicit embargo windows/metadata are
  retained. Invalid, unsorted, duplicate, naïve, or insufficient input fails
  closed.
- `backtesting/walk_forward.py` fits the injected model on train only; sigmoid,
  isotonic, and temperature calibrators are each fit on calibration only and
  selected using validation loss before test evaluation. BUY and SELL are
  evaluated independently.
- `backtesting/metrics.py` reports Brier/log loss/reliability bins, precision
  with Wilson intervals, coverage/abstention, fixed-coverage precision,
  fixed-error coverage, false-signal rate, cost-adjusted expectancy, profit
  factor, drawdown, monthly/regime slices, threshold sensitivity, and explicit
  sample-size suppression.
- `backtesting/reporting.py` provides serializable evidence and a private-marker
  backed `PromotionRegistry`. Caller-provided IDs or `verified=True` flags do
  not resolve as promotion evidence; only artifacts derived by the governance
  builder can be resolved. The existing Phase 0–4 `SelectiveDecision` remains
  unconditionally `RESEARCH/NO_TRADE`.

Verification:

- `py -m pytest tests/unit/test_purged_splits.py tests/integration/test_walk_forward.py -v` — 7 passed.
- `py -m pytest -q` — 286 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed (42 source files).

## Final narrow correction

- Evaluation now requires every train, validation, calibration, and test block
  to carry one non-null matching instrument and exactly one shared label-column
  name; missing or mixed metadata fails closed.
- Factories that return a model without callable `fit` are rejected, preserving
  the fresh-unfitted model contract.

Final correction verification:

- `py -m pytest tests/unit/test_purged_splits.py tests/integration/test_walk_forward.py -v` — 13 passed.
- `py -m pytest -q` — 292 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed (42 source files).

## Session-boundary correction

- Zero-embargo session lookup now uses a right-sided boundary search, so a
  one-row trailing session is never selected into validation again. Requested
  multi-fold configurations now fail explicitly when their complete-session
  capacity is insufficient; expanding-vs-rolling semantics and integer/fraction
  sizing are documented and covered by regression tests.

Session-boundary verification:

- Focused suites — 15 passed.
- Full suite run 1 — 294 passed.
- Full suite run 2 — 294 passed.
- Ruff and mypy — passed.
- `rg -n "train_test_split|KFold\(" src/nifty_signal_engine` — no matches.

No order-submission, credential, or live execution path was added.

## Independent-review correction

- Promotion artifacts now require an internally verified chronological
  `FoldReport` produced by `evaluate_fold`; manually constructed reports,
  naked metrics, `None`, fabricated IDs, and caller `verified=True` flags are
  rejected or cannot resolve through the registry. Artifact identity is derived
  from report evidence and carries a private in-process proof marker.
- Model factories are explicitly fresh/unfitted contracts. Every returned
  model is fit on the train block, including one-argument factories; common
  fitted markers are rejected. Both BUY and SELL calibrators are selected
  before any untouched-test prediction, and SELL economics reverse raw
  underlying returns. `net_return`/`net_pnl` columns are not charged costs a
  second time.
- Folds are bound to complete IST calendar sessions with no date overlap across
  train, validation, calibration, and test. Expanding training is the default;
  fixed-width rolling windows require `rolling=True`. Unknown outcomes and
  invalid split-count types fail closed.

Correction verification:

- `py -m pytest tests/unit/test_purged_splits.py tests/integration/test_walk_forward.py -v` — 11 passed.
- `py -m pytest -q` — 290 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed (42 source files).
