"""Fail-closed selective decision scaffolding for research signals."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from nifty_signal_engine.domain.signal import ResearchSignal, SignalAction
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

    def evaluate(
        self,
        prediction: DirectionProbabilities,
        quality: DataQualityReport,
        economics: EconomicsAssessment | None = None,
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

        if not self.validated:
            reasons.append("MODEL_NOT_PROMOTED")

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
        if directional[2] >= max(directional[0], directional[1]):
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
        )


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
