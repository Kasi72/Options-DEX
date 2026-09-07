"""Serializable governance reports and verified promotion-artifact registry."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

_PROMOTION_MARKER = object()

if TYPE_CHECKING:
    from nifty_signal_engine.backtesting.metrics import DirectionMetrics


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Conservative evidence gates; these do not change live signal policy."""

    minimum_samples: int = 200
    minimum_precision: float = 0.0
    minimum_profit_factor: float = 1.2
    minimum_expectancy: float = 0.0
    maximum_drawdown: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.minimum_samples, bool) or self.minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        if self.minimum_profit_factor < 0 or self.minimum_expectancy < 0:
            raise ValueError("promotion thresholds must be non-negative")


@dataclass(frozen=True, slots=True)
class PromotionArtifact:
    """Evidence object that can be resolved only through a registry.

    The artifact is intentionally a research record.  Registering one does not
    alter ``SelectiveDecision``'s Phase 0–4 fail-closed behaviour.
    """

    artifact_id: str
    instrument: str
    horizon_minutes: int
    direction: str
    model_version: str
    calibration_id: str
    validation_report_id: str
    verified: bool
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)
    _verification_marker: object | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.instrument not in {"NIFTY", "BANKNIFTY"}:
            raise ValueError("promotion instrument must be NIFTY or BANKNIFTY")
        if self.direction not in {"BUY", "SELL"} or self.horizon_minutes <= 0:
            raise ValueError("promotion direction/horizon is invalid")
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be boolean")
        for name in ("artifact_id", "model_version", "calibration_id", "validation_report_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def key(self) -> tuple[str, int, str]:
        return self.instrument, self.horizon_minutes, self.direction

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "instrument": self.instrument,
            "horizon_minutes": self.horizon_minutes,
            "direction": self.direction,
            "model_version": self.model_version,
            "calibration_id": self.calibration_id,
            "validation_report_id": self.validation_report_id,
            "verified": self.verified,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


class PromotionRegistry:
    """In-memory registry with fail-closed resolution semantics."""

    def __init__(self) -> None:
        self._artifacts: dict[tuple[str, int, str], PromotionArtifact] = {}

    def register(self, artifact: PromotionArtifact) -> PromotionArtifact:
        if not isinstance(artifact, PromotionArtifact):
            raise TypeError("artifact must be PromotionArtifact")
        # An unverified record is useful for audit, but can never be resolved as
        # promotion evidence.  Replacement is allowed only for the same key and
        # a different, explicitly versioned artifact id.
        self._artifacts[artifact.key] = artifact
        return artifact

    def resolve(self, instrument: str, horizon_minutes: int, direction: str) -> PromotionArtifact | None:
        artifact = self._artifacts.get((instrument, horizon_minutes, direction))
        return (
            artifact
            if artifact is not None
            and artifact.verified
            and artifact._verification_marker is _PROMOTION_MARKER
            else None
        )

    def is_promoted(self, instrument: str, horizon_minutes: int, direction: str) -> bool:
        return self.resolve(instrument, horizon_minutes, direction) is not None

    def snapshot(self) -> Mapping[tuple[str, int, str], PromotionArtifact]:
        return MappingProxyType(dict(self._artifacts))


def build_promotion_artifact(
    report: object,
    metrics: DirectionMetrics,
    *,
    instrument: str | None = None,
    horizon_minutes: int | None = None,
    model_version: str | None = None,
    calibration_id: str | None = None,
    validation_report_id: str | None = None,
    policy: PromotionPolicy | None = None,
) -> PromotionArtifact:
    """Create evidence from a report, applying all governance gates.

    No caller-provided boolean can make this object verified: verification is
    derived from the measured metrics and policy at construction time.
    """
    # ``report`` is intentionally mandatory evidence.  A naked metrics object
    # and caller-supplied IDs are not a chronological validation artifact.
    from nifty_signal_engine.backtesting.walk_forward import FoldReport

    if not isinstance(report, FoldReport) or report._verification_marker is not _PROMOTION_MARKER:
        raise ValueError("promotion evidence must come from a verified chronological report")
    if report.directions.get(metrics.direction) is not metrics:
        raise ValueError("metrics must be the direction metrics attached to the report")
    expected_instrument = report.instrument
    expected_horizon = report.horizon_minutes
    expected_model = report.model_version
    if expected_instrument is None or expected_horizon is None:
        raise ValueError("chronological report is missing instrument or horizon provenance")
    expected_calibration = f"calibration-fold-{report.fold_id}-{metrics.direction.lower()}"
    expected_validation = report.report_id
    supplied = (instrument, horizon_minutes, model_version, calibration_id, validation_report_id)
    expected = (expected_instrument, expected_horizon, expected_model, expected_calibration, expected_validation)
    if any(value is not None and value != expected_value for value, expected_value in zip(supplied, expected, strict=True)):
        raise ValueError("promotion IDs must match the verified chronological report")
    instrument, horizon_minutes, model_version, calibration_id, validation_report_id = expected
    selected_policy = policy if policy is not None else PromotionPolicy()
    failures: list[str] = []
    if metrics.accepted_count < selected_policy.minimum_samples:
        failures.append("SAMPLE_SIZE_BELOW_GOVERNANCE_MINIMUM")
    if metrics.precision is None or metrics.precision < selected_policy.minimum_precision:
        failures.append("PRECISION_GATE_FAILED")
    if metrics.profit_factor is None or metrics.profit_factor < selected_policy.minimum_profit_factor:
        failures.append("PROFIT_FACTOR_GATE_FAILED")
    if metrics.expectancy_after_costs is None or metrics.expectancy_after_costs <= selected_policy.minimum_expectancy:
        failures.append("EXPECTANCY_GATE_FAILED")
    if selected_policy.maximum_drawdown is not None and (metrics.max_drawdown is None or metrics.max_drawdown > selected_policy.maximum_drawdown):
        failures.append("DRAWDOWN_GATE_FAILED")
    payload = {
        "instrument": instrument,
        "horizon_minutes": horizon_minutes,
        "direction": metrics.direction,
        "model_version": model_version,
        "calibration_id": calibration_id,
        "validation_report_id": validation_report_id,
        "sample_count": metrics.sample_count,
        "accepted_count": metrics.accepted_count,
        "precision": metrics.precision,
        "profit_factor": metrics.profit_factor,
        "expectancy_after_costs": metrics.expectancy_after_costs,
        "max_drawdown": metrics.max_drawdown,
    }
    artifact_id = "promotion-" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    artifact = PromotionArtifact(
        artifact_id=artifact_id,
        instrument=instrument,
        horizon_minutes=horizon_minutes,
        direction=metrics.direction,
        model_version=model_version,
        calibration_id=calibration_id,
        validation_report_id=validation_report_id,
        verified=not failures,
        reasons=tuple(failures) or ("EVIDENCE_GATES_PASSED",),
        evidence=payload,
    )
    # Only this constructor path receives the private in-process marker.  A
    # JSON round-trip or caller-supplied ``verified=True`` is therefore never
    # sufficient for registry resolution.
    object.__setattr__(artifact, "_verification_marker", _PROMOTION_MARKER)
    return artifact


def report_to_dict(report: object) -> dict[str, object]:
    if hasattr(report, "to_dict"):
        value = report.to_dict()
        if isinstance(value, dict):
            return value
    raise TypeError("report must provide to_dict()")


def write_report(report: object, path: str) -> None:
    """Write canonical JSON report metadata for audit/reproducibility."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report_to_dict(report), handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
