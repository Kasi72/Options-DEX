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
nifty-signal inspect-session --instrument NIFTY --date 2026-08-26
nifty-signal replay --instrument NIFTY --date 2026-08-26
nifty-signal walk-forward --instrument NIFTY --horizon 30min
streamlit run src/nifty_signal_engine/streamlit_app.py
```

The current action is explicitly `RESEARCH/NO_TRADE` until independently
verified chronological validation and shadow-promotion metadata exist. Full
option-chain historical depth is required before promotion; the supplied
one-day aggregate CSV cannot train a dependable predictor.
