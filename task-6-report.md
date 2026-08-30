# Task 6 report — Dhan option-chain normalization

## Implementation SHA

`b5b1fb7692e69e46493a38ee3aaf7efaf152b1cb` — `feat: normalize dhan chains and discover expiries`

## Evidence

- TDD RED: `py -m pytest tests/unit/test_dhan_normalizer.py tests/integration/test_dhan_client.py -v` initially failed at collection because the Dhan boundary modules did not exist.
- Focused GREEN: the same command passed, 10 tests.
- Static checks: `py -m ruff check src tests` and `py -m mypy src` passed.
- Regression suite: `py -m pytest -v` passed, 85 tests.

## Delivered boundary

- `RawSnapshot` retains unmodified response bytes and an aware capture timestamp before normalization.
- The Dhan adapter is mock-transport tested only; it does not submit orders. It uses distinct NIFTY (13) and BANKNIFTY (25) identifiers and parses exchange-provided expiry metadata rather than calculating weekday expiries.
- Normalization rejects malformed JSON, absent/malformed expiry or timestamps, invalid numeric fields, invalid IV percentages, bad bid/ask ordering, and empty strikes. It preserves a valid single CE or PE side and maps API delta/gamma into immutable domain quotes.

## Concern

`httpx` is used by the required client and is available in the current development environment, but it is not yet declared in `pyproject.toml`. The Task 6 allowed-path brief did not authorize editing packaging metadata, so this task intentionally leaves that declaration to an authorized packaging change before a clean-environment install is relied on.
