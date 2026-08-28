# NIFTY and BANK NIFTY Signal Engine Phases 0–4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a corrected, modular, replayable research engine for NIFTY and BANK NIFTY that collects immutable option-chain data, derives leakage-safe features, establishes transparent BUY/SELL baselines, and performs cost-aware chronological validation without presenting unvalidated outputs as production signals.

**Architecture:** Create a new Python package beside the preserved prototype. An independent collector writes immutable raw snapshots and normalized SQLite/Parquet records; pure calculation and feature modules consume typed domain objects; replay, labels, baselines, and walk-forward evaluation use the same code paths as live research; Streamlit remains a thin research UI.

**Tech Stack:** Python `>=3.12,<3.15`, Pydantic 2, pandas, NumPy, SciPy, PyArrow/Parquet, SQLAlchemy 2 with SQLite WAL, HTTPX, scikit-learn, Streamlit, Plotly, Typer, pytest, Hypothesis, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-08-28-nifty-banknifty-options-screener-design.md`

## Global Constraints

- Preserve `C:\Users\drkkr\Downloads\app.py` and `C:\Users\drkkr\Downloads\nifty_vol_gex_dex_log.csv` byte-for-byte; copy them into `reference/` with SHA-256 provenance.
- Initial instruments are NIFTY and BANK NIFTY; instrument IDs, lot sizes, expiries, and market rules belong in configuration, not calculation code.
- Use timezone-aware `Asia/Kolkata` timestamps throughout.
- Store rupee exposure and crore exposure explicitly; never apply an undocumented scale factor.
- Raw API snapshots are immutable and saved before normalization or feature calculation.
- CSV is export-only; SQLite and partitioned Parquet are the local sources of truth.
- Invalid, stale, incomplete, or out-of-range data returns an explicit non-tradable status; it never produces a normal signal.
- `BUY` means bullish underlying exposure through a long call/call debit spread; `SELL` means bearish underlying exposure through a long put/put debit spread, not naked writing.
- Research outputs default to `NO_TRADE` until chronological validation and shadow-promotion gates pass.
- No automatic order submission is implemented in Phases 0–4.
- Tests precede implementation in every task; commits occur only after the task's focused and regression tests pass.

---

## File map

```text
pyproject.toml                         Packaging, dependencies, tool configuration
README.md                              Local setup, modes, commands, safety boundary
.env.example                           Credential names without secrets
reference/                             Immutable copies and SHA-256 manifest
src/nifty_signal_engine/
  config/                              Instrument, session, and runtime settings
  domain/                              Typed immutable market, exposure, and signal models
  calculations/                        Pure Greeks, exposures, roots, rolling statistics
  data/                                Dhan adapter, validators, repositories, collector
  features/                            One-minute bars and leakage-safe features
  signals/                             Labels, transparent baselines, selective decision types
  backtesting/                         Replay, costs, fills, walk-forward reports
  monitoring/                          Data-quality and model-health status
  cli.py                               Collection, replay, validation, and export commands
  streamlit_app.py                     Thin research interface
tests/
  unit/                                Pure and deterministic unit/property tests
  integration/                         Repository, collector, replay, and UI-contract tests
  fixtures/                            Sanitized broker responses and expected snapshots
```

---

### Task 1: Bootstrap the preserved, testable project

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `README.md`
- Create: `reference/manifest.json`
- Create: `reference/original_app.py` by byte-for-byte copy
- Create: `reference/nifty_vol_gex_dex_log.csv` by byte-for-byte copy
- Create: `src/nifty_signal_engine/__init__.py`
- Create: `tests/unit/test_provenance.py`

**Interfaces:**
- Produces package version `nifty_signal_engine.__version__`.
- Produces provenance records `{source_path, copied_path, sha256}` used by later regression tests.

- [ ] **Step 1: Initialize version control and copy immutable references**

Run from the project root:

```powershell
git init
New-Item -ItemType Directory -Force reference | Out-Null
Copy-Item -LiteralPath 'C:\Users\drkkr\Downloads\app.py' -Destination 'reference\original_app.py'
Copy-Item -LiteralPath 'C:\Users\drkkr\Downloads\nifty_vol_gex_dex_log.csv' -Destination 'reference\nifty_vol_gex_dex_log.csv'
```

Expected: copied files exist and original files remain unchanged.

- [ ] **Step 2: Write the failing provenance test**

```python
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_hashes_match_manifest() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "reference/manifest.json").read_text())
    for item in manifest["files"]:
        assert sha256(root / item["copied_path"]) == item["sha256"]
```

- [ ] **Step 3: Run the test and verify failure**

Run: `py -m pytest tests/unit/test_provenance.py -v`

Expected: FAIL because the project dependencies and manifest do not exist yet.

- [ ] **Step 4: Add packaging and the exact provenance manifest**

`reference/manifest.json` must contain:

```json
{
  "files": [
    {
      "source_path": "C:\\Users\\drkkr\\Downloads\\app.py",
      "copied_path": "reference/original_app.py",
      "sha256": "4C7DC443C60321C452E615CC2FB904C2DF9C68E1BE044D617239A49DDB5C8C63"
    },
    {
      "source_path": "C:\\Users\\drkkr\\Downloads\\nifty_vol_gex_dex_log.csv",
      "copied_path": "reference/nifty_vol_gex_dex_log.csv",
      "sha256": "E9D828EB4FF44506097BF7C2D3FD63A161E3DC4EFE499FFB5898957D6EB5F1A0"
    }
  ]
}
```

Configure `pyproject.toml` with `src` layout, a `dev` extra containing pytest/Hypothesis/Ruff/mypy, and command entry point:

```toml
[project.scripts]
nifty-signal = "nifty_signal_engine.cli:app"
```

`src/nifty_signal_engine/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 5: Install and verify**

