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

## Correction round 1

### Commit

- Correction commit: `782be16a4a94e77ee733663823d80a37b574913e` (`fix: harden research baseline provenance`).

### Changes

- Equal BUY/SELL probabilities now explicitly produce `NO_DIRECTIONAL_EDGE`, with `direction=None` and `NO_TRADE` even when every threshold and downstream gate is permissive.
- Replaced the rule baseline's impossible VWAP/regime prerequisites with supported completed-row inputs: price movement, flow DEX, OI DEX, regular-session status, and completed-flow status. A real `FeaturePipeline` integration test proves deterministic interoperability; missing, invalid, incomplete, or disagreeing inputs still fail closed or favor `NO_MOVE`.
- Inserted `StandardScaler` after train-fitted median imputation and before logistic regression in the same sklearn `Pipeline`. Unit-rescaled features produce stable probabilities, and exposed scale parameters remain fixed after the caller mutates the original training frame.
- Added canonical IST provenance to directional predictions and research signals: feature availability, model and feature-schema versions, horizon, training-window bounds and identity, calibration identity/time, and validation-report identity/time.
- A promoted selective decision pins the expected model, schema, horizon, training, calibration, and validation identities. Action remains impossible without a matching quality instrument, completed feature row, feature/schema time match, calibrated probabilities, a validation report completed before prediction, and all existing safety/economics gates.
- Added serialization, instrument mismatch, identity mismatch, missing-provenance, validation chronology, and naïve-quality timestamp regressions. No order, network, credential, or source-data path changed.

### TDD evidence

- RED/GREEN: equal UP/DOWN probabilities previously selected `SELL`; the new tie regression observed `BUY_PUT` before the tie-abstention branch was added.
- RED/GREEN: a real completed pipeline row previously raised `vwap features are not valid`; the rule now consumes only currently emitted fields.
- RED/GREEN: multiplying one feature by one billion changed the unscaled logistic probability from approximately `0.8904` to `0.9753`; train-fitted scaling now makes unit-equivalent predictions agree to `1e-12`.
- RED/GREEN: provenance tests initially failed on absent fields/signatures and allowed an unprovenanced action; matching provenance is now serialized while every missing/mismatched field abstains.
- RED/GREEN: UTC training provenance remained UTC and a naïve quality time crashed aware comparison; training timestamps are canonicalized to IST and invalid quality chronology fails closed.

### Verification

- `py -m pytest tests/unit/test_baselines.py tests/unit/test_selective_signals.py tests/integration/test_baseline_pipeline.py -v` — 39 passed.
- `py -m pytest -q` — 254 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 34 source files.
- `git diff --cached --check` — passed before the correction commit.

### Remaining concerns

- Calibration and validation identities are auditable scaffolding, not proof from a signed/registered artifact store. Task 13 must populate them from chronological calibration and walk-forward validation outputs; until then normal baseline predictions remain `RESEARCH/NO_TRADE`.
- `DataQualityReport` has no typed instrument field, so the selective gate consumes an explicit `details["instrument"]` provenance value and abstains when it is absent. A future additive quality-contract migration should make this field first-class while preserving stored-audit compatibility.

## Correction round 2

### Commit

- Correction commit: `ac59409380a1770bae855a2ef178684cc7c09b70` (`fix: keep baseline promotion fail closed`).

### Changes

- Matching non-empty calibration and validation identifiers no longer promote a scaffold-only result. Without separate artifact evidence, evaluation remains `RESEARCH/NO_TRADE` and reports `MODEL_NOT_PROMOTED`, `CALIBRATION_NOT_VALIDATED`, and `VALIDATION_NOT_PROMOTED`.
- Added explicit calibration-artifact validation and validation-artifact promotion flags to the selective input and research-signal audit output. These are independent of identifier strings; both flags plus the existing `validated` state and all other gates are required by the future action seam.
- Logistic fitting now requires an `instrument` column on every training frame. The column must be non-null, contain exactly one supported instrument, and match the model's configured NIFTY or BANKNIFTY identity. Untagged, mixed, unknown, and mismatched frames are rejected before fitting.
- Prediction remains convenient but safe: a feature-only row can be scored only after the model was fitted from explicitly tagged single-instrument training data, and an explicitly conflicting prediction instrument is still rejected.

### TDD evidence

- RED/GREEN: the complete matching-string scaffold previously emitted `BUY_CALL`; it now abstains with the three explicit promotion/calibration reasons.
- RED/GREEN: the future artifact-flag seam initially failed construction because the flags did not exist; a complete evidenced fixture now reaches the prior research-action branch without weakening the default.
- RED/GREEN: an untagged frame previously fitted silently as NIFTY while mixed/null/unknown cases already rejected; all four cases now reject through one explicit instrument-identity contract.

### Verification

- `py -m pytest tests/unit/test_baselines.py tests/unit/test_selective_signals.py tests/integration/test_baseline_pipeline.py -v` — 44 passed.
- `py -m pytest -q` — 259 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 34 source files.
- Signal-package scan found no credential or order-submission path.

### Remaining concern

- The artifact flags are deliberately inert defaults in Phases 0–4. Task 13 must source them from verified calibration and validation artifacts rather than application configuration or user-provided identifiers.
