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
