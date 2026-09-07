# Changelog

All notable changes to NIFTY Signal Engine are documented here. The project is pre-1.0 and follows a research-first release discipline.

## [Unreleased]

- Added deterministic, fail-closed market-regime classification for completed feature rows.
- Added weighted directional probability blending with pairwise model-disagreement scoring.
- Added unit coverage for regime warm-up, trend detection, ensemble disagreement, and instrument isolation.
- Future: connect verified promotion artifacts to a governed shadow-to-live promotion process.
- Future: add longer authoritative multi-session NIFTY/BANKNIFTY datasets and production feed adapters.
- Future: add deployment packaging, observability, and authenticated multi-user web access.

## [0.1.0] — 2026-09-07

### Added

- Strict Pydantic domain contracts, IST timestamp normalization, and separate NIFTY/BANKNIFTY configuration.
- Black–Scholes Greeks with validated units and non-finite-input rejection.
- Gamma/delta exposure, deterministic zero-level analysis, rolling/session state, and out-of-order protection.
- Dhan normalization, expiry discovery, duplicate-contract detection, provenance, and fail-closed quality gates.
- SQLite/WAL persistence with immutable raw evidence, schema checks, recovery, and partitioned Parquet output.
- Causal features covering completed bars, flow, GEX/DEX, acceleration, and explicit missing-data statuses.
- Selective signal baselines with tie abstention, scaling, chronology/provenance gates, and `RESEARCH/NO_TRADE` enforcement.
- Deterministic replay with global quote chronology, realistic bid/ask fills, slippage, transaction costs, and explicit INR accounting.
- Purged/embargoed IST walk-forward validation with expanding folds, train-only fitting, calibration, precision/coverage, confidence intervals, drawdown, cost-aware economics, and governance suppression.
- Non-forgeable registry-backed promotion artifacts for future governed promotion.
- Read-only Streamlit dashboard for health, exposures, zero-levels, research probabilities, actions, and reports.
- CLI workflows for fixture/research collection and session inspection.
- 301 automated tests covering domain rules, calculations, provenance, persistence, replay, validation, and UI boundaries.

### Safety

- No order-submission, broker-credential, or live-execution path.
- Invalid, stale, incomplete, unaudited, or mixed-instrument evidence is suppressed from normal signal output.
- Original user-provided Downloads files remain untouched; copied references are hash-checked.

### Known limitations

- The supplied one-day aggregate CSV cannot establish dependable predictive performance.
- Dhan-only timestamps without authoritative exchange time remain research-only.
- Broad `mypy src tests` may encounter a pre-existing duplicate `tests/factories.py` module; `mypy src` passes.
- No license has been selected yet.