Run:

```powershell
py -m pip install -e '.[dev]'
py -m pytest tests/unit/test_provenance.py -v
py -m ruff check .
```

Expected: PASS; Ruff reports no violations.

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml .env.example README.md reference src tests
git commit -m "chore: bootstrap preserved signal-engine project"
```

---

### Task 2: Define immutable domain models and session configuration

**Files:**
- Create: `src/nifty_signal_engine/config/settings.py`
- Create: `src/nifty_signal_engine/config/instruments.py`
- Create: `src/nifty_signal_engine/config/market_hours.py`
- Create: `src/nifty_signal_engine/domain/market.py`
- Create: `src/nifty_signal_engine/domain/exposure.py`
- Create: `src/nifty_signal_engine/domain/signal.py`
- Create: `tests/factories.py`
- Test: `tests/unit/test_domain_models.py`
- Test: `tests/unit/test_market_hours.py`

**Interfaces:**
- Produces `InstrumentConfig`, `RuntimeSettings`, `OptionQuote`, `OptionChainSnapshot`, `DataQualityStatus`, `ExposureResult`, `ZeroLevelResult`, and `ResearchSignal`.
- Produces `session_date(timestamp) -> date` and `market_phase(timestamp) -> MarketPhase`.
- Produces test factories `aware`, `make_quote`, and `make_chain` used by later tasks.

- [ ] **Step 1: Write failing immutable/timezone tests**

```python
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo
import pytest

from nifty_signal_engine.config.market_hours import MarketPhase, market_phase
from nifty_signal_engine.domain.market import OptionQuote


def test_quote_requires_timezone_and_is_frozen() -> None:
    with pytest.raises(ValueError):
        OptionQuote(timestamp=datetime(2026, 8, 26, 9, 16), strike=24_300, option_type="CE")


def test_market_phase_distinguishes_preopen_and_regular() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    assert market_phase(datetime(2026, 8, 26, 9, 10, tzinfo=tz)) is MarketPhase.PREOPEN
    assert market_phase(datetime(2026, 8, 26, 9, 16, tzinfo=tz)) is MarketPhase.REGULAR
```

- [ ] **Step 2: Run focused tests**

Run: `py -m pytest tests/unit/test_domain_models.py tests/unit/test_market_hours.py -v`

Expected: FAIL with missing modules.

- [ ] **Step 3: Implement typed models and explicit statuses**

Use Pydantic frozen models and enums. The required minimal signatures are:

```python
class OptionQuote(BaseModel, frozen=True):
    timestamp: datetime
    strike: float
    option_type: Literal["CE", "PE"]
    expiry: date | None = None
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int = 0
    oi: int = 0
    previous_oi: int | None = None
    iv: float | None = None
    api_delta: float | None = None
    api_gamma: float | None = None

    @model_validator(mode="after")
    def validate_timestamp(self) -> "OptionQuote":
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self
```

```python
class OptionChainSnapshot(BaseModel, frozen=True):
    instrument: Literal["NIFTY", "BANKNIFTY"]
    source_timestamp: datetime
    received_at: datetime
    spot: float
    expiry: date
    quotes: tuple[OptionQuote, ...]
