"""Transparent, research-only directional baselines."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal, Self

import pandas as pd  # type: ignore[import-untyped]
from pydantic import BaseModel, model_validator
from sklearn.impute import SimpleImputer  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]

from nifty_signal_engine.features.pipeline import FeatureRow
from nifty_signal_engine.signals.labels import DirectionLabel

Instrument = Literal["NIFTY", "BANKNIFTY"]
_INSTRUMENTS = frozenset({"NIFTY", "BANKNIFTY"})
_CLASSES = (DirectionLabel.DOWN.value, DirectionLabel.NO_MOVE.value, DirectionLabel.UP.value)


class DirectionProbabilities(BaseModel, frozen=True):
    """Raw class probabilities plus deliberately nullable calibration outputs."""

    up: float
    down: float
    no_move: float
    instrument: Instrument | None = None
    calibrated_up: float | None = None
    calibrated_down: float | None = None
    calibrated_no_move: float | None = None

    @model_validator(mode="after")
    def validate_distributions(self) -> Self:
        _validate_probability_triplet((self.up, self.down, self.no_move), "raw")
        calibrated = (
            self.calibrated_up,
            self.calibrated_down,
            self.calibrated_no_move,
        )
        if any(value is not None for value in calibrated):
            if any(value is None for value in calibrated):
                raise ValueError("calibrated probabilities must be complete")
            _validate_probability_triplet(
                tuple(float(value) for value in calibrated if value is not None),
                "calibrated",
            )
        return self


class RuleBaseline:
    """Confluence rule over a completed price/VWAP, regime, and flow row."""

    _FEATURES = (
        "spot_return_1m",
        "vwap_distance",
        "vwap_slope",
        "regime_direction",
        "flow_dex_rupees",
    )

    def predict(self, row: FeatureRow) -> DirectionProbabilities:
        if not isinstance(row, FeatureRow):
            raise TypeError("row must be a FeatureRow")
        if row.instrument not in _INSTRUMENTS:
            raise ValueError("unsupported instrument")
        if not row.completed or row.row_kind != "COMPLETED_MINUTE":
            raise ValueError("rule baseline requires a completed bar")
        if row.quality_codes:
            raise ValueError("rule baseline requires a quality-clean row")
        if row.values.get("vwap_status") != "VALID":
            raise ValueError("vwap features are not valid")
        if row.values.get("regime_status") != "VALID":
            raise ValueError("regime is not valid")

        signs = tuple(_feature_sign(row.values.get(name), name) for name in self._FEATURES)
        if all(sign > 0 for sign in signs):
            values = (0.8, 0.1, 0.1)
        elif all(sign < 0 for sign in signs):
            values = (0.1, 0.8, 0.1)
        else:
            values = (0.2, 0.2, 0.6)
        return DirectionProbabilities(
            up=values[0],
            down=values[1],
            no_move=values[2],
            instrument=row.instrument,  # type: ignore[arg-type]
        )


class LogisticBaseline:
    """One-instrument multinomial model with train-fitted median imputation."""

    def __init__(self, *, instrument: Instrument) -> None:
        if instrument not in _INSTRUMENTS:
            raise ValueError("unsupported instrument")
        self.instrument = instrument
        self._features: tuple[str, ...] = ()
        self._pipeline: Pipeline | None = None
        self._training_medians: dict[str, float] = {}

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._features

    @property
    def training_medians(self) -> dict[str, float]:
        return dict(self._training_medians)

    @property
    def coefficients(self) -> dict[str, tuple[float, ...]]:
        classifier = self._fitted_classifier()
        return {
            str(label): tuple(float(value) for value in weights)
            for label, weights in zip(
                classifier.classes_, classifier.coef_, strict=True
            )
        }

    @property
    def intercepts(self) -> dict[str, float]:
        classifier = self._fitted_classifier()
        return dict(
            zip(
                (str(label) for label in classifier.classes_),
                (float(value) for value in classifier.intercept_),
                strict=True,
            )
        )

    def fit(self, frame: pd.DataFrame, labels: Sequence[str | DirectionLabel]) -> Self:
        features = self._training_features(frame)
        normalized_labels = tuple(
            value.value if isinstance(value, DirectionLabel) else value for value in labels
        )
        if len(features) != len(normalized_labels):
            raise ValueError("frame and labels must have the same length")
        if set(normalized_labels) != set(_CLASSES):
            raise ValueError("training requires exactly the three classes")
        if any(value not in _CLASSES for value in normalized_labels):
            raise ValueError("unsupported training label")

        numeric = _numeric_frame(features)
        if numeric.isna().all(axis=0).any():
            raise ValueError("features cannot be entirely missing")
        if _has_infinity(numeric):
            raise ValueError("features cannot contain infinite values")
        pipeline = Pipeline(
            steps=(
                ("median", SimpleImputer(strategy="median")),
                (
                    "logistic",
                    LogisticRegression(solver="lbfgs", max_iter=1_000, random_state=0),
                ),
            )
        )
        pipeline.fit(numeric, normalized_labels)
        imputer = pipeline.named_steps["median"]
        self._features = tuple(numeric.columns)
        self._training_medians = dict(
            zip(self._features, (float(value) for value in imputer.statistics_), strict=True)
        )
        self._pipeline = pipeline
        return self

    def predict_proba(
        self, row: FeatureRow | Mapping[str, object] | pd.Series
    ) -> DirectionProbabilities:
        if self._pipeline is None:
            raise RuntimeError("logistic baseline must be fitted before prediction")
        values, instrument = _prediction_values(row)
        if instrument is not None and instrument != self.instrument:
            raise ValueError("prediction instrument does not match fitted instrument")
        missing = [name for name in self._features if name not in values]
        if missing:
            raise ValueError(f"prediction is missing features: {', '.join(missing)}")
        frame = _numeric_frame(pd.DataFrame([{name: values[name] for name in self._features}]))
        if _has_infinity(frame):
            raise ValueError("prediction features cannot contain infinite values")
        result = self._pipeline.predict_proba(frame)[0]
        classifier = self._pipeline.named_steps["logistic"]
        by_class = dict(zip(classifier.classes_, result, strict=True))
        return DirectionProbabilities(
            up=float(by_class[DirectionLabel.UP.value]),
            down=float(by_class[DirectionLabel.DOWN.value]),
            no_move=float(by_class[DirectionLabel.NO_MOVE.value]),
            instrument=self.instrument,
        )

    def _training_features(self, frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("frame must be a pandas DataFrame")
        if frame.empty:
            raise ValueError("training frame must not be empty")
        if not frame.columns.is_unique or any(not isinstance(name, str) for name in frame.columns):
            raise ValueError("feature names must be unique strings")
        features = frame.copy()
        if "instrument" in features:
            instruments = set(features.pop("instrument"))
            if instruments != {self.instrument}:
                raise ValueError("training data must contain a single configured instrument")
        if features.empty:
            raise ValueError("training frame requires numeric features")
        return features

    def _fitted_classifier(self) -> LogisticRegression:
        if self._pipeline is None:
            raise RuntimeError("logistic baseline must be fitted first")
        return self._pipeline.named_steps["logistic"]  # type: ignore[no-any-return]


def _validate_probability_triplet(values: tuple[float, ...], name: str) -> None:
    if len(values) != 3 or any(
        isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1
        for value in values
    ):
        raise ValueError(f"{name} probabilities must be finite values in [0, 1]")
    if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"{name} probabilities must sum to one")


def _feature_sign(value: object, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} must be available")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return 1 if numeric > 0 else -1 if numeric < 0 else 0


def _numeric_frame(frame: pd.DataFrame) -> pd.DataFrame:
    try:
        return frame.apply(pd.to_numeric, errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError("features must be numeric or missing") from exc


def _has_infinity(frame: pd.DataFrame) -> bool:
    return any(math.isinf(float(value)) for value in frame.to_numpy().flat if not pd.isna(value))


def _prediction_values(
    row: FeatureRow | Mapping[str, object] | pd.Series,
) -> tuple[Mapping[str, object], object]:
    if isinstance(row, FeatureRow):
        if not row.completed or row.quality_codes:
            raise ValueError("prediction requires a quality-clean completed row")
        return row.values, row.instrument
    if isinstance(row, pd.Series):
        values = row.to_dict()
    elif isinstance(row, Mapping):
        values = row
    else:
        raise TypeError("row must be a FeatureRow, mapping, or pandas Series")
    return values, values.get("instrument")
