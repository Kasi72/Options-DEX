"""Model evaluation on purged folds with calibration fit on calibration data only."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, cast

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
from sklearn.isotonic import IsotonicRegression  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

from nifty_signal_engine.backtesting.metrics import (
    CalibrationMetrics,
    DirectionMetrics,
    calibration_metrics,
    direction_metrics,
)
from nifty_signal_engine.backtesting.reporting import (
    _PROMOTION_MARKER,
    PromotionArtifact,
    PromotionPolicy,
    build_promotion_artifact,
)
from nifty_signal_engine.backtesting.splits import Fold
from nifty_signal_engine.config.market_hours import IST


class _Calibrator:
    def __init__(self, method: str, values: Sequence[float], labels: Sequence[float]) -> None:
        self.method = method
        self._temperature = 1.0
        self._sigmoid: LogisticRegression | None = None
        self._isotonic: IsotonicRegression | None = None
        p = np.clip(np.asarray(values, dtype=float), 1e-6, 1 - 1e-6)
        y = np.asarray(labels, dtype=float)
        if method == "sigmoid" and len(np.unique(y)) > 1:
            self._sigmoid = LogisticRegression(solver="lbfgs", random_state=0).fit(
                np.log(p / (1 - p)).reshape(-1, 1), y
            )
        elif method == "isotonic" and len(np.unique(y)) > 1:
            self._isotonic = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(p, y)
        elif method == "temperature" and len(np.unique(y)) > 1:
            logits = np.log(p / (1 - p))
            best_loss = math.inf
            for temperature in np.linspace(0.25, 4.0, 76):
                calibrated = _sigmoid(logits / temperature)
                loss = float(-np.mean(y * np.log(np.clip(calibrated, 1e-12, 1)) + (1 - y) * np.log(np.clip(1 - calibrated, 1e-12, 1))))
                if loss < best_loss:
                    best_loss, self._temperature = loss, float(temperature)
        elif method not in {"sigmoid", "isotonic", "temperature", "raw"}:
            raise ValueError(f"unsupported calibration method: {method}")

    def predict(self, values: Sequence[float]) -> np.ndarray:
        p = np.clip(np.asarray(values, dtype=float), 1e-6, 1 - 1e-6)
        if self._sigmoid is not None:
            return self._sigmoid.predict_proba(np.log(p / (1 - p)).reshape(-1, 1))[:, 1]
        if self._isotonic is not None:
            return np.asarray(self._isotonic.predict(p), dtype=float)
        if self.method == "temperature":
            return _sigmoid(np.log(p / (1 - p)) / self._temperature)
        return p


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40, 40)))


@dataclass(frozen=True, slots=True)
class FoldReport:
    fold_id: int
    directions: Mapping[str, DirectionMetrics]
    selected_calibrators: Mapping[str, str]
    promotion_artifacts: Mapping[str, PromotionArtifact] = field(default_factory=dict)
    model_version: str = "unknown"
    instrument: str | None = None
    horizon_minutes: int | None = None
    created_at: datetime | None = None
    _verification_marker: object | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", MappingProxyType(dict(self.directions)))
        object.__setattr__(self, "selected_calibrators", MappingProxyType(dict(self.selected_calibrators)))
        object.__setattr__(self, "promotion_artifacts", MappingProxyType(dict(self.promotion_artifacts)))

    @property
    def buy(self) -> DirectionMetrics | None:
        return self.directions.get("BUY")

    @property
    def sell(self) -> DirectionMetrics | None:
        return self.directions.get("SELL")

    @property
    def report_id(self) -> str:
        return f"walk-forward-fold-{self.fold_id}"

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "report_id": self.report_id,
            "model_version": self.model_version,
            "instrument": self.instrument,
            "horizon_minutes": self.horizon_minutes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "selected_calibrators": dict(self.selected_calibrators),
            "directions": {key: value.to_dict() for key, value in self.directions.items()},
            "promotion_artifacts": {key: value.to_dict() for key, value in self.promotion_artifacts.items()},
        }


def evaluate_fold(
    fold: Fold,
    model_factory: Callable[..., object],
    *,
    threshold: float = 0.5,
    governance_minimum: int = 30,
    policy: PromotionPolicy | None = None,
) -> FoldReport:
    """Fit a model on train, calibrate on calibration, and report untouched test.

    Model factories may accept ``(features, labels)``, ``(train_frame)``, or no
    arguments and expose ``fit``.  The returned model must be fresh and
    unfitted; the evaluator always fits it on the train block.
    """
    if not isinstance(fold, Fold) or not callable(model_factory):
        raise TypeError("fold and model_factory are required")
    blocks = (fold.train, fold.validation, fold.calibration, fold.test)
    label_columns = [_label_column(block) for block in blocks]
    if any(column is None for column in label_columns) or len(set(label_columns)) != 1:
        raise ValueError("all fold blocks must use one matching label column")
    label_column = label_columns[0]
    assert label_column is not None
    labels_train = tuple(fold.train[label_column])
    labels_validation = tuple(fold.validation[label_column])
    labels_calibration = tuple(fold.calibration[label_column])
    labels_test = tuple(fold.test[label_column])
    instruments = [_instrument(block) for block in blocks]
    if any(value is None for value in instruments):
        raise ValueError("each fold block must contain one non-null instrument")
    if instruments and len(set(instruments)) != 1:
        raise ValueError("fold blocks must contain one matching instrument")
    selected_policy = policy if policy is not None else PromotionPolicy()
    train_features = _features(fold.train)
    model = _make_model(model_factory, train_features, labels_train)
    raw_validation = _predict(model, _features(fold.validation))
    raw_calibration = _predict(model, _features(fold.calibration))
    directions: dict[str, DirectionMetrics] = {}
    selected: dict[str, str] = {}
    selected_models: dict[str, _Calibrator] = {}
    candidate_reports: dict[str, tuple[CalibrationMetrics, ...]] = {}
    artifacts: dict[str, PromotionArtifact] = {}
    instrument = _instrument(fold.test)
    horizon_minutes = int(fold.horizon.total_seconds() // 60)
    for direction in ("BUY", "SELL"):
        key = "up" if direction == "BUY" else "down"
        calibration_y = _binary_labels(labels_calibration, direction)
        candidates: list[tuple[str, _Calibrator, CalibrationMetrics]] = []
        for method in ("sigmoid", "isotonic", "temperature"):
            calibrator = _Calibrator(method, raw_calibration[key].tolist(), calibration_y.tolist())
            calibration_prediction = calibrator.predict(raw_calibration[key].tolist())
            candidates.append((method, calibrator, calibration_metrics(labels_calibration, calibration_prediction.tolist(), direction=direction, method=method)))
        # Validation is the selection block.  No test value participates in this
        # choice; calibrators themselves were fitted exclusively on calibration.
        chosen_name, chosen, _ = min(
            candidates,
            key=lambda item: _selection_loss(labels_validation, item[1].predict(raw_validation[key].tolist()).tolist(), direction),
        )
        selected[direction] = chosen_name
        selected_models[direction] = chosen
        candidate_reports[direction] = tuple(item[2] for item in candidates)
    # Both directional calibrators are selected before the untouched test block
    # is read.  This prevents test predictions from influencing the other
    # direction's selection through shared model state.
    raw_test = _predict(model, _features(fold.test))
    for direction in ("BUY", "SELL"):
        key = "up" if direction == "BUY" else "down"
        chosen = selected_models[direction]
        calibrated_test = chosen.predict(raw_test[key].tolist())
        calibration_report = calibration_metrics(labels_test, calibrated_test.tolist(), direction=direction, method=selected[direction])
        direction_report = direction_metrics(
            labels_test,
            calibrated_test.tolist(),
            direction=direction,
            threshold=threshold,
            governance_minimum=governance_minimum,
            returns=_returns(fold.test, direction),
            # ``net_return``/``net_pnl`` already include transaction costs;
            # only gross outcome columns receive a separate cost vector.
            costs=None if any(name in fold.test for name in ("net_return", "net_pnl")) else _costs(fold.test),
            calibration=calibration_report,
            calibration_candidates=candidate_reports[direction],
            monthly=_column(fold.test, "month") or tuple(
                timestamp.strftime("%Y-%m") for timestamp in fold.test.index
            ),
            regimes=_column(fold.test, "regime"),
        )
        directions[direction] = direction_report
    report = FoldReport(
        fold_id=fold.fold_id,
        directions=directions,
        selected_calibrators=selected,
        promotion_artifacts={},
        model_version=str(getattr(model, "model_version", model.__class__.__name__)),
        instrument=instrument,
        horizon_minutes=horizon_minutes,
        created_at=datetime.now(IST),
    )
    if instrument is not None:
        object.__setattr__(report, "_verification_marker", _PROMOTION_MARKER)
        for direction, direction_report in directions.items():
            key = "up" if direction == "BUY" else "down"
            artifacts[direction] = build_promotion_artifact(
                report,
                direction_report,
                instrument=instrument,
                horizon_minutes=horizon_minutes,
                model_version=report.model_version,
                calibration_id=f"calibration-fold-{fold.fold_id}-{direction.lower()}",
                validation_report_id=report.report_id,
                policy=selected_policy,
            )
        object.__setattr__(report, "promotion_artifacts", MappingProxyType(dict(artifacts)))
    return report


def _make_model(factory: Callable[..., object], features: pd.DataFrame, labels: Sequence[object]) -> object:
    try:
        signature = inspect.signature(factory)
        positional = [p for p in signature.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        positional = []
    if len(positional) >= 2:
        result = factory(features, labels)
    elif len(positional) == 1:
        result = factory(features)
    else:
        result = factory()
    if result is None:
        raise ValueError("model_factory returned None")
    if not callable(getattr(result, "fit", None)):
        raise TypeError("model_factory must return a fresh model with callable fit")
    if _looks_fitted(result):
        raise ValueError("model_factory must return a fresh, unfitted model")
    if callable(getattr(result, "fit", None)):
        cast(Any, result).fit(features, labels)
    return result


def _features(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=[name for name in ("label", "target", "direction", "return", "realized_return", "net_return", "pnl", "net_pnl", "cost", "cost_rupees", "transaction_cost", "month", "regime") if name in frame], errors="ignore")


def _labels(frame: pd.DataFrame) -> tuple[object, ...]:
    for name in ("label", "target", "direction"):
        if name in frame:
            return tuple(frame[name])
    raise ValueError("frame must contain label, target, or direction")


def _label_column(frame: pd.DataFrame) -> str | None:
    columns = [name for name in ("label", "target", "direction") if name in frame]
    return columns[0] if len(columns) == 1 else None


def _binary_labels(labels: Sequence[object], direction: str) -> np.ndarray:
    allowed = {"UP", "DOWN", "NO_MOVE"}
    normalized = [str(value) for value in labels]
    if any(value not in allowed for value in normalized):
        raise ValueError("labels contain an unsupported outcome")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")
    positive = "UP" if direction == "BUY" else "DOWN"
    return np.asarray([1.0 if value == positive else 0.0 for value in normalized], dtype=float)


def _predict(model: object, frame: pd.DataFrame) -> dict[str, np.ndarray]:
    predictor = getattr(model, "predict_proba", None)
    if not callable(predictor):
        predictor = getattr(model, "predict", None)
    if not callable(predictor):
        raise TypeError("model must expose predict_proba or predict")
    try:
        output = predictor(frame)
    except (TypeError, ValueError):
        output = [predictor(row) for _, row in frame.iterrows()]
    rows = list(output) if not isinstance(output, pd.DataFrame) else output.to_dict(orient="records")
    result: dict[str, list[float]] = {"up": [], "down": []}
    classes = tuple(str(value) for value in getattr(model, "classes_", ()))
    for row in rows:
        if isinstance(row, Mapping):
            up = row.get("up", row.get("UP", row.get("prob_up")))
            down = row.get("down", row.get("DOWN", row.get("prob_down")))
            if up is None or down is None:
                raise ValueError("prediction mapping requires UP and DOWN probabilities")
        elif hasattr(row, "up") and hasattr(row, "down"):
            up, down = row.up, row.down
        else:
            values = np.asarray(row, dtype=float).reshape(-1)
            if len(values) == 1:
                up, down = float(values[0]), float(1 - values[0])
            elif classes:
                by_class = dict(zip(classes, values, strict=False))
                up, down = by_class.get("UP", 0.0), by_class.get("DOWN", 0.0)
            elif len(values) >= 3:
                up, down = float(values[-1]), float(values[0])
            else:
                down, up = float(values[0]), float(values[1])
        result["up"].append(float(up))
        result["down"].append(float(down))
    return {key: np.asarray(value, dtype=float) for key, value in result.items()}


def _selection_loss(labels: Sequence[object], probabilities: np.ndarray, direction: str) -> float:
    y = _binary_labels(labels, direction)
    p = np.clip(probabilities, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _column(frame: pd.DataFrame, name: str) -> Sequence[object] | None:
    return tuple(frame[name]) if name in frame else None


def _returns(frame: pd.DataFrame, direction: str) -> Sequence[float] | None:
    for name in ("net_return", "net_pnl", "return", "realized_return", "pnl"):
        if name in frame:
            sign = 1.0 if direction == "BUY" else -1.0
            return tuple(sign * float(value) for value in frame[name])
    return None


def _costs(frame: pd.DataFrame) -> Sequence[float] | None:
    for name in ("cost", "cost_rupees", "transaction_cost"):
        if name in frame:
            return tuple(float(value) for value in frame[name])
    return None


def _looks_fitted(model: object) -> bool:
    """Reject common fitted-model markers under the fresh-factory contract."""
    if getattr(model, "fitted_", False) is True or getattr(model, "is_fitted", False) is True:
        return True
    if getattr(model, "classes_", None) is not None:
        return True
    return getattr(model, "_pipeline", None) is not None


def _instrument(frame: pd.DataFrame) -> str | None:
    if "instrument" not in frame:
        return None
    values = frame["instrument"].dropna().unique()
    return str(values[0]) if len(values) == 1 else None
