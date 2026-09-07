"""Deterministic market-regime classification for selective signals.

The classifier is deliberately transparent: it uses only completed, quality-clean
feature rows and returns an explicit ``UNKNOWN`` state during warm-up or when the
inputs required for a regime decision are unavailable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from nifty_signal_engine.features.pipeline import FeatureRow


class MarketRegime(StrEnum):
    UNKNOWN = "UNKNOWN"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    """Auditable regime result and the evidence used to obtain it."""

    regime: MarketRegime
    confidence: float
    trend_score: float
    volatility_percentile: float | None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("confidence", "trend_score"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.volatility_percentile is not None and not 0 <= self.volatility_percentile <= 1:
            raise ValueError("volatility_percentile must be in [0, 1]")


def classify_regime(row: FeatureRow) -> RegimeAssessment:
    """Classify a completed feature row without using future information.

    Thresholds are intentionally conservative and are configuration-free. They
    are a regime filter, not a directional trading model; promotion still
    requires walk-forward validation and calibration.
    """

    if not isinstance(row, FeatureRow):
        raise TypeError("row must be a FeatureRow")
    if not row.completed or row.quality_codes:
        return _unknown("FEATURE_ROW_INVALID")
    values = row.values
    required = ("observed_session_return", "opening_range_breakout", "realized_volatility")
    if any(values.get(name) is None for name in required):
        return _unknown("REGIME_FEATURES_MISSING")
    ret = _number(values["observed_session_return"])
    breakout = _number(values["opening_range_breakout"])
    volatility = _number(values["realized_volatility"])
    if volatility < 0:
        return _unknown("REALIZED_VOLATILITY_INVALID")

    # A realized-volatility value is a scale, not a percentile.  Treat the
    # feature as high-vol only when it is materially large relative to a
    # one-minute return scale; callers can replace this with a trained percentile
    # transform without changing the contract.
    vol_score = min(1.0, volatility / 0.003)
    trend_score = min(1.0, abs(ret) / 0.01) * 0.7 + (0.3 if breakout != 0 else 0.0)
    trend_score = min(1.0, trend_score)
    if vol_score >= 0.85:
        return RegimeAssessment(MarketRegime.HIGH_VOLATILITY, vol_score, trend_score, vol_score, ("VOLATILITY_ELEVATED",))
    if trend_score < 0.35:
        return RegimeAssessment(MarketRegime.RANGE, 1.0 - trend_score, trend_score, vol_score, ("TREND_EVIDENCE_WEAK",))
    if ret > 0 and breakout >= 0:
        return RegimeAssessment(MarketRegime.TREND_UP, trend_score, trend_score, vol_score, ("POSITIVE_RETURN",))
    if ret < 0 and breakout <= 0:
        return RegimeAssessment(MarketRegime.TREND_DOWN, trend_score, trend_score, vol_score, ("NEGATIVE_RETURN",))
    return RegimeAssessment(MarketRegime.RANGE, 1.0 - trend_score, trend_score, vol_score, ("TREND_EVIDENCE_CONFLICTING",))


def _unknown(reason: str) -> RegimeAssessment:
    return RegimeAssessment(MarketRegime.UNKNOWN, 0.0, 0.0, None, (reason,))


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("regime features must be finite numeric values")
    return float(value)
