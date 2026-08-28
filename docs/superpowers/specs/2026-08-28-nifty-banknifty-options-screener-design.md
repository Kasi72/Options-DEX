# NIFTY and BANK NIFTY Options Signal Engine — Design Specification

**Date:** 28 August 2026  
**Status:** Approved design, pending user review  
**Initial delivery:** Corrected local research engine with Streamlit interface  
**Later delivery:** Full-stack web application after the signal engine is validated

## 1. Purpose

Build a research-grade, precision-first options screener for intraday NIFTY and BANK NIFTY trading. The system will estimate the underlying index's directional and volatility distributions, decide whether an option trade has positive expected value after realistic costs, select an appropriate liquid contract or defined-risk spread, and emit an advisory signal or `NO TRADE`.

The primary predictive objective is precision among emitted signals, subject to minimum trade coverage, positive net expectancy, calibrated uncertainty, and bounded drawdown. The engine does not maximize the number of predictions. It deliberately rejects most observations when doing so improves the reliability of the remaining signals.

The system must not claim guaranteed prediction. A model becomes eligible to emit production advisory signals only after it passes chronological, out-of-sample, cost-adjusted validation and live shadow evaluation. Until then, outputs are explicitly labelled research or paper signals.

The existing Streamlit application is the prototype and mathematical reference, not the target architecture. It contains useful GEX, DEX, VEX, CHEX, zero-level, and dashboard concepts, but combines API access, calculations, state, CSV persistence, alerts, and presentation in one file. The initial implementation will preserve the original files and create a separate modular project.

## 2. Product scope

### 2.1 Intraday engine — first priority

- Instruments: NIFTY and BANK NIFTY.
- Forecast horizons: separate 15-, 30-, and 60-minute models.
- Positions are closed before the market close.
- Prediction target: underlying/futures movement, not raw option-premium direction.
- Trade choices: long call, long put, defined-risk debit spread, or `NO TRADE`.
- `BUY` means a bullish underlying signal executed through a long call or call debit spread.
- `SELL` means a bearish underlying signal executed through a long put or put debit spread; it does not authorize naked option writing.
- Initial operating mode: advisory only; no automatic order submission.
- Signals are selective. The system is explicitly permitted and expected to abstain.

### 2.2 Positional engine — later independent phase

- Horizon: approximately two to five trading days.
- Separate labels, features, models, calibration, contract selection, and validation.
- Explicit modelling of overnight gaps, theta, IV changes, expiry risk, and scheduled events.
- Defined-risk spreads preferred when appropriate.
- An unsuccessful intraday position is never silently converted into an overnight position.

### 2.3 Full-stack application — after financial validation

The final web product will use a Python API/backend, persistent background collection, a time-series database, real-time browser updates, authentication, encrypted broker credentials, model-health monitoring, and historical analytics. The full-stack interface is deliberately deferred until the financial engine and data pipeline are trustworthy.

## 3. Non-goals for the initial implementation

- Guaranteed direction or profit.
- Automatic broker order execution.
- Naked option selling.
- Tick-level neural networks without suitable tick history.
- Multi-broker integration.
- Social-media or news sentiment.
- Mobile-native application.
- Large libraries of overlapping technical indicators.
- Positional signals before the intraday engine is validated.
- Visual polish that delays data integrity, replay, or validation.

## 4. Signal contract

Each emitted signal has a stable identifier, creation timestamp, model version, feature schema version, calculation version, and lifecycle state.

Required signal fields:

- Instrument: NIFTY or BANK NIFTY.
- Direction: bullish/BUY or bearish/SELL.
- Action: buy call, buy put, debit spread, or no trade.
- Forecast horizon: 15, 30, or 60 minutes.
- Calibrated probabilities for up, down, and no meaningful move.
- Expected underlying move and return quantiles.
- Expected realized volatility.
- Data confidence, regime confidence, and model-distribution confidence.
- Selected expiry, strike or legs, and contract identifiers.
- Entry trigger and executable premium range.
- Underlying invalidation level.
- Emergency option-premium stop.
- Targets, time stop, latest entry time, and forced exit time.
- Estimated total trading friction.
- Expected net P&L, probability of profit, and target-before-stop probability.
- Supportive factors, contradictory factors, and cancellation conditions.

