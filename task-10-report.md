# Task 10 report — leakage-safe triple-barrier labels

## Commit

- Implementation commit: `d3929b4d334baf1759ae896de91952f61c5dbbbe` (`feat: add leakage-safe triple-barrier labels`)

## Delivered

- Added research-only `label_triple_barrier` outcomes: `UP`, `DOWN`, `NO_MOVE`, `AMBIGUOUS`, and `INSUFFICIENT_FUTURE_DATA`. They are labels only and have no signal or order behavior.
- Accepts timezone-aware, strictly chronological close series, and OHLC frames when intrabar range information is available.
- The first upper/lower barrier wins. An OHLC bar touching both barriers is explicitly `AMBIGUOUS` because intrabar sequencing is unavailable.
- Requires an exact vertical-horizon observation when neither price barrier fires; otherwise returns `INSUFFICIENT_FUTURE_DATA`.
- Records terminal return and path-to-outcome MFE/MAE without reading prices after the horizon or after a resolved barrier.
- Rejects naïve, nonmonotonic, duplicate, nonfinite, nonpositive, and invalid-OHLC paths; added the missing runtime pandas dependency.

## TDD evidence

- RED: the initial first-hit test failed with `ModuleNotFoundError` because the labels package did not exist.
- GREEN: `py -m pytest tests/unit/test_labels.py -v` — 11 passed.
- RED: the coarse OHLC dual-hit case failed when labels accepted close-only series only.
- GREEN: the same focused suite passes with explicit `AMBIGUOUS` handling.

## Verification

- `py -m pytest -v` — 214 passed.
- `py -m ruff check src tests` — passed.
- `py -m mypy src` — passed for 31 source files.
- `git diff --check` — no whitespace errors (only configured LF-to-CRLF working-copy warnings).
- `py -m pip install --dry-run .` — resolved the declared pandas runtime dependency.

## Concerns

None. Generated `src/nifty_signal_engine.egg-info/` metadata from the dry-run is untracked and intentionally excluded from commits.

## Review correction — canonical IST timestamps

- Fix commit: `b6b089792d7edc8df4f4e0c124f6532c41a5e53f` (`fix: canonicalize triple-barrier timestamps to IST`).
- RED: a UTC-indexed path with an equivalent Asia/Kolkata origin returned a UTC `barrier_time`; the new regression failed its canonical-timezone assertion.
- GREEN: inputs are normalized once to `Asia/Kolkata` after aware-index validation and before origin lookup, slicing, or calculation. Returned barrier timestamps therefore use the global timezone while retaining the original instants.
- Verification: `py -m pytest tests/unit/test_labels.py -v` — 12 passed; `py -m pytest -q` — 215 passed; `py -m ruff check src tests` and `py -m mypy src` — passed.
