# Task 11 report — safe directional baselines

## Commit

- Implementation commit: `2d882e1b507433911500cde2aa0be0e88194efb1` (`feat: establish safe directional baselines`).

## Delivered

- Added a deterministic `RuleBaseline` that requires a quality-clean completed bar and unanimous price, VWAP, valid-regime, and option-flow direction. Disagreement produces a dominant `NO_MOVE` probability.
- Added a per-instrument `LogisticBaseline` backed by a scikit-learn pipeline. Median imputation and regularized multinomial logistic regression are fitted together on the supplied training frame; fitted medians, coefficients, intercepts, and feature order remain inspectable.
- Raw `DirectionProbabilities` validate finite unit-simplex values. Calibration fields remain explicitly `None` for both baselines because chronological calibration belongs to Task 13.
- Added `SelectiveDecision` with separate NIFTY/BANKNIFTY BUY and SELL thresholds and nullable disagreement, meta-label, conformal, sequential-evidence, and economics research fields.
- Every missing or failed mandatory gate returns `NO_TRADE` with explicit reasons. Outputs are always marked `RESEARCH`; only an explicitly promoted, calibrated, quality-clean candidate with all downstream gates can expose a research action.
- `BUY` maps only to long calls/call debit spreads and `SELL` only to long puts/put debit spreads. No order-submission or credential/network path was added.
- Declared scikit-learn as a runtime dependency.

## TDD evidence

- Initial RED: both focused modules failed collection with `ModuleNotFoundError` for the missing baseline package.
- GREEN: the first implementation passed 22 tests; three test-helper errors were corrected before behavioral status was assessed, after which all 25 original focused tests passed.
- Additional RED/GREEN cycles:
  - an inconsistent `tradable=True` report carrying a quality code initially omitted `DATA_QUALITY_FAILED`; the gate now fails closed on either indicator;
  - fitted parameters were initially not exposed; coefficients and intercepts are now available as immutable copies;
  - a feature-only row from a fitted model's training frame initially failed instrument matching; model identity now supplies the instrument while any explicitly conflicting row instrument is rejected.

## Verification

- `py -m pytest tests/unit/test_baselines.py tests/unit/test_selective_signals.py -v` — 28 passed.
- `py -m pytest -q` — 243 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 34 source files.
- `git diff --cached --check` — passed before the implementation commit.
- Source scans found no random `train_test_split`/`KFold` path and no credential or order-submission terms in the signal package.

## Concerns

- The current feature pipeline deliberately reports VWAP as unavailable and does not yet produce a validated regime field. Consequently, the rule baseline rejects current pipeline rows rather than inventing confluence; later feature work must supply both inputs before it can emit a directional research probability.
- Host-wide `py -m pip check` reports pre-existing missing extras for unrelated `pkdevtools`, `pknsetools`, and `pandas-ta` packages. Task 11 imports and tests scikit-learn 1.7.2 successfully; no project test or static check is affected.
