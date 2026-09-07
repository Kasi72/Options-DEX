# NIFTY Signal Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Precision-first research infrastructure for NIFTY and BANKNIFTY options. The engine converts timestamped option-chain observations into auditable exposures, Greeks, zero-levels, causal features, calibrated directional research, realistic replay results, and walk-forward validation reports.

> **Current status:** research and shadow-monitoring only. Every public signal is explicitly `RESEARCH / NO_TRADE`. No broker credentials, order submission, or live execution path is included.

## What it provides

- Strict, timezone-aware domain models for NIFTY and BANKNIFTY.
- Dhan option-chain normalization with source/receipt provenance and expiry discovery.
- Immutable raw evidence, quality decisions, restart-safe collection, SQLite/WAL persistence, and Parquet exports.
- Black–Scholes Greeks, gamma/delta exposure, zero-level/root analysis, rolling baselines, and session-aware features.
- Causal labels and selective BUY/SELL research predictions with abstention, quality gates, and separate instrument models.
- Deterministic replay with quote chronology checks, bid/ask-bounded fills, slippage, and explicit INR costs.
- Purged, embargoed, IST walk-forward validation with calibration, precision/coverage, confidence intervals, drawdown, and cost-aware economics.
- Registry-backed promotion artifacts for a future governed promotion phase.
- Read-only Streamlit dashboard for collection health, exposures, zero-levels, research probabilities, actions, and reports.

## Safety and research contract

The engine is deliberately conservative. Invalid, stale, incomplete, mixed-instrument, out-of-range, or unaudited observations are non-tradable and never become normal signals. BUY/SELL direction is research output only; the intended eventual option expression is a long call/call debit spread for bullish views and a long put/put debit spread for bearish views—never naked option writing.

The supplied one-day aggregate CSV is useful for schema inspection and exploratory checks, but it is not sufficient to train or validate a dependable predictor. Promotion requires chronological multi-session data, verified calibration/validation artifacts, adequate sample sizes, shadow monitoring, and explicit governance approval.

## Requirements

- Python 3.12–3.14
- Windows, macOS, or Linux

## Installation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e ".[dev]"
```

On macOS/Linux, use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Quick start

```powershell
nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY
nifty-signal inspect-session --instrument NIFTY --session-date 2026-08-26
streamlit run src/nifty_signal_engine/streamlit_app.py
```

The dashboard reads persisted JSON/SQLite research artifacts. It does not collect data, migrate schemas, delete files, call a broker, or submit orders.

## Supabase + scheduled Dhan collection

The repository includes a privilege-separated cloud path. Apply
`supabase/migrations/20260907000000_market_data.sql` in your Supabase SQL
editor. The migration enables RLS and grants the public dashboard read access
only to published rows. The scheduled collector uses the service-role key only
inside the scheduler to insert market snapshots; never place that key in
Streamlit Secrets or browser code.

Configure these GitHub Actions secrets:

```text
DHAN_CLIENT_ID
DHAN_ACCESS_TOKEN
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

The workflow in `.github/workflows/collect-market-data.yml` runs every five
minutes on weekdays and can also be started manually. Configure these
Streamlit Cloud secrets for the read-only dashboard:

```toml
SUPABASE_URL = "https://<project-ref>.supabase.co"
SUPABASE_ANON_KEY = "<publishable-or-anon-key>"
```

The collector currently publishes audited market snapshots and provenance.
Feature/research artifacts remain governed outputs and are shown only when
their own validated artifacts exist. Supabase Cron or another scheduler can be
used instead of GitHub Actions if preferred.

## License

This project is licensed under the [MIT License](LICENSE). The license permits
use, modification, and redistribution subject to preservation of the copyright
and license notice. Market data providers, broker APIs, and third-party assets
remain subject to their own terms and licenses.

## Data and provenance

Raw observations are persisted before normalization or feature calculation. Each downstream artifact carries instrument, observation/availability timestamps, source provenance, schema/version information, and quality decisions. CSV is an export format only; SQLite and partitioned Parquet are the analytical sources of truth.

For a new feed, provide complete option-chain fields (instrument, expiry, strike, option type, bid/ask/last, open interest, volume, and authoritative observation time). Underlying traded volume, futures, and cross-index inputs remain nullable until genuinely supplied; the engine never fabricates them from option volume.

## Architecture

```text
source adapter → immutable raw store → normalization/quality gates
              → exposures/Greeks/features → labels and baselines
              → replay and walk-forward validation → read-only dashboard
```

Key packages: `config`, `calculations`, `features`, `signals`, `backtesting`, `monitoring`, and `streamlit_app.py` under `src/nifty_signal_engine`.

## Verification

```powershell
py -m pytest -q
py -m ruff check src tests
py -m mypy src
```

The repository currently contains 301 automated tests. Broad `mypy src tests` discovery can report a pre-existing duplicate `tests/factories.py` module; source-package type checking remains clean.

## Project documents

- [Design specification](docs/superpowers/specs/2026-08-28-nifty-banknifty-options-screener-design.md)
- [Implementation plan](docs/superpowers/plans/2026-08-28-phases-0-4-implementation-plan.md)
- [Changelog](CHANGELOG.md)
- Task validation reports: `task-*.md`

## License and disclaimer

No license has been selected yet. Add an appropriate license before redistribution. This software is research tooling, not investment advice. Options involve substantial risk; validate data, execution assumptions, and regulatory obligations independently before making any trading decision.
