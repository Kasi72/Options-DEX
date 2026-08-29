# Task 3 report — verified Greeks and explicit units

## Status

Complete. The calculation package now supplies frozen Black-Scholes Greek
results and explicit rupee/crore conversions. Invalid non-positive spot,
strike, time-to-expiry, and volatility inputs raise `ValueError` rather than
being converted into zero exposures.

## Files

- `src/nifty_signal_engine/calculations/__init__.py`
- `src/nifty_signal_engine/calculations/greeks.py`
- `src/nifty_signal_engine/calculations/units.py`
- `tests/unit/test_greeks.py`
- `tests/unit/test_units.py`

## RED evidence

Before implementation, `py -m pytest tests/unit/test_greeks.py
tests/unit/test_units.py -v` stopped at collection with the expected
`ModuleNotFoundError: No module named 'nifty_signal_engine.calculations'` for
both test modules.

## GREEN evidence and checks

- `py -m pytest tests/unit/test_greeks.py tests/unit/test_units.py -v`: 12 passed.
- `py -m ruff check src/nifty_signal_engine/calculations tests/unit/test_greeks.py tests/unit/test_units.py`: passed.
- `py -m mypy src/nifty_signal_engine/calculations tests/unit/test_greeks.py tests/unit/test_units.py`: passed.
- `py -m pytest tests/unit -v`: 20 passed.

The Greek property test checks finite outputs, strict call/put delta ranges,
and positive gamma over bounded valid inputs. Regression tests check call/put
delta parity and gamma equality. Unit tests verify that one crore is exactly
10,000,000 rupees without an extra scale factor.

## Concerns

The implementation uses SciPy as required by the task and it is available in
the current environment. `pyproject.toml` does not yet declare SciPy, but that
file is outside Task 3's allowed paths; a dependency-declaration task should
add it before a fresh-install release.

## Commit

`feat: add verified greeks and exposure units`

## Follow-up review fixes

The controller granted a narrow exception to add `scipy>=1.16.0` as a runtime
dependency. This supports the project's declared Python range through 3.14 and
matches the SciPy API used by the calculation module.

### RED evidence

`py -m pytest tests/unit/test_greeks.py -v` failed 10 newly added cases because
the Greek calculator accepted `NaN` or positive/negative infinity for spot,
strike, years, rate, or volatility.

### GREEN evidence and checks

- `py -m pytest tests/unit/test_greeks.py tests/unit/test_units.py -v`: 27 passed.
- `py -m ruff check src/nifty_signal_engine/calculations tests/unit/test_greeks.py tests/unit/test_units.py`: passed.
- `py -m mypy src/nifty_signal_engine/calculations tests/unit/test_greeks.py tests/unit/test_units.py`: passed.
- `py -m pip install --dry-run .`: metadata built successfully and resolved `scipy>=1.16.0` (installed 1.16.3).
- `py -m pytest tests/unit -v`: 35 passed.

### Concerns

No reference originals or live-execution paths were changed. The metadata
dry-run generated an untracked `src/nifty_signal_engine.egg-info/` directory;
it is not part of either Task 3 commit. Its recursive removal was rejected by
the environment policy, so it remains a safe local cleanup item.