Signal lifecycle:

```text
CANDIDATE -> ARMED -> ACTIVE -> CLOSED
     |          |         |
     +------> CANCELLED <--+
```

Every candidate, rejection, cancellation, execution simulation, and outcome is stored. The engine never suppresses failed candidates from research records.

## 5. Target architecture

```text
Dhan and auxiliary market sources
              |
              v
      Independent collector
              |
              v
   Immutable raw snapshots
              |
              v
 Validation and normalization
              |
              v
     Calculation engine
              |
              v
       Feature pipeline
              |
              v
 Regime + direction + volatility models
              |
              v
 Calibration + uncertainty + signal gates
              |
              v
 Contract selector + risk engine
              |
              v
 Signal log + Streamlit research interface
```

Proposed package boundaries:

```text
nifty_signal_engine/
|-- config/
|   |-- instruments.py
|   |-- market_hours.py
|   `-- settings.py
|-- domain/
|   |-- option_chain.py
|   |-- exposure.py
|   `-- signal.py
|-- data/
|   |-- dhan_client.py
|   |-- collector.py
|   |-- validators.py
|   `-- repositories.py
|-- calculations/
|   |-- greeks.py
|   |-- gex_dex.py
|   |-- vex_chex.py
|   `-- zero_levels.py
|-- features/
|   |-- option_structure.py
|   |-- incremental_flow.py
|   |-- futures_features.py
|   |-- price_features.py
|   `-- regime_features.py
|-- signals/
|   |-- rule_baseline.py
|   |-- probability_model.py
|   |-- contract_selector.py
|   `-- risk_engine.py
|-- backtesting/
|   |-- labels.py
|   |-- simulator.py
|   |-- walk_forward.py
|   `-- reports.py
|-- monitoring/
|   |-- data_quality.py
|   `-- model_health.py
|-- tests/
`-- streamlit_app.py
```

Units communicate through typed domain models and explicit repository interfaces. The Streamlit layer only requests and presents results; it does not fetch market data, calculate exposures, maintain collection timing, or own persistence.

## 6. Data foundation

### 6.1 Required observations

For NIFTY and BANK NIFTY:

- Underlying spot price and source timestamp.
- Relevant futures price, volume, OI, and basis.
- Complete option-chain snapshots for selected active expiries.
- Strike, type, expiry, LTP, bid, ask, quote quantities, spread, volume, prior volume, OI, prior OI, IV, and API-provided Greeks.
- Locally calculated Greeks for comparison.
- India VIX.
- Market and constituent breadth when a reliable source is available.
- Trading date, minutes since open, minutes to close, DTE, and expiry classification.
- API health, source freshness, and validation results.

The collector discovers active expiries through the broker API. Instrument identifiers, market hours, holiday rules, lot sizes, and rate assumptions are configuration/reference data rather than scattered constants.

### 6.2 Persistence

- Raw responses are immutable and saved before transformation.
- SQLite in WAL mode stores configuration, operational state, signal records, and model metadata during the local phase.
- Partitioned Parquet files store full strike-level historical snapshots.
- CSV is export-only and is not a source of truth.
- Repository interfaces allow PostgreSQL/TimescaleDB to replace local storage during the full-stack phase.
- Every derived result links to its raw snapshot and records the calculation parameters used.

### 6.3 Collection cadence

- Collect approximately every 10 to 15 seconds, subject to API constraints and health.
- Produce stable one-minute feature bars for the initial 15- to 60-minute models.
- The collector runs independently of browser sessions and UI changes.
- Restarts recover previous valid counters and session state without corrupting incremental volume or OI.

### 6.4 Data-quality gate

No prediction is generated when required data is stale, internally inconsistent, insufficiently complete, outside validated ranges, or affected by a session reset. Examples include missing strikes, invalid IV/Greeks, unexpected expiry metadata, abnormal spread, resetting cumulative counters, clock skew, and unavailable futures confirmation.

Invalid conditions produce explicit statuses such as `DATA_INVALID`, `STALE_SOURCE`, `INSUFFICIENT_STRIKES`, or `NO_VALID_ZERO_CROSSOVER`. They do not produce fabricated numeric substitutes.

## 7. Corrections to the prototype

The first implementation corrects these known issues before adding predictive models:

1. Remove or explicitly rename the extra `/1000` exposure scaling so crore units are mathematically correct.
2. Perform session rollover before computing changes and rolling statistics.
3. Return nullable zero levels plus a reason when no genuine sign-changing root exists.
4. Mark pre-open and zero-volume snapshots as non-tradable.
5. Require a complete configurable minimum history for rolling Z-scores.
6. Use timezone-aware `Asia/Kolkata` timestamps.
7. Discover active expiries instead of assuming a weekday.
8. Store all calculation parameters with each observation.
9. Separate cumulative volume from incremental volume.
10. Track intraday OI changes and session resets.
11. Compare local and API Greeks and flag material disagreement.
12. Replace swallowed exceptions with structured errors and health records.
13. Never overwrite an incompatible data file automatically.
14. Version schemas, calculations, features, models, and cost schedules.

## 8. Exposure layers

The engine maintains three distinct exposure concepts:

### 8.1 Structural exposure

OI-weighted GEX, DEX, VEX, and CHEX. This estimates slower positioning structure, important strikes, and possible volatility regimes.

### 8.2 Incremental flow exposure

Exposure weighted by volume added since the prior valid snapshot. One- and five-minute aggregates measure new activity rather than cumulative daily growth.

### 8.3 Scenario-based dealer exposure

Multiple plausible dealer-side assumptions are calculated. Scenario agreement becomes a confidence feature. Material disagreement produces `UNCERTAIN` instead of forcing a dealer-gamma sign.

Each layer records sign, magnitude, normalized magnitude, change, concentration, distance from spot, calculation status, and data confidence.

## 9. Prediction targets

Separate NIFTY and BANK NIFTY models operate at 15-, 30-, and 60-minute horizons.

Each timestamp is labelled `UP`, `DOWN`, or `NO_MEANINGFUL_MOVE` using volatility-adjusted triple barriers on the underlying/futures path. The first upper or lower barrier hit before the horizon determines direction; failure to reach either barrier produces the no-move class.

Barriers scale with recent realized volatility and are fitted without future information. The target is a meaningful move capable of supporting an option trade, not a trivially positive or negative close.

The model outputs:

- `P(up)`, `P(down)`, and `P(no move)`.
- Expected underlying return.
- Conditional return quantiles.
- Expected realized volatility.
- Probability estimate reliability.
- Feature-distribution confidence.
- Model-consensus and disagreement measures.
- Selective-classifier acceptance/rejection status.
- Sequential evidence state and entry-boundary status.

Option profitability is evaluated separately using executable quotes, IV, theta, spreads, slippage, and costs.

### 9.1 Precision-first decision objective

Prediction quality is evaluated conditionally on signal emission. The system reports separate BUY precision, SELL precision, coverage, abstention, false-signal rate, and net expectancy. Ordinary accuracy across all timestamps is secondary because most market observations may not contain a tradable edge.

The engine learns a risk-coverage frontier: accepting fewer observations should reduce error among accepted observations. A dedicated rejector chooses `NO TRADE` when calibrated probability, return intervals, model consensus, regime familiarity, data quality, or option economics are insufficient.

BUY and SELL use separate probability models, calibration, thresholds, and error budgets. A separate no-move/tradability model estimates whether any meaningful option opportunity exists.

## 10. Feature families

### 10.1 Price and futures

- Returns over several recent windows.
- VWAP distance and slope.
- Opening-range location and breakout state.
- Trend strength, efficiency, and acceleration.
- Realized volatility, ATR, and range expansion.
- Distance from session high and low.
- Futures/spot basis and basis change.
- Futures volume and OI change.
- NIFTY/BANK NIFTY cross-confirmation.

### 10.2 Structural options

- Scenario-based OI GEX/DEX.
- Normalized exposure magnitude.
- Valid zero-gamma/zero-delta distance.
- Call/put wall distance and stability.
- Gamma and OI concentration near spot.
- OI changes by strike and moneyness.
- Put-call ratios.
- Cross-expiry exposure and scenario agreement.

### 10.3 Intraday option flow

- Incremental call/put volume at one and five minutes.
- Incremental GEX/DEX and acceleration.
- Intraday OI changes.
- Volume/OI divergence.
- IV change by side and moneyness.
- Skew, term-structure, and ATM straddle movement.
- Quote spread and liquidity.
- Approximate signed flow only when quote quality supports it.

### 10.4 Volatility, expiry, and market context

- India VIX level and change.
- ATM IV and IV term structure.
- Put-call skew.
- Implied versus realized volatility.
- DTE and expiry-day classification.
- Minutes since open and to close.
- Breadth and major-constituent confirmation when available.

The initial model uses a disciplined shortlist of approximately 30 to 60 stable features. Additional features survive only when walk-forward ablation demonstrates value.

## 11. Mathematical and model stack

### 11.1 Baselines

- Regularized multinomial logistic regression.
- Deterministic rule-based confluence.
- Simple momentum, VWAP/trend, opening-range, and mean-reversion strategies.
- Existing GEX/DEX sign logic.

### 11.2 Primary predictive stack

- Separate gradient-boosted BUY and SELL classifiers.
- A separate no-move/tradability classifier.
- Conditional quantile regression for the return distribution.
- Markov-switching or Hidden Markov regime probabilities.
- Kalman/state-space estimates for latent trend, basis, and smoothed flow.
- GJR-GARCH/HAR-style and machine-learning volatility ensemble.
- CUSUM followed by Bayesian online change-point comparison.
- Chronological probability calibration using sigmoid, isotonic, or temperature scaling selected on validation data.
- A meta-labelling model that estimates whether each primary BUY/SELL candidate will reach its target before its stop after costs.
- A selective classifier with an explicit reject option and measured risk-coverage curve.
- Adaptive time-series conformal prediction and conformal risk control for uncertainty-aware abstention and a validation-time false-signal budget after a stable probability model exists.
- Sequential probability-ratio or anytime-valid evidence accumulation before signal emission.
- Regime-aware mixture of experts with dynamic model averaging only when it beats static ensembles.
- Explicit Bayesian/ensemble disagreement as a rejection feature rather than hiding disagreement through averaging.
- Conformalized quantile intervals for asymmetric return uncertainty.
- Smooth, constrained IV-surface modelling for contract valuation and anomaly detection.
- Survival/competing-risks modelling for target-before-stop probability after enough labelled trades exist.
- Conditional mutual-information and stability screening for incremental feature value.
- Extreme-value modelling for tail-aware stops, stress, and sizing.
- Copula/dependence modelling for scenario and portfolio-risk analysis only when validated.
- Distributionally robust thresholds and contract decisions after the ordinary walk-forward system is stable.

### 11.3 Later tick-data research

- Hawkes processes for self-exciting flow.
- Order-book microprice and queue imbalance.
- Trade signing and futures/options lead-lag.
- Sequence/deep models only after simpler methods and sufficient tick data establish a justified need.

Every advanced component requires walk-forward, ablation, cost-adjusted, stability, and drift tests. Complexity that does not improve unseen-period performance is removed.

### 11.4 Precision decision sequence

```text
Validated data
    -> regime and change-point state
    -> separate BUY and SELL probabilities
    -> regime-specialist model weighting
    -> quantile return and volatility forecasts
    -> chronological probability calibration
    -> model-disagreement test
    -> meta-label: target before stop after costs?
    -> conformal uncertainty and risk filter
    -> sequential evidence confirmation
    -> option economics and liquidity
    -> BUY / SELL / NO TRADE
