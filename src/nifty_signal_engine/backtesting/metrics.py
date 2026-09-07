"""Leakage-independent calibration, selective and economic metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np  # type: ignore[import-untyped]


def _binary(labels: Sequence[object], positive: str) -> np.ndarray:
    allowed = {"UP", "DOWN", "NO_MOVE"}
    normalized = [str(value) for value in labels]
    if any(value not in allowed for value in normalized):
        raise ValueError("labels contain an unsupported outcome")
    return np.asarray([1.0 if value == positive else 0.0 for value in normalized], dtype=float)


def _probabilities(values: Sequence[float]) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or result.size == 0 or not np.isfinite(result).all():
        raise ValueError("probabilities must be a non-empty finite vector")
    if ((result < 0) | (result > 1)).any():
        raise ValueError("probabilities must be in [0, 1]")
    return result


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    direction: str
    method: str
    brier_score: float | None
    log_loss: float | None
    reliability_bins: tuple[dict[str, float], ...]
    sample_count: int

    @property
    def brier(self) -> float | None:
        return self.brier_score

    @property
    def sample_size(self) -> int:
        return self.sample_count

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "method": self.method,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "reliability_bins": [dict(item) for item in self.reliability_bins],
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class EconomicMetrics:
    expectancy_after_costs: float | None
    profit_factor: float | None
    max_drawdown: float | None
    gross_profit: float
    gross_loss: float
    total_costs: float
    sample_count: int

    @property
    def drawdown(self) -> float | None:
        return self.max_drawdown

    @property
    def net_expectancy(self) -> float | None:
        return self.expectancy_after_costs

    def to_dict(self) -> dict[str, object]:
        return {
            "expectancy_after_costs": self.expectancy_after_costs,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "total_costs": self.total_costs,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class DirectionMetrics:
    direction: str
    sample_count: int
    accepted_count: int
    precision: float | None
    observed_precision: float | None
    precision_ci: tuple[float, float] | None
    coverage: float
    abstention: float
    false_signal_rate: float | None
    precision_at_fixed_coverage: float | None
    coverage_at_fixed_error: float | None
    threshold: float
    expectancy_after_costs: float | None
    profit_factor: float | None
    max_drawdown: float | None
    calibration: CalibrationMetrics | None = None
    calibration_candidates: tuple[CalibrationMetrics, ...] = ()
    monthly_slices: tuple[dict[str, object], ...] = ()
    regime_slices: tuple[dict[str, object], ...] = ()
    threshold_sensitivity: tuple[dict[str, object], ...] = ()
    headline_suppressed: bool = False

    @property
    def error_rate(self) -> float | None:
        return self.false_signal_rate

    @property
    def precision_confidence_interval(self) -> tuple[float, float] | None:
        return self.precision_ci

    @property
    def abstention_rate(self) -> float:
        return self.abstention

    @property
    def risk_coverage(self) -> tuple[float, float | None]:
        return self.coverage, self.precision

    @property
    def raw_precision(self) -> float | None:
        """Observed precision, including when the governance headline is hidden."""
        return self.observed_precision

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "direction": self.direction,
            "sample_count": self.sample_count,
            "accepted_count": self.accepted_count,
            "precision": None if self.headline_suppressed else self.precision,
            "observed_precision": self.observed_precision,
            "precision_ci": self.precision_ci,
            "coverage": self.coverage,
            "abstention": self.abstention,
            "false_signal_rate": self.false_signal_rate,
            "precision_at_fixed_coverage": self.precision_at_fixed_coverage,
            "coverage_at_fixed_error": self.coverage_at_fixed_error,
            "threshold": self.threshold,
            "expectancy_after_costs": self.expectancy_after_costs,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "headline_suppressed": self.headline_suppressed,
            "monthly_slices": [dict(item) for item in self.monthly_slices],
            "regime_slices": [dict(item) for item in self.regime_slices],
            "threshold_sensitivity": [dict(item) for item in self.threshold_sensitivity],
        }
        result["calibration"] = self.calibration.to_dict() if self.calibration else None
        result["calibration_candidates"] = [item.to_dict() for item in self.calibration_candidates]
        return result


def calibration_metrics(
    labels: Sequence[object],
    probabilities: Sequence[float],
    *,
    direction: str = "BUY",
    method: str = "raw",
    bins: int = 10,
) -> CalibrationMetrics:
    y = _binary(labels, "UP" if direction == "BUY" else "DOWN")
    p = _probabilities(probabilities)
    if len(y) != len(p):
        raise ValueError("labels and probabilities must have the same length")
    if isinstance(bins, bool) or bins <= 0:
        raise ValueError("bins must be positive")
    clipped = np.clip(p, 1e-12, 1 - 1e-12)
    brier = float(np.mean((p - y) ** 2))
    logloss = float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped)))
    reliability: list[dict[str, float]] = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for i in range(bins):
        mask = (p >= edges[i]) & ((p < edges[i + 1]) if i < bins - 1 else (p <= edges[i + 1]))
        if mask.any():
            reliability.append({
                "lower": float(edges[i]),
                "upper": float(edges[i + 1]),
                "mean_probability": float(p[mask].mean()),
                "observed_frequency": float(y[mask].mean()),
                "sample_count": float(mask.sum()),
            })
    return CalibrationMetrics(direction, method, brier, logloss, tuple(reliability), len(y))


# Descriptive aliases keep the public API discoverable for callers that prefer
# verb-led names while preserving the concise interface used by the evaluator.
compute_calibration_metrics = calibration_metrics


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float] | None:
    if trials <= 0 or successes < 0 or successes > trials:
        return None
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z must be positive and finite")
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def economic_metrics(
    returns: Sequence[float],
    costs: Sequence[float] | None = None,
) -> EconomicMetrics:
    values = np.asarray(returns, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("returns must be a finite vector")
    if costs is None:
        cost_values = np.zeros(len(values), dtype=float)
    else:
        cost_values = np.asarray(costs, dtype=float)
        if cost_values.shape != values.shape or not np.isfinite(cost_values).all() or (cost_values < 0).any():
            raise ValueError("costs must be finite, non-negative, and aligned with returns")
    net = values - cost_values
    gains = float(net[net > 0].sum())
    losses = float(-net[net < 0].sum())
    factor = gains / losses if losses > 0 else (math.inf if gains > 0 else None)
    running = np.maximum.accumulate(np.r_[0.0, np.cumsum(net)])
    drawdown = float(np.max(running - np.r_[0.0, np.cumsum(net)])) if len(net) else 0.0
    return EconomicMetrics(float(net.mean()) if len(net) else None, factor, drawdown, gains, losses, float(cost_values.sum()), len(net))


compute_economic_metrics = economic_metrics


def direction_metrics(
    labels: Sequence[object],
    probabilities: Sequence[float],
    *,
    direction: str = "BUY",
    threshold: float = 0.5,
    fixed_coverage: float = 0.2,
    fixed_error: float = 0.1,
    returns: Sequence[float] | None = None,
    costs: Sequence[float] | None = None,
    governance_minimum: int = 30,
    calibration: CalibrationMetrics | None = None,
    calibration_candidates: Sequence[CalibrationMetrics] = (),
    monthly: Sequence[object] | None = None,
    regimes: Sequence[object] | None = None,
    sensitivity: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> DirectionMetrics:
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")
    p = _probabilities(probabilities)
    y = _binary(labels, "UP" if direction == "BUY" else "DOWN")
    if len(y) != len(p):
        raise ValueError("labels and probabilities must have the same length")
    if not 0 < threshold <= 1 or not 0 < fixed_coverage <= 1 or not 0 <= fixed_error < 1:
        raise ValueError("threshold and coverage/error settings are invalid")
    accepted = p >= threshold
    count = int(accepted.sum())
    successes = int(y[accepted].sum())
    precision = successes / count if count else None
    false_rate = 1 - precision if precision is not None else None
    top_count = max(1, math.ceil(len(p) * fixed_coverage))
    top = np.argsort(-p, kind="stable")[:top_count]
    fixed_precision = float(y[top].mean()) if len(top) else None
    candidates = sorted({float(item) for item in sensitivity if 0 < item <= 1})
    sensitivity_rows: list[dict[str, object]] = []
    for value in candidates:
        chosen = p >= value
        n = int(chosen.sum())
        precision_at = float(y[chosen].mean()) if n else None
        sensitivity_rows.append({"threshold": value, "coverage": n / len(p), "precision": precision_at, "accepted_count": n})
    threshold_for_error: float | None = None
    for value in sorted({float(item) for item in p}, reverse=True):
        chosen = p >= value
        if chosen.any() and 1 - float(y[chosen].mean()) <= fixed_error:
            threshold_for_error = value
    chosen_error = p >= threshold_for_error if threshold_for_error is not None else np.zeros(len(p), dtype=bool)
    economics = None
    if returns is not None:
        values = np.asarray(returns, dtype=float)
        if len(values) != len(p):
            raise ValueError("returns must align with labels")
        selected_costs = np.asarray(costs, dtype=float) if costs is not None else None
        economics = economic_metrics(values[accepted].tolist(), selected_costs[accepted].tolist() if selected_costs is not None else None)
    # Governance sample size is the number of emitted directional signals, not
    # the number of opportunities on which the model abstained.
    headline_suppressed = count < governance_minimum
    return DirectionMetrics(
        direction=direction,
        sample_count=len(p),
        accepted_count=count,
        precision=None if headline_suppressed else precision,
        observed_precision=precision,
        precision_ci=wilson_interval(successes, count),
        coverage=count / len(p),
        abstention=1 - count / len(p),
        false_signal_rate=false_rate,
        precision_at_fixed_coverage=fixed_precision,
        coverage_at_fixed_error=float(chosen_error.mean()),
        threshold=threshold,
        expectancy_after_costs=economics.expectancy_after_costs if economics else None,
        profit_factor=economics.profit_factor if economics else None,
        max_drawdown=economics.max_drawdown if economics else None,
        calibration=calibration,
        calibration_candidates=tuple(calibration_candidates),
        monthly_slices=_slice_metrics(y, p, accepted, monthly),
        regime_slices=_slice_metrics(y, p, accepted, regimes),
        threshold_sensitivity=tuple(sensitivity_rows),
        headline_suppressed=headline_suppressed,
    )


compute_direction_metrics = direction_metrics
evaluate_direction = direction_metrics


def _slice_metrics(y: np.ndarray, p: np.ndarray, accepted: np.ndarray, values: Sequence[object] | None) -> tuple[dict[str, object], ...]:
    if values is None or len(values) != len(y):
        return ()
    result: list[dict[str, object]] = []
    for label in dict.fromkeys(values):
        mask = np.asarray([value == label for value in values]) & accepted
        n = int(mask.sum())
        result.append({"slice": str(label), "sample_count": n, "precision": float(y[mask].mean()) if n else None, "coverage": float(mask.mean())})
    return tuple(result)
