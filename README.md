# NIFTY Signal Engine

A precision-first research engine for NIFTY and BANKNIFTY options signals.

## Development

Install the project and development tools with:

```powershell
py -m pip install -e '.[dev]'
```

Run the test suite with:

```powershell
py -m pytest
```

## Research workflow

All commands below operate in research/shadow mode. Collection persists
immutable raw evidence before normalization; the Streamlit interface only
reads local repository and report services. It never submits orders or uses
broker credentials.

```powershell
nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY
nifty-signal inspect-session --instrument NIFTY --session-date 2026-08-26
streamlit run src/nifty_signal_engine/streamlit_app.py
```

Replay and walk-forward reports are read-only artifacts selected in the
dashboard when produced by their respective backtesting services; those
services are not collector commands and are intentionally not re-run by the
UI.

The current action is explicitly `RESEARCH/NO_TRADE` until independently
verified chronological validation and shadow-promotion metadata exist. Full
option-chain historical depth is required before promotion; the supplied
one-day aggregate CSV cannot train a dependable predictor.
