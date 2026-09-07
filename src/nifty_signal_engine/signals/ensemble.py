"""Small, deterministic probability ensemble for directional research models."""

from __future__ import annotations

import math
from collections.abc import Sequence

from nifty_signal_engine.signals.baselines import DirectionProbabilities


def combine_predictions(
    predictions: Sequence[DirectionProbabilities],
    weights: Sequence[float] | None = None,
) -> tuple[DirectionProbabilities, float]:
    """Return a weighted probability blend and total-variation disagreement.

    The disagreement score is the largest pairwise total-variation distance
    between component distributions. It is suitable for a fail-closed gate:
    high disagreement should produce ``NO_TRADE`` rather than forced direction.
    """

    if not predictions:
        raise ValueError("at least one prediction is required")
    first = predictions[0]
    if any(item.instrument != first.instrument for item in predictions):
        raise ValueError("all predictions must use the same instrument")
    if weights is None:
        weights = [1.0] * len(predictions)
    if len(weights) != len(predictions) or any(
        isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in weights
    ):
        raise ValueError("weights must be finite positive values")
    total = float(sum(weights))
    values = [
        sum(weight * getattr(item, name) for item, weight in zip(predictions, weights, strict=True)) / total
        for name in ("up", "down", "no_move")
    ]
    # Preserve the first model's provenance only when all components agree.
    provenance = first if all(item.model_version == first.model_version for item in predictions) else None
    result = DirectionProbabilities(
        up=values[0], down=values[1], no_move=values[2], instrument=first.instrument,
        feature_available_at=first.feature_available_at if provenance else None,
        model_version=first.model_version if provenance else "ensemble-v1",
        feature_schema_version=first.feature_schema_version if provenance else None,
    )
    disagreement = max(
        (sum(abs(getattr(a, name) - getattr(b, name)) for name in ("up", "down", "no_move")) / 2
         for index, a in enumerate(predictions) for b in predictions[index + 1:]),
        default=0.0,
    )
    return result, disagreement
