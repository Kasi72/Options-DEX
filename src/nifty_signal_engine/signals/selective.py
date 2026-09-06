"""Fail-closed selective decision scaffolding for research signals."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, cast

from nifty_signal_engine.config.market_hours import IST
from nifty_signal_engine.domain.signal import ResearchSignal, SignalAction
from nifty_signal_engine.features.pipeline import FeatureRow
from nifty_signal_engine.monitoring.data_quality import DataQualityReport
from nifty_signal_engine.signals.baselines import DirectionProbabilities
from nifty_signal_engine.signals.service import allowed_actions

Direction = Literal["BUY", "SELL"]
_INSTRUMENTS = frozenset({"NIFTY", "BANKNIFTY"})


@dataclass(frozen=True, slots=True)
class EconomicsAssessment:
    """A research economics gate; it has no order-submission behavior."""

    accepted: bool
    action: SignalAction = SignalAction.NO_TRADE
    expected_value_after_costs: float | None = None

    def __post_init__(self) -> None:
        value = self.expected_value_after_costs
        if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
            raise ValueError("expected value must be finite")
        if self.accepted and (
            self.action is SignalAction.NO_TRADE or value is None or value <= 0
        ):
            raise ValueError("accepted economics requires a positive-value action")

    def for_direction(self, direction: Direction) -> EconomicsAssessment:
        if self.action not in allowed_actions(direction):
            raise ValueError(f"{self.action.value} is not allowed for {direction}")
        return self


def _empty_thresholds() -> dict[str, float | None]:
    return {"NIFTY": None, "BANKNIFTY": None}


@dataclass(frozen=True, slots=True)
class SelectiveDecision:
    """Apply every production gate without allowing one gate to rescue another."""

    validated: bool = False
    buy_thresholds: Mapping[str, float | None] = field(default_factory=_empty_thresholds)
    sell_thresholds: Mapping[str, float | None] = field(default_factory=_empty_thresholds)
    maximum_model_disagreement: float | None = None
    model_disagreement: float | None = None
    meta_label_threshold: float | None = None
    meta_label_probability: float | None = None
    conformal_accepted: bool | None = None
    sequential_evidence_accepted: bool | None = None
    model_version: str | None = None
    feature_schema_version: str | None = None
    horizon_minutes: int | None = None
    training_window_id: str | None = None
    calibration_id: str | None = None
    calibration_artifact_validated: bool = False
    validation_report_id: str | None = None
    validation_completed_at: datetime | None = None
    validation_artifact_promoted: bool = False

    def __post_init__(self) -> None:
        buy = _validated_thresholds(self.buy_thresholds, "BUY")
        sell = _validated_thresholds(self.sell_thresholds, "SELL")
        object.__setattr__(self, "buy_thresholds", MappingProxyType(buy))
        object.__setattr__(self, "sell_thresholds", MappingProxyType(sell))
        for name in (
            "maximum_model_disagreement",
            "model_disagreement",
            "meta_label_threshold",
            "meta_label_probability",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a probability in [0, 1]")
        for name in (
            "model_version",
            "feature_schema_version",
            "training_window_id",
            "calibration_id",
            "validation_report_id",
        ):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.horizon_minutes is not None and (
            isinstance(self.horizon_minutes, bool) or self.horizon_minutes <= 0
        ):
            raise ValueError("horizon_minutes must be positive")
        for name in (
            "validated",
            "calibration_artifact_validated",
            "validation_artifact_promoted",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if self.validation_completed_at is not None and (
            self.validation_completed_at.tzinfo is None
            or self.validation_completed_at.utcoffset() is None
        ):
            raise ValueError("validation_completed_at must be timezone-aware")
        if self.validation_completed_at is not None:
            object.__setattr__(
                self,
                "validation_completed_at",
                self.validation_completed_at.astimezone(IST),
            )

    def evaluate(
        self,
        prediction: DirectionProbabilities,
        quality: DataQualityReport,
        economics: EconomicsAssessment | None = None,
        *,
        row: FeatureRow | None = None,
    ) -> ResearchSignal:
        if not isinstance(prediction, DirectionProbabilities):
            raise TypeError("prediction must be DirectionProbabilities")
        reasons: list[str] = []
        instrument = prediction.instrument
        quality_valid = isinstance(quality, DataQualityReport)
        if not quality_valid:
            reasons.append("QUALITY_MISSING")
        else:
            if quality.checked_at.tzinfo is None or quality.checked_at.utcoffset() is None:
                reasons.append("QUALITY_TIMESTAMP_INVALID")
            if not quality.tradable or quality.codes:
                reasons.append("DATA_QUALITY_FAILED")
                reasons.extend(code.value for code in quality.codes)
        if instrument not in _INSTRUMENTS:
            reasons.append("PREDICTION_INSTRUMENT_MISSING")

        reasons.extend(self._provenance_reasons(prediction, quality, row))

        if not self.validated or not self.validation_artifact_promoted:
            reasons.append("MODEL_NOT_PROMOTED")
        if not self.calibration_artifact_validated:
            reasons.append("CALIBRATION_NOT_VALIDATED")
        if not self.validation_artifact_promoted:
            reasons.append("VALIDATION_NOT_PROMOTED")

        calibrated = (
            prediction.calibrated_up,
            prediction.calibrated_down,
            prediction.calibrated_no_move,
        )
        if any(value is None for value in calibrated):
            reasons.append("CALIBRATION_UNAVAILABLE")
            directional = (prediction.up, prediction.down, prediction.no_move)
        else:
            directional = cast(tuple[float, float, float], calibrated)
        direction: Direction | None
        if directional[2] >= max(directional[0], directional[1]) or math.isclose(
            directional[0], directional[1], rel_tol=0.0, abs_tol=1e-12
        ):
            direction = None
            reasons.append("NO_DIRECTIONAL_EDGE")
        elif directional[0] > directional[1]:
            direction = "BUY"
        else:
            direction = "SELL"

        buy_threshold = self.buy_thresholds.get(instrument) if instrument else None
        sell_threshold = self.sell_thresholds.get(instrument) if instrument else None
        if direction is not None:
            threshold = buy_threshold if direction == "BUY" else sell_threshold
            probability = directional[0] if direction == "BUY" else directional[1]
            if threshold is None:
                reasons.append(f"{direction}_THRESHOLD_UNAVAILABLE")
            elif probability < threshold:
                reasons.append(f"{direction}_THRESHOLD_NOT_MET")

        if self.maximum_model_disagreement is None or self.model_disagreement is None:
            reasons.append("MODEL_DISAGREEMENT_UNAVAILABLE")
        elif self.model_disagreement > self.maximum_model_disagreement:
            reasons.append("MODEL_DISAGREEMENT_EXCESSIVE")
        if self.meta_label_threshold is None or self.meta_label_probability is None:
            reasons.append("META_LABEL_UNAVAILABLE")
        elif self.meta_label_probability < self.meta_label_threshold:
            reasons.append("META_LABEL_REJECTED")
        if self.conformal_accepted is None:
            reasons.append("CONFORMAL_UNAVAILABLE")
        elif not self.conformal_accepted:
            reasons.append("CONFORMAL_REJECTED")
        if self.sequential_evidence_accepted is None:
            reasons.append("SEQUENTIAL_EVIDENCE_UNAVAILABLE")
        elif not self.sequential_evidence_accepted:
            reasons.append("SEQUENTIAL_EVIDENCE_REJECTED")

        selected_action = SignalAction.NO_TRADE
        if economics is None:
            reasons.append("ECONOMICS_UNAVAILABLE")
        elif not isinstance(economics, EconomicsAssessment):
            raise TypeError("economics must be EconomicsAssessment or None")
        elif not economics.accepted:
            reasons.append("ECONOMICS_REJECTED")
        elif direction is None:
            reasons.append("ECONOMICS_DIRECTION_UNAVAILABLE")
        else:
            try:
                economics.for_direction(direction)
            except ValueError:
                reasons.append("ECONOMICS_ACTION_UNSAFE")
            else:
                selected_action = economics.action

        if reasons:
            selected_action = SignalAction.NO_TRADE
        return ResearchSignal(
            action=selected_action,
            instrument=instrument,
            direction=direction,
            reasons=tuple(dict.fromkeys(reasons)),
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
            model_disagreement=self.model_disagreement,
            meta_label_probability=self.meta_label_probability,
            conformal_accepted=self.conformal_accepted,
            sequential_evidence_accepted=self.sequential_evidence_accepted,
            economics_accepted=economics.accepted if economics is not None else None,
            expected_value_after_costs=(
                economics.expected_value_after_costs if economics is not None else None
            ),
            feature_available_at=prediction.feature_available_at,
            model_version=prediction.model_version,
            feature_schema_version=prediction.feature_schema_version,
            horizon_minutes=prediction.horizon_minutes,
            training_window_start=prediction.training_window_start,
            training_window_end=prediction.training_window_end,
            training_window_id=prediction.training_window_id,
            calibration_id=prediction.calibration_id,
            calibration_completed_at=prediction.calibration_completed_at,
            calibration_artifact_validated=self.calibration_artifact_validated,
            validation_report_id=prediction.validation_report_id,
            validation_completed_at=self.validation_completed_at,
            validation_artifact_promoted=self.validation_artifact_promoted,
        )

    def _provenance_reasons(
        self,
        prediction: DirectionProbabilities,
        quality: DataQualityReport,
        row: FeatureRow | None,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        required = (
            (prediction.feature_available_at, "FEATURE_TIME_MISSING"),
            (prediction.model_version, "MODEL_VERSION_MISSING"),
            (prediction.feature_schema_version, "FEATURE_SCHEMA_VERSION_MISSING"),
            (prediction.horizon_minutes, "HORIZON_MISSING"),
            (prediction.training_window_id, "TRAINING_WINDOW_ID_MISSING"),
            (prediction.calibration_id, "CALIBRATION_ID_MISSING"),
            (prediction.calibration_completed_at, "CALIBRATION_TIME_MISSING"),
            (prediction.validation_report_id, "VALIDATION_REPORT_ID_MISSING"),
        )
        reasons.extend(reason for value, reason in required if value is None)
        if (
            prediction.training_window_start is None
            or prediction.training_window_end is None
        ):
            reasons.append("TRAINING_WINDOW_MISSING")
        if row is None:
            reasons.append("FEATURE_ROW_MISSING")
        else:
            if not row.completed or row.quality_codes:
                reasons.append("FEATURE_ROW_INVALID")
            if prediction.instrument != row.instrument:
                reasons.append("INSTRUMENT_MISMATCH")
            if (
                prediction.feature_available_at is not None
                and prediction.feature_available_at != row.available_at
            ):
                reasons.append("FEATURE_TIME_MISMATCH")
            if (
                prediction.feature_schema_version is not None
                and prediction.feature_schema_version != row.schema_version
            ):
                reasons.append("FEATURE_SCHEMA_MISMATCH")
        if isinstance(quality, DataQualityReport):
            quality_instrument = quality.details.get("instrument")
            if quality_instrument is None:
                reasons.append("QUALITY_INSTRUMENT_MISSING")
            elif quality_instrument != prediction.instrument:
                reasons.append("INSTRUMENT_MISMATCH")
            if (
                prediction.feature_available_at is not None
                and quality.checked_at.tzinfo is not None
                and quality.checked_at.utcoffset() is not None
                and quality.checked_at < prediction.feature_available_at
            ):
                reasons.append("QUALITY_PREDATES_FEATURES")
        if self.validation_report_id is None:
            reasons.append("VALIDATION_REPORT_ID_MISSING")
        elif (
            prediction.validation_report_id is not None
            and self.validation_report_id != prediction.validation_report_id
        ):
            reasons.append("VALIDATION_REPORT_MISMATCH")
        expected_identities = (
            (self.model_version, prediction.model_version, "MODEL_VERSION_MISSING", "MODEL_VERSION_MISMATCH"),
            (
                self.feature_schema_version,
                prediction.feature_schema_version,
                "FEATURE_SCHEMA_VERSION_MISSING",
                "FEATURE_SCHEMA_MISMATCH",
            ),
            (self.horizon_minutes, prediction.horizon_minutes, "HORIZON_MISSING", "HORIZON_MISMATCH"),
            (
                self.training_window_id,
                prediction.training_window_id,
                "TRAINING_WINDOW_ID_MISSING",
                "TRAINING_WINDOW_ID_MISMATCH",
            ),
            (
                self.calibration_id,
                prediction.calibration_id,
                "CALIBRATION_ID_MISSING",
                "CALIBRATION_ID_MISMATCH",
            ),
        )
        for expected, actual, missing_reason, mismatch_reason in expected_identities:
            if expected is None:
                reasons.append(missing_reason)
            elif actual is not None and expected != actual:
                reasons.append(mismatch_reason)
        if self.validation_completed_at is None:
            reasons.append("VALIDATION_TIME_MISSING")
        elif (
            prediction.feature_available_at is not None
            and self.validation_completed_at > prediction.feature_available_at
        ):
            reasons.append("VALIDATION_AFTER_PREDICTION")
        return tuple(dict.fromkeys(reasons))


def _validated_thresholds(
    values: Mapping[str, float | None], direction: str
) -> dict[str, float | None]:
    if not isinstance(values, Mapping) or set(values) != _INSTRUMENTS:
        raise ValueError(f"{direction} thresholds must be separate for both instruments")
    result = dict(values)
    for value in result.values():
        if value is not None and (
            isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1
        ):
            raise ValueError(f"{direction} thresholds must be in (0, 1]")
    return result
