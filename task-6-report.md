# Task 6 report — Dhan option-chain normalization

## Implementation SHA

`b5b1fb7692e69e46493a38ee3aaf7efaf152b1cb` — `feat: normalize dhan chains and discover expiries`

`5f3ba4a0a020b9439afd65917c3639027854f38c` — `fix: harden dhan normalization boundary`

## Evidence

- TDD RED: `py -m pytest tests/unit/test_dhan_normalizer.py tests/integration/test_dhan_client.py -v` initially failed at collection because the Dhan boundary modules did not exist.
- Initial focused GREEN: the same command passed, 10 tests.
- Review-fix RED: duplicate normalized contract identity, `received_at` preceding capture, offset-equivalent aware timestamps, and missing `httpx` metadata failed before the corrective change.
- Review-fix focused GREEN: `py -m pytest tests/unit/test_dhan_normalizer.py tests/integration/test_dhan_client.py -v` passed, 14 tests.
- Static checks: `py -m ruff check src tests` and `py -m mypy src` passed.
- Unit suite: `py -m pytest tests/unit -v` passed, 84 tests.
- Packaging metadata: `py -m pip install --dry-run .` resolved `httpx>=0.27` and would install the project without installing it. The generated `*.egg-info` metadata was removed immediately after the check.

## Delivered boundary

- `RawSnapshot` retains unmodified response bytes and an aware capture timestamp before normalization.
- The Dhan adapter is mock-transport tested only; it does not submit orders. It uses distinct NIFTY (13) and BANKNIFTY (25) identifiers and parses exchange-provided expiry metadata rather than calculating weekday expiries.
- Normalization rejects malformed JSON, absent/malformed expiry or timestamps, invalid numeric fields, invalid IV percentages, bad bid/ask ordering, and empty strikes. It preserves a valid single CE or PE side and maps API delta/gamma into immutable domain quotes.
- Normalization additionally rejects duplicate normalized `(strike, option_type)` identities and a receipt timestamp preceding capture, while comparing aware timestamp instants correctly across offsets.

## Concern

No remaining Task 6 concerns. The review-authorized packaging exception declares `httpx>=0.27` as a runtime dependency. The adapter remains credential-injected and mock-transport tested; no live request or order path was executed.