```

```python
class SignalAction(StrEnum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"
    NO_TRADE = "NO_TRADE"
```

Create `tests/factories.py` with deterministic constructors:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

IST = ZoneInfo("Asia/Kolkata")


def aware(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(IST)


def make_quote(*, timestamp: str, strike: float = 24_300, option_type: Literal["CE", "PE"] = "CE",
               volume: int = 0, oi: int = 100, iv: float = 0.15,
               bid: float = 99.0, ask: float = 101.0) -> OptionQuote:
    return OptionQuote(timestamp=aware(timestamp), strike=strike, option_type=option_type,
                       expiry=date(2026, 9, 1), volume=volume, oi=oi, iv=iv,
                       bid=bid, ask=ask)


def make_chain(*, timestamp: str, call_volume: int = 0,
               put_volume: int = 0, spot: float = 24_300) -> OptionChainSnapshot:
    ts = aware(timestamp)
    return OptionChainSnapshot(
        instrument="NIFTY", source_timestamp=ts, received_at=ts, spot=spot,
        expiry=date(2026, 9, 1),
        quotes=(
            make_quote(timestamp=timestamp, option_type="CE", volume=call_volume),
            make_quote(timestamp=timestamp, option_type="PE", volume=put_volume),
        ),
    )
```

- [ ] **Step 4: Implement session helpers**

Use `ZoneInfo("Asia/Kolkata")`, regular phase `09:15 <= time < 15:30`, post-close otherwise, and reject naive timestamps.

- [ ] **Step 5: Run checks and commit**

Run:

```powershell
py -m pytest tests/unit/test_domain_models.py tests/unit/test_market_hours.py -v
py -m mypy src/nifty_signal_engine/config src/nifty_signal_engine/domain
py -m ruff check src tests
```

Expected: all pass.

```powershell
git add src/nifty_signal_engine/config src/nifty_signal_engine/domain tests/unit
git commit -m "feat: define immutable market and signal domain"
```

---

### Task 3: Implement verified Greeks and explicit units

**Files:**
- Create: `src/nifty_signal_engine/calculations/greeks.py`
- Create: `src/nifty_signal_engine/calculations/units.py`
- Test: `tests/unit/test_greeks.py`
- Test: `tests/unit/test_units.py`

**Interfaces:**
- Produces `black_scholes_greeks(spot, strike, years, rate, volatility, option_type) -> Greeks`.
- Produces `rupees_to_crore(value: float) -> float` and `crore_to_rupees(value: float) -> float`.

- [ ] **Step 1: Write failing numerical and unit tests**

```python
from math import isclose
from nifty_signal_engine.calculations.greeks import black_scholes_greeks
from nifty_signal_engine.calculations.units import crore_to_rupees, rupees_to_crore


def test_call_put_delta_parity_and_gamma_equality() -> None:
    call = black_scholes_greeks(100, 100, 0.25, 0.07, 0.20, "CE")
    put = black_scholes_greeks(100, 100, 0.25, 0.07, 0.20, "PE")
    assert isclose(call.delta - put.delta, 1.0, abs_tol=1e-12)
    assert isclose(call.gamma, put.gamma, rel_tol=1e-12)


def test_crore_round_trip_has_no_extra_thousand_scale() -> None:
    assert rupees_to_crore(10_000_000) == 1.0
    assert crore_to_rupees(1.0) == 10_000_000
```

- [ ] **Step 2: Verify failure**

Run: `py -m pytest tests/unit/test_greeks.py tests/unit/test_units.py -v`

Expected: FAIL with missing modules.

- [ ] **Step 3: Implement pure calculations**

Return a frozen `Greeks(delta, gamma, vanna, call_charm, put_charm)` object. Reject non-positive spot/strike/years/volatility with `ValueError`; do not convert invalid input to zeros. Use SciPy normal CDF/PDF and preserve the prototype formulas for regression comparison.

- [ ] **Step 4: Add boundary/property tests**

Use Hypothesis to assert finite values, `0 < call_delta < 1`, `-1 < put_delta < 0`, and positive gamma for valid bounded inputs.

- [ ] **Step 5: Run and commit**

Run: `py -m pytest tests/unit/test_greeks.py tests/unit/test_units.py -v`

Expected: PASS.

```powershell
git add src/nifty_signal_engine/calculations tests/unit/test_greeks.py tests/unit/test_units.py
git commit -m "feat: add verified greeks and exposure units"
```

---

### Task 4: Implement exposure aggregation, walls, and honest zero levels

**Files:**
- Create: `src/nifty_signal_engine/calculations/exposures.py`
- Create: `src/nifty_signal_engine/calculations/zero_levels.py`
- Test: `tests/unit/test_exposures.py`
- Test: `tests/unit/test_zero_levels.py`

**Interfaces:**
- Consumes `OptionQuote`, `Greeks`, `InstrumentConfig`.
- Produces `aggregate_exposure(quotes, spot, years, rate, lot_size, weighting) -> ExposureResult`.
- Produces `find_zero_level(objective, lower, upper) -> ZeroLevelResult` with statuses `VALID`, `NO_SIGN_CHANGE`, `ALL_ZERO`, or `NUMERICAL_ERROR`.

- [ ] **Step 1: Write failing sign/unit tests**

```python
def test_net_gex_is_call_plus_signed_put() -> None:
    quotes = (
        make_quote(timestamp="2026-08-26T10:00:00+05:30", option_type="CE", oi=1_000),
        make_quote(timestamp="2026-08-26T10:00:00+05:30", option_type="PE", oi=900),
    )
    result = aggregate_exposure(quotes, 24_300, 7 / 365, 0.07, 65, "OI")
    assert result.net_gex_rupees == pytest.approx(
        result.call_gex_rupees + result.put_gex_rupees
    )
    assert result.net_gex_crore == pytest.approx(result.net_gex_rupees / 10_000_000)
```

- [ ] **Step 2: Write zero-level failure-status tests**

```python
def test_all_zero_curve_returns_null_level() -> None:
    result = find_zero_level(lambda _: 0.0, 23_000, 25_000)
    assert result.level is None
    assert result.status is ZeroLevelStatus.ALL_ZERO


def test_no_sign_change_is_not_reported_as_spot() -> None:
    result = find_zero_level(lambda x: x * x + 1, 23_000, 25_000)
    assert result.level is None
    assert result.status is ZeroLevelStatus.NO_SIGN_CHANGE
```

- [ ] **Step 3: Verify failures**

Run: `py -m pytest tests/unit/test_exposures.py tests/unit/test_zero_levels.py -v`

Expected: FAIL with missing functions.

- [ ] **Step 4: Implement minimal pure aggregators and Brent root solving**

Keep structural (`OI`) and cumulative/incremental volume weighting explicit. Wall results include strike, signed exposure, and selected strike universe; missing walls return `None`, not zero.

- [ ] **Step 5: Run property/regression tests and commit**

Run: `py -m pytest tests/unit/test_exposures.py tests/unit/test_zero_levels.py -v`

Expected: PASS.

```powershell
git add src/nifty_signal_engine/calculations tests/unit/test_exposures.py tests/unit/test_zero_levels.py
git commit -m "feat: calculate auditable exposures and zero levels"
```

---

### Task 5: Implement session-safe rolling statistics and state

**Files:**
- Create: `src/nifty_signal_engine/calculations/rolling.py`
- Create: `src/nifty_signal_engine/data/session_state.py`
- Test: `tests/unit/test_rolling.py`
- Test: `tests/unit/test_session_state.py`

**Interfaces:**
- Produces `rolling_zscore(current, history, minimum=20, window=20) -> float | None`.
- Produces `SessionState.update(snapshot) -> IncrementalSnapshot`.
- Resets volume deltas at trading-date changes and returns `None` for unavailable changes.

- [ ] **Step 1: Write failing minimum-history and rollover tests**

```python
def test_zscore_is_unavailable_before_minimum_history() -> None:
    assert rolling_zscore(10.0, [1.0] * 19, minimum=20, window=20) is None


def test_first_snapshot_of_new_session_has_no_change() -> None:
    state = SessionState()
    state.update(make_chain(timestamp="2026-08-26T15:29:00+05:30", call_volume=1000))
    result = state.update(make_chain(timestamp="2026-08-27T09:15:00+05:30", call_volume=10))
    assert result.incremental_volume is None
    assert result.cross_session is True
```

- [ ] **Step 2: Verify failure, implement, and rerun**

Run: `py -m pytest tests/unit/test_rolling.py tests/unit/test_session_state.py -v`

Expected before implementation: FAIL. Expected after implementation: PASS.

- [ ] **Step 3: Add prototype-regression test**

Load the supplied CSV and assert its first-row Z-scores are classified as invalid cross-session artefacts, while net arithmetic and GD/DD arithmetic remain valid.

- [ ] **Step 4: Commit**

```powershell
git add src/nifty_signal_engine/calculations/rolling.py src/nifty_signal_engine/data/session_state.py tests
git commit -m "fix: make rolling features session safe"
```

---

### Task 6: Normalize Dhan option-chain and discover expiries

**Files:**
- Create: `src/nifty_signal_engine/data/dhan_client.py`
- Create: `src/nifty_signal_engine/data/normalizer.py`
- Create: `tests/fixtures/dhan_option_chain.json`
- Create: `tests/fixtures/dhan_expiries.json`
- Test: `tests/unit/test_dhan_normalizer.py`
- Test: `tests/integration/test_dhan_client.py`

**Interfaces:**
- Produces async `DhanClient.fetch_expiries(instrument) -> tuple[date, ...]`.
- Produces async `DhanClient.fetch_option_chain(instrument, expiry) -> RawSnapshot`.
- Produces `normalize_option_chain(raw, instrument, received_at) -> OptionChainSnapshot`.

- [ ] **Step 1: Save sanitized response fixtures**

Fixtures must include CE/PE quotes, missing-side strike, IV expressed as percentage, API Greeks, bid/ask, volume, OI, previous OI, and source spot. Remove credentials and account identifiers.

- [ ] **Step 2: Write failing normalization tests**

```python
def test_normalizer_converts_iv_percent_and_preserves_api_greeks() -> None:
    snapshot = normalize_fixture("dhan_option_chain.json")
    call = next(q for q in snapshot.quotes if q.option_type == "CE")
    assert 0 < call.iv < 1
    assert call.api_gamma is not None
    assert call.bid <= call.ask
```

- [ ] **Step 3: Write mocked HTTP tests**

Use `httpx.MockTransport` to assert exact endpoints, headers, timeout, non-200 typed errors, invalid JSON typed errors, and expiry-list parsing. No test contacts the live API.

- [ ] **Step 4: Implement client/normalizer and run**

Run: `py -m pytest tests/unit/test_dhan_normalizer.py tests/integration/test_dhan_client.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/nifty_signal_engine/data tests/fixtures tests/unit/test_dhan_normalizer.py tests/integration/test_dhan_client.py
git commit -m "feat: normalize dhan chains and discover expiries"
```

---

### Task 7: Persist immutable raw snapshots and normalized records

**Files:**
- Create: `src/nifty_signal_engine/data/schema.py`
- Create: `src/nifty_signal_engine/data/repositories.py`
- Create: `src/nifty_signal_engine/data/parquet_store.py`
- Test: `tests/integration/test_repositories.py`

**Interfaces:**
- Produces `SnapshotRepository.save_raw(raw) -> SnapshotId`.
- Produces `SnapshotRepository.save_normalized(snapshot_id, snapshot) -> None`.
- Produces `SnapshotRepository.iter_session(instrument, session_date) -> Iterator[OptionChainSnapshot]`.
- Raw snapshots are content-addressed; re-saving identical content is idempotent and does not mutate stored bytes.

- [ ] **Step 1: Write failing idempotency and recovery tests**

```python
def test_raw_snapshot_is_content_addressed_and_immutable(repo, raw_payload) -> None:
    first = repo.save_raw(raw_payload)
    second = repo.save_raw(raw_payload)
    assert first == second
    assert repo.read_raw(first) == raw_payload
```

Also assert SQLite uses WAL, schema versions are recorded, and Parquet partitions use `instrument=.../session_date=YYYY-MM-DD/`.

- [ ] **Step 2: Implement schema and repositories**

SQLite tables: `schema_versions`, `raw_snapshots`, `normalized_snapshots`, `option_quotes`, `collector_state`, `research_signals`, `signal_events`, and `experiment_runs`. Store raw compressed JSON plus SHA-256; never update raw rows.

- [ ] **Step 3: Run crash/reopen integration tests**

Run: `py -m pytest tests/integration/test_repositories.py -v`

Expected: PASS before and after closing/reopening the repository.

- [ ] **Step 4: Commit**

```powershell
git add src/nifty_signal_engine/data tests/integration/test_repositories.py
git commit -m "feat: persist immutable market snapshots"
```

---

### Task 8: Build the independent collector and data-quality gate

**Files:**
- Create: `src/nifty_signal_engine/data/collector.py`
- Create: `src/nifty_signal_engine/monitoring/data_quality.py`
- Create: `src/nifty_signal_engine/cli.py`
- Test: `tests/unit/test_data_quality.py`
- Test: `tests/integration/test_collector.py`

**Interfaces:**
- Produces `assess_snapshot(current, previous, now) -> DataQualityReport`.
- Produces async `Collector.collect_once(instrument) -> CollectionResult`.
- CLI commands: `nifty-signal collect-once`, `collect`, `inspect-session`.

`DataQualityReport` is a frozen model with `tradable: bool`, `codes: tuple[DataQualityCode, ...]`, `checked_at: datetime`, and `details: dict[str, str]`.

- [ ] **Step 1: Write failing quality tests**

```python
def test_stale_snapshot_is_non_tradable() -> None:
    current = make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100)
    now = aware("2026-08-26T09:16:00+05:30")
    report = assess_snapshot(current, None, now)
    assert report.tradable is False
    assert DataQualityCode.STALE_SOURCE in report.codes


def test_preopen_zero_volume_is_non_tradable_not_error() -> None:
    current = make_chain(timestamp="2026-08-26T09:10:00+05:30")
    now = aware("2026-08-26T09:10:01+05:30")
    report = assess_snapshot(current, None, now)
    assert report.tradable is False
    assert DataQualityCode.PREOPEN in report.codes
```

- [ ] **Step 2: Write collector ordering test**

Assert `save_raw` is called before normalization, features, or any signal callback. Assert UI state is not a collector dependency.

- [ ] **Step 3: Implement quality checks and collector**

Checks include timestamp freshness, strike coverage, bid/ask validity, IV range, Greek finiteness, expiry consistency, source spot validity, cumulative-counter reset, and clock skew. Retries use bounded exponential backoff and never duplicate raw content.

- [ ] **Step 4: Run CLI smoke with fixtures**

Run:

```powershell
py -m pytest tests/unit/test_data_quality.py tests/integration/test_collector.py -v
nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY
```

Expected: tests pass; fixture snapshot is saved and marked research-only.

- [ ] **Step 5: Commit**

```powershell
git add src/nifty_signal_engine/data/collector.py src/nifty_signal_engine/monitoring src/nifty_signal_engine/cli.py tests
git commit -m "feat: collect and gate market snapshots independently"
```

---

### Task 9: Produce replayable one-minute bars and leakage-safe features

**Files:**
- Create: `src/nifty_signal_engine/features/bars.py`
- Create: `src/nifty_signal_engine/features/option_structure.py`
- Create: `src/nifty_signal_engine/features/flow.py`
- Create: `src/nifty_signal_engine/features/price.py`
- Create: `src/nifty_signal_engine/features/pipeline.py`
- Test: `tests/unit/test_feature_pipeline.py`
- Test: `tests/integration/test_feature_replay.py`

**Interfaces:**
- Produces `FeaturePipeline.on_snapshot(snapshot, quality) -> FeatureRow | None`.
- Produces immutable `FeatureRow` with `available_at`, instrument, session, schema version, feature mapping, and quality codes.
- No feature may use a timestamp greater than `available_at`.

- [ ] **Step 1: Write failing incremental-flow and availability tests**

```python
def test_incremental_volume_uses_previous_valid_snapshot_only() -> None:
    pipeline = FeaturePipeline()
    quality = DataQualityReport(
        tradable=True, codes=(), checked_at=aware("2026-08-26T09:15:15+05:30"), details={}
    )
    pipeline.on_snapshot(
        make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100), quality
    )
    row = pipeline.on_snapshot(
        make_chain(timestamp="2026-08-26T09:15:15+05:30", call_volume=125), quality
    )
    assert row.values["call_volume_increment"] == 25


def test_feature_row_rejects_future_source_timestamp() -> None:
    with pytest.raises(ValueError, match="future source timestamp"):
        FeatureRow(
            available_at=aware("2026-08-26T10:00:00+05:30"), instrument="NIFTY",
            session_date=date(2026, 8, 26), schema_version="1",
            values={"spot_return_1m": 0.001}, quality_codes=(),
            source_timestamps=(aware("2026-08-26T10:00:01+05:30"),),
        )
```

- [ ] **Step 2: Implement one-minute completion semantics**

A one-minute bar becomes available only after its minute closes. Store both 10–15 second flow updates and completed one-minute aggregates; models consume completed bars unless a future plan explicitly validates partial-bar features.

- [ ] **Step 3: Implement the initial 30–60 stable features**

Include session returns, VWAP distance/slope, opening-range state, realized volatility, structural OI GEX/DEX, incremental GEX/DEX, wall/zero-level distance with validity flags, OI/volume concentration, IV/skew/straddle change, nullable futures-basis fields with explicit missing status, time, DTE, and nullable cross-index fields with explicit missing status.

- [ ] **Step 4: Verify deterministic replay**

Run: `py -m pytest tests/unit/test_feature_pipeline.py tests/integration/test_feature_replay.py -v`

Expected: replaying the same saved snapshots twice yields byte-identical serialized feature rows.

- [ ] **Step 5: Commit**

```powershell
git add src/nifty_signal_engine/features tests/unit/test_feature_pipeline.py tests/integration/test_feature_replay.py
git commit -m "feat: derive leakage-safe replayable features"
```

---

### Task 10: Generate triple-barrier labels without look-ahead leakage

**Files:**
- Create: `src/nifty_signal_engine/signals/labels.py`
- Test: `tests/unit/test_labels.py`

**Interfaces:**
- Produces `label_triple_barrier(prices, origin, horizon, upper_return, lower_return) -> TripleBarrierOutcome`.
- Outcome includes label `UP`, `DOWN`, or `NO_MOVE`, barrier timestamp, terminal return, MFE, and MAE.

- [ ] **Step 1: Write exact path tests**

```python
def test_first_barrier_hit_wins() -> None:
    index = pd.to_datetime([
        "2026-08-26T10:00:00+05:30", "2026-08-26T10:01:00+05:30",
        "2026-08-26T10:02:00+05:30",
    ])
    prices = pd.Series([100.0, 101.1, 98.5], index=index)
    result = label_triple_barrier(prices, prices.index[0], 180, 0.01, -0.01)
    assert result.label is DirectionLabel.UP
    assert result.barrier_time == prices.index[1]


def test_no_barrier_is_no_move() -> None:
    index = pd.to_datetime([
        "2026-08-26T10:00:00+05:30", "2026-08-26T10:01:00+05:30",
        "2026-08-26T10:02:00+05:30",
    ])
    prices = pd.Series([100.0, 100.2, 99.9], index=index)
    assert label_triple_barrier(prices, prices.index[0], 120, 0.01, -0.01).label is DirectionLabel.NO_MOVE
```

- [ ] **Step 2: Implement and test tie/missing-data rules**

If both barriers are crossed inside one coarse bar and intrabar ordering is unavailable, label the observation `AMBIGUOUS` and exclude it from model fitting rather than guessing. Missing terminal coverage produces `INSUFFICIENT_FUTURE_DATA`.

- [ ] **Step 3: Run and commit**

```powershell
py -m pytest tests/unit/test_labels.py -v
git add src/nifty_signal_engine/signals/labels.py tests/unit/test_labels.py
git commit -m "feat: label volatility-adjusted directional outcomes"
```

---

### Task 11: Add transparent BUY/SELL baselines and reject-by-default signals

**Files:**
- Create: `src/nifty_signal_engine/signals/baselines.py`
- Create: `src/nifty_signal_engine/signals/selective.py`
- Create: `src/nifty_signal_engine/signals/service.py`
- Test: `tests/unit/test_baselines.py`
- Test: `tests/unit/test_selective_signals.py`

**Interfaces:**
- Produces `RuleBaseline.predict(row) -> DirectionProbabilities`.
- Produces `LogisticBaseline.fit(frame, labels) -> LogisticBaseline` and `.predict_proba(row)`.
- Produces `SelectiveDecision.evaluate(prediction, quality, economics=None) -> ResearchSignal`.
- Produces `allowed_actions(direction: Literal["BUY", "SELL"]) -> frozenset[SignalAction]`.
- In Phases 0–4, every actionable-looking result remains `mode=RESEARCH`; missing validation/economics forces `NO_TRADE`.

- [ ] **Step 1: Write failing semantic safety tests**

```python
def test_bearish_direction_maps_to_buy_put_not_naked_sell() -> None:
    assert allowed_actions("SELL") == frozenset({
        SignalAction.BUY_PUT, SignalAction.PUT_DEBIT_SPREAD, SignalAction.NO_TRADE
    })


def test_unvalidated_model_cannot_emit_trade() -> None:
    prediction = DirectionProbabilities(up=0.90, down=0.05, no_move=0.05)
    quality = DataQualityReport(
        tradable=True, codes=(), checked_at=aware("2026-08-26T10:00:00+05:30"), details={}
    )
    signal = SelectiveDecision(validated=False).evaluate(prediction, quality)
    assert signal.action is SignalAction.NO_TRADE
    assert "MODEL_NOT_PROMOTED" in signal.reasons
```

- [ ] **Step 2: Implement rule and logistic baselines**

The rule baseline combines completed-bar price/VWAP trend, valid regime, and flow agreement. The logistic baseline uses a pipeline with median imputation fitted only on training data and multinomial logistic regression. Both expose nullable calibrated-probability fields but do not claim calibration before Task 13.

- [ ] **Step 3: Implement selective scaffolding**

Store separate BUY/SELL thresholds, model disagreement, meta-label, conformal, sequential-evidence, and economics fields as nullable research fields. Any unavailable mandatory production gate returns `NO_TRADE` with explicit reasons.

- [ ] **Step 4: Run and commit**

```powershell
py -m pytest tests/unit/test_baselines.py tests/unit/test_selective_signals.py -v
git add src/nifty_signal_engine/signals tests/unit/test_baselines.py tests/unit/test_selective_signals.py
git commit -m "feat: establish safe directional baselines"
```

---

### Task 12: Build cost-aware event replay and fill simulation

**Files:**
- Create: `src/nifty_signal_engine/backtesting/costs.py`
- Create: `src/nifty_signal_engine/backtesting/fills.py`
- Create: `src/nifty_signal_engine/backtesting/replay.py`
- Test: `tests/unit/test_costs.py`
- Test: `tests/unit/test_fills.py`
- Test: `tests/integration/test_event_replay.py`

**Interfaces:**
- Produces versioned `CostSchedule.estimate(trade) -> CostBreakdown`.
- Produces `FillSimulator.enter_long(candidate, next_quote) -> Fill | Rejection`.
- Produces `ReplayEngine.run(session) -> ReplayResult` using production calculation/features/signals.

`TradeCandidate` has `signal_time: datetime` and `contract_id: str`. `ExecutableQuote` has `timestamp: datetime`, `contract_id: str`, `bid: float | None`, and `ask: float | None`.

- [ ] **Step 1: Write failing next-quote and spread tests**

```python
def test_long_entry_uses_next_ask_not_signal_ltp() -> None:
    candidate = TradeCandidate(
        signal_time=aware("2026-08-26T10:00:00+05:30"), contract_id="NIFTY-20260901-24300-CE"
    )
    quote = ExecutableQuote(
        timestamp=aware("2026-08-26T10:00:15+05:30"),
        contract_id=candidate.contract_id, bid=99, ask=101,
    )
    fill = FillSimulator().enter_long(candidate, quote)
    assert fill.price == 101


def test_crossed_or_missing_quote_rejects_entry() -> None:
    candidate = TradeCandidate(
        signal_time=aware("2026-08-26T10:00:00+05:30"), contract_id="NIFTY-20260901-24300-CE"
    )
    quote = ExecutableQuote(
        timestamp=aware("2026-08-26T10:00:15+05:30"),
        contract_id=candidate.contract_id, bid=102, ask=101,
    )
    result = FillSimulator().enter_long(candidate, quote)
    assert result.code is RejectionCode.INVALID_QUOTE
```

- [ ] **Step 2: Implement versioned cost components**

Represent brokerage, exchange charges, taxes, GST, regulatory fees, stamp duty, spread, and extra slippage as named components. Rates come from configuration and are printed in every report; no current rate is hidden in code.

- [ ] **Step 3: Implement replay and verify production-path reuse**

The replay engine calls the same `FeaturePipeline` and `SignalService` as research live mode. An integration spy test fails if a backtest-only feature calculation path is introduced.

- [ ] **Step 4: Run and commit**

```powershell
py -m pytest tests/unit/test_costs.py tests/unit/test_fills.py tests/integration/test_event_replay.py -v
git add src/nifty_signal_engine/backtesting tests
git commit -m "feat: replay signals with executable quotes and costs"
```

---

### Task 13: Implement purged walk-forward validation and calibration reports

**Files:**
- Create: `src/nifty_signal_engine/backtesting/splits.py`
- Create: `src/nifty_signal_engine/backtesting/metrics.py`
- Create: `src/nifty_signal_engine/backtesting/walk_forward.py`
- Create: `src/nifty_signal_engine/backtesting/reporting.py`
- Test: `tests/unit/test_purged_splits.py`
- Test: `tests/integration/test_walk_forward.py`

**Interfaces:**
- Produces `PurgedWalkForward.split(frame) -> Iterator[Fold]` with train, validation, calibration, test, and embargo metadata.
- Produces `evaluate_fold(fold, model_factory) -> FoldReport`.
- Produces separate BUY/SELL calibration, precision, risk-coverage, abstention, economic, and sample-size metrics.

- [ ] **Step 1: Write failing chronological/purge tests**

```python
def test_splits_are_chronological_and_embargoed() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    observations = pd.DataFrame({"feature": range(240), "label": ["UP", "DOWN"] * 120}, index=index)
    fold = next(PurgedWalkForward(horizon="60min", embargo="60min").split(observations))
    assert fold.train.index.max() < fold.validation.index.min()
    assert fold.validation.index.max() < fold.calibration.index.min()
    assert fold.calibration.index.max() < fold.test.index.min()
    assert fold.test.index.min() - fold.calibration.index.max() >= pd.Timedelta("60min")
```

- [ ] **Step 2: Implement calibration without training leakage**

Fit sigmoid, isotonic, and temperature candidates only on the calibration block; select by validation protocol defined before the untouched test report. Report Brier, log loss, reliability bins, and sample count for each direction.

- [ ] **Step 3: Implement precision governance reports**

For each instrument/horizon/direction, report precision, confidence interval, coverage, abstention, false-signal rate, precision at fixed coverage, coverage at fixed error, expectancy after costs, profit factor, drawdown, monthly/regime slices, and threshold sensitivity. Suppress headline precision when sample size is below the configured governance minimum.

- [ ] **Step 4: Verify no random split path exists**

Run: `rg -n "train_test_split|KFold\(" src/nifty_signal_engine`

Expected: no matches. Any future cross-validation class must be explicitly time-aware and purged.

- [ ] **Step 5: Run and commit**

```powershell
py -m pytest tests/unit/test_purged_splits.py tests/integration/test_walk_forward.py -v
git add src/nifty_signal_engine/backtesting tests
git commit -m "feat: validate directional precision chronologically"
```

---

### Task 14: Add the thin Streamlit research interface and final verification

**Files:**
- Create: `src/nifty_signal_engine/streamlit_app.py`
- Create: `tests/integration/test_ui_contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes repository, data-quality, feature, replay, and report services only.
- Displays collection health, latest research probabilities, zero-level validity, exposure layers, BUY/SELL precision with coverage/sample size, and explicit `NO_TRADE` reasons.
- Does not import `httpx`, calculate Greeks, write raw snapshots, sleep, or rerun the collector.

- [ ] **Step 1: Write failing UI boundary test**

```python
def test_streamlit_layer_has_no_collection_or_calculation_dependencies() -> None:
    source = Path("src/nifty_signal_engine/streamlit_app.py").read_text()
    forbidden = ["requests.", "httpx.", "time.sleep", "brentq", "norm.cdf", "open("]
    assert not any(token in source for token in forbidden)
```

- [ ] **Step 2: Implement the research UI**

Required panels:

- Collector/data-health status and last valid source time.
- NIFTY/BANK NIFTY latest structural and incremental exposure with units.
- Valid/invalid zero levels with reason.
- Research-only `P(up/down/no move)` for each horizon.
- BUY/SELL precision shown only with coverage, test count, confidence interval, and evaluation window.
- Current action fixed to `NO_TRADE` until promotion metadata exists.
- Replay and walk-forward report selector.

- [ ] **Step 3: Document commands and safety state**

README commands:

```powershell
nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY
nifty-signal inspect-session --instrument NIFTY --date 2026-08-26
nifty-signal replay --instrument NIFTY --date 2026-08-26
nifty-signal walk-forward --instrument NIFTY --horizon 30min
streamlit run src/nifty_signal_engine/streamlit_app.py
```

State explicitly that full-chain historical depth is required before promotion and that the supplied one-day aggregate CSV cannot train a dependable predictor.

- [ ] **Step 4: Run complete verification**

Run:

```powershell
py -m pytest -q
py -m ruff check .
py -m mypy src/nifty_signal_engine
nifty-signal collect-once --fixture tests/fixtures/dhan_option_chain.json --instrument NIFTY
nifty-signal replay --instrument NIFTY --date 2026-08-26
```

Expected: all tests/lint/types pass; fixture collection and replay complete; output remains research-only and `NO_TRADE` without promotion metadata.

- [ ] **Step 5: Verify original provenance again**

Run: `py -m pytest tests/unit/test_provenance.py -v`

Expected: both preserved hashes match the manifest.

- [ ] **Step 6: Commit**

```powershell
git add src/nifty_signal_engine/streamlit_app.py tests/integration/test_ui_contract.py README.md
git commit -m "feat: deliver research dashboard and validation workflow"
```

---

## Phase 0–4 completion checkpoint

Before starting advanced-model Phase 5, verify:

- The original application and CSV hashes remain unchanged.
- All tests, Ruff, and mypy pass.
- Raw fixtures and live snapshots are persisted before transformation.
- The collector runs without the UI.
- Session rollover cannot create first-row Z-score/change artefacts.
- Rupee/crore conversions contain no undocumented scale.
- All-zero and no-crossing exposure curves return null/status results.
- Replay deterministically reproduces feature rows.
- Labels and folds are chronological, purged, and embargoed.
- BUY and SELL semantics cannot trigger naked option writing.
- Precision is never displayed without coverage, sample size, confidence interval, costs, and unseen period.
- No unpromoted model can emit an actionable trade.
- The interface clearly displays research/shadow state and `NO_TRADE` reasons.

Only after this checkpoint and sufficient full-chain history should a separate Phase 5 plan introduce boosted BUY/SELL models, regime specialists, meta-labelling, conformal risk control, sequential evidence, IV-surface fitting, volatility ensembles, and survival models.