```

No component may relax a failed upstream data-quality gate. Sequential confirmation cannot rescue invalid data, and a high raw model score cannot override an uncertain conformal set or negative expected option value.

## 12. Leakage prevention

- Features use only data available at the prediction timestamp.
- Completed-bar features are shifted before label attachment when required.
- Entire days remain together in chronological splits.
- No random row-level train/test splitting.
- Scalers, imputers, selectors, regimes, calibrators, and thresholds are fitted only inside each training/validation cycle.
- Overlapping forecast horizons use purging and embargo.
- Full-day totals are never exposed to intraday predictions.
- Replay uses the same feature and signal code as live operation.

## 13. Signal gating

A trade candidate must pass all gates:

1. Data quality and freshness.
2. Feature-distribution familiarity.
3. Regime stability.
4. Separate calibrated BUY/SELL directional probability.
5. Selective-classifier acceptance and acceptable risk-coverage point.
6. Model-consensus/disagreement limit.
7. Meta-labelled target-before-stop probability.
8. Conformal uncertainty/risk acceptance.
9. Sequential evidence boundary confirmation.
10. Expected-move sufficiency.
11. Volatility and IV suitability.
12. Option liquidity and spread.
13. Expected value after costs.
14. Risk/reward requirements.
15. Duplicate, cooldown, time-of-day, expiry, and portfolio-risk controls.

Thresholds are learned from walk-forward validation and may differ by instrument, horizon, time, expiry state, regime, and strategy. Unstable conditions require higher thresholds. Failure of any mandatory gate returns `NO TRADE` with reasons.

The engine maintains independent BUY and SELL false-signal budgets. When rolling calibration or selective precision deteriorates, it automatically raises the affected threshold, reduces coverage, and, if necessary, disables that direction until shadow revalidation passes.

## 14. Contract and strategy selection

### 14.1 Strategy hierarchy

- Long call/put when direction is strong, expected move is sufficient, IV is reasonable, and liquidity is high.
- Debit spread when direction is strong but IV/decay makes a naked option unattractive and both legs remain executable.
- `NO TRADE` when no candidate clears an absolute expected-value and quality threshold.

Naked option selling is excluded from the initial system.

### 14.2 Expiry and strike

- Compare active liquid expiries rather than always choosing the nearest.
- Same-day-expiry trades require independently validated stricter rules; otherwise select a later liquid expiry or abstain.
- Initial strike search normally covers liquid contracts around 0.35 to 0.70 absolute delta.
- Deep OTM options are normally rejected.

### 14.3 Candidate scoring

Compare expected net P&L, probability of profit, target-before-stop probability, adverse excursion, bid-ask cost, liquidity, theta, vega, gamma responsiveness, quote quality, and uncertainty.

An IV-surface-aware Monte Carlo scenario engine simulates bounded joint spot, IV, skew, and time-decay paths for contract comparison. Directional prediction and contract economics remain separate auditable stages.

## 15. Trade management and risk

- Entry occurs on the next executable quote after confirmation, not on the quote that generated the prediction.
- Primary invalidation is based on the underlying thesis: structural level, VWAP, volatility-adjusted barrier, probability collapse, or regime transition.
- An option-premium emergency stop protects against contract-specific failures.
- Targets derive from return quantiles, triple barriers, structural levels, expected move, and target-before-stop probability.
- Each signal has a confirmation timeout, hard holding-time limit, latest entry, and forced session exit.
- A 15-minute forecast cannot silently become a 60-minute trade.

Initial sizing uses fixed fractional account risk. Fractional Kelly is eligible only after probability and payoff calibration are stable and is always capped and confidence-adjusted.

Hard protections:

- Per-trade, daily, per-instrument, and combined portfolio risk caps.
- Correlation-aware combined NIFTY/BANK NIFTY exposure cap.
- Maximum trades per instrument.
- Cooldown after stops and pause after consecutive losses.
- No averaging down or martingale sizing.
- No overlapping duplicate signals.
- No new positions under degraded data/model health.

## 16. Backtesting and validation

### 16.1 Historical depth

- About 20 sessions: pipeline validation only.
- About 60 sessions: preliminary research.
- About 120 sessions: first meaningful walk-forward evaluation.
- 250 or more sessions preferred for regime stability.

If trustworthy historical full-chain data is unavailable, the system collects forward and remains in research mode. It does not fabricate a production model from the supplied one-day aggregate CSV.

### 16.2 Replay and execution

The event-driven simulator processes immutable snapshots chronologically through production calculation, feature, prediction, gate, contract, and risk code. Entry uses the next available executable quote. Backtests include bid/ask, configurable slippage, brokerage, exchange fees, applicable taxes, two-leg costs, missing data, rejected entries, and versioned cost schedules.

### 16.3 Walk-forward protocol

- Chronological train, validation, calibration, and untouched test blocks.
- Rolling or expanding windows.
- Purging and embargo for overlapping labels.
- NIFTY and BANK NIFTY evaluated separately before portfolio aggregation.
- Baseline comparisons and component ablation.

### 16.4 Metrics

Statistical metrics include Brier score, log loss, calibration curves, class precision/recall, conformal coverage, quantile coverage, block-bootstrap intervals, multiple-testing-aware comparisons, and selection-bias-aware risk statistics.

Precision-specific reports include separate BUY/SELL precision, false BUY/SELL rates, precision-recall curves, risk-coverage curves, precision at fixed coverage, coverage at fixed error, abstention rate, conformal set size, sequential false-alarm rate, and meta-model lift over the primary model.

Economic metrics include net expectancy, profit factor, win/loss distribution, drawdown, Sharpe/Sortino, favorable/adverse excursion, target-before-stop rate, holding time, friction, P&L concentration, consecutive losses, and breakdowns by regime, month, expiry, horizon, and time of day.

### 16.5 Promotion gates

A model may enter shadow mode only when it has positive cost-adjusted walk-forward expectancy, outperforms the baseline, has acceptable direction-specific precision, calibration and drawdown, demonstrates a stable risk-coverage trade-off, remains stable across several unseen windows and threshold perturbations, is not dominated by a few trades or expiry days, has a meaningful independent trade count, and has no unresolved leakage or data-quality defects.

An initial governance target is approximately 200 or more test trades for a model/horizon combination and a profit factor around 1.2 or better, but promotion considers confidence intervals and cross-period stability rather than optimizing mechanically to one threshold.

No precision claim is published without its test-trade count, coverage/abstention rate, confidence interval, cost assumptions, and unseen evaluation period. BUY and SELL are promoted independently. If one direction fails, the other may remain eligible while the failed direction returns `NO TRADE`.

## 17. Operating modes and monitoring

1. **Research:** replay-only, no actionable live alerts.
2. **Shadow:** live paper signals, suggested minimum 20 sessions.
3. **Limited live advisory:** minimum size, hard caps, human confirmation.
4. **Production advisory:** normal configured sizing; still no automatic orders.

Monitor feature drift, prediction drift, direction-specific precision, risk-coverage behaviour, sequential false-alarm rates, probability calibration, expectancy, slippage, regime mix, data-source behaviour, IV-surface anomalies, ensemble disagreement, and out-of-distribution rates. Failed health thresholds first reduce coverage and then demote the affected model/direction to shadow or no-trade mode automatically.

## 18. Error handling and auditability

- Errors are typed, logged, and surfaced with correlation identifiers.
- Raw credentials and tokens never appear in logs or frontend payloads.
- Partial failures cannot produce a normal signal.
- Signals, revisions, cancellations, and outcomes are append-only audit events.
- Every model artefact records training window, feature list, hyperparameters, calibration method, code/schema versions, and validation report.
- Replays are deterministic given the same raw data, configuration, and versions.

## 19. Testing strategy

- Unit tests for Greeks, units, exposure signs, root finding, rolling windows, session resets, labels, and costs.
- Mathematical property tests for boundaries, monotonicity where applicable, finite outputs, and null/status behaviour.
- Contract tests for broker-response normalization.
- Repository tests for persistence and recovery.
- Replay determinism tests.
- Leakage tests using deliberately shifted synthetic data.
- Signal lifecycle and risk-invariant tests.
- Integration tests from saved raw snapshot to final no-trade/trade decision.
- Fault-injection tests for stale data, API failures, malformed strikes, quote gaps, restarts, and counter resets.

## 20. Implementation phases

### Phase 0 — preserve and baseline

Preserve original inputs, capture current behaviour, create configuration and dependency structure, and encode known defects as regression tests.

### Phase 1 — modular calculation engine

Extract and correct Greeks, exposures, zero levels, walls, rolling statistics, DTE, sessions, and units. Maintain a thin Streamlit compatibility layer.

### Phase 2 — collection and storage

Implement Dhan adapter, expiry discovery, independent collection, immutable snapshots, SQLite/Parquet repositories, replay, incremental flow/OI, retries, and data-quality gates.

### Phase 3 — features and baselines

Implement feature families, triple-barrier labels, deterministic regime, rule baseline, logistic baseline, and transparent reports.

### Phase 4 — backtesting platform

Implement event replay, realistic cost/execution simulation, purged walk-forward evaluation, calibration reports, ablation, sensitivity analysis, and leakage audits.

### Phase 5 — advanced models

Add advanced mathematical components one at a time under evidence gates.

### Phase 6 — contract selection and advisory signals

Add expiry/strike/spread selection, Monte Carlo comparison, risk controls, lifecycle, and audited Streamlit signals.

### Phase 7 — shadow and limited live validation

Collect live paper outcomes, reconcile execution realism, and promote models independently.

### Phase 8 — positional engine

Build and validate the separate two- to five-day system.

### Phase 9 — full-stack web application

Migrate repositories to PostgreSQL/TimescaleDB, add FastAPI, background workers, WebSockets, Next.js/React, authentication, encrypted credentials, notifications, and operational deployment.

## 21. Initial implementation boundary

The first implementation plan covers Phases 0 through 4:

- A new modular project that preserves the original prototype.
- Corrected and tested calculations.
- NIFTY and BANK NIFTY data collection.
- Immutable strike-level persistence and replay.
- Feature pipeline and transparent baselines.
- Triple-barrier labels and chronological validation framework.
- Temporary Streamlit research interface.

This boundary creates the infrastructure required to discover whether a dependable edge exists. Advanced models, contract emission, positional trading, and the full-stack application are subsequent evidence-gated plans.

## 22. Completion criteria for the initial implementation

- Original files remain unchanged and recoverable.
- New project installs reproducibly and contains no embedded credentials.
- Calculation corrections pass unit, property, and regression tests.
- Collector stores immutable, complete, versioned snapshots for both instruments.
- Session restarts and rollover do not corrupt incremental features.
- Replay reproduces live-derived features exactly.
- Invalid data returns explicit non-tradable statuses.
- Baseline signals and triple-barrier outcomes run without look-ahead leakage.
- Walk-forward reports include realistic costs and baseline comparisons.
- Streamlit displays data health, research probabilities, and `NO TRADE` where production gates are not satisfied.
- Streamlit reports separate BUY/SELL precision, coverage, abstention, false-signal rate, and confidence intervals without presenting raw probability as guaranteed accuracy.
- Separate BUY/SELL models, the selective rejector, meta-label interface, sequential evidence state, and precision-governance hooks are represented in the architecture even before enough data exists to train production candidates.
- No unvalidated model is described as dependable or production-ready.

## 23. Source context

- Prototype: `C:\Users\drkkr\Downloads\app.py`
- Supplied aggregate example: `C:\Users\drkkr\Downloads\nifty_vol_gex_dex_log.csv`
- Dhan option-chain API documentation: <https://dhanhq.co/docs/v2/option-chain/>
- Cboe research on dealer gamma and volatility: <https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf>
- Scikit-learn probability calibration: <https://scikit-learn.org/stable/modules/calibration.html>
- Statsmodels Markov-switching examples: <https://www.statsmodels.org/dev/examples/notebooks/generated/markov_regression.html>
- ARCH volatility forecasting: <https://bashtage.github.io/arch/doc/univariate/univariate_volatility_forecasting.html>
- Selective classification and risk-coverage: <https://jmlr2020.csail.mit.edu/papers/v11/el-yaniv10a.html>
- Conformal risk control: <https://people.eecs.berkeley.edu/~angelopoulos/publications/downloads/conformal-risk.pdf>
- Dynamic model averaging under changing conditions: <https://www.sciencedirect.com/science/article/pii/S0927539817300816>
