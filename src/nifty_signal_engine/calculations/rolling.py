"""Leakage-safe rolling statistics."""

import math
from collections.abc import Sequence


def rolling_zscore(
    current: float,
    history: Sequence[float],
    minimum: int = 20,
    window: int = 20,
) -> float | None:
    """Return a z-score against strictly prior observations, when estimable.

    ``history`` is never mutated and the current value is deliberately not
    included in the reference distribution.  A zero or invalid variance is
    unavailable rather than being represented by a fabricated zero.
    """
    if minimum < 1 or window < 1:
        raise ValueError("minimum and window must be positive")
    if not math.isfinite(current) or len(history) < minimum:
        return None
    prior = tuple(history[-window:])
    if len(prior) < minimum or not all(math.isfinite(value) for value in prior):
        return None
    mean = sum(prior) / len(prior)
    variance = sum((value - mean) ** 2 for value in prior) / len(prior)
    if variance <= 0.0 or not math.isfinite(variance):
        return None
    result = (current - mean) / math.sqrt(variance)
    return result if math.isfinite(result) else None
