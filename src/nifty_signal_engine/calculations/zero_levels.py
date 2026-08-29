"""Honest zero-exposure root finding from a sampled finite curve."""

from collections.abc import Callable
from itertools import pairwise
from math import isfinite
from numbers import Real

from scipy.optimize import brentq  # type: ignore[import-untyped]

from nifty_signal_engine.domain.exposure import ZeroLevelResult, ZeroLevelStatus

GRID_SAMPLES = 101
ZERO_TOLERANCE = 1e-12
RESIDUAL_RELATIVE_TOLERANCE = 1e-8


def find_zero_level(
    objective: Callable[[float], float], lower: float, upper: float
) -> ZeroLevelResult:
    """Return the lowest sampled sign-changing Brent root or an honest status.

    ``ALL_ZERO`` means every finite deterministic grid sample lies within
    ``ZERO_TOLERANCE``. It does not assert the continuous curve is zero between
    sampled points.
    """
    if not _valid_bounds(lower, upper):
        return _numerical_error()
    samples = _sample_curve(objective, lower, upper)
    if samples is None:
        return _numerical_error()
    values = tuple(value for _, value in samples)
    if all(abs(value) <= ZERO_TOLERANCE for value in values):
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.ALL_ZERO)
    bracket = _first_crossing_bracket(samples)
    if bracket is None:
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NO_SIGN_CHANGE)
    try:
        root = brentq(objective, *bracket)
        residual = _finite_objective_value(objective, root)
    except (ArithmeticError, RuntimeError, TypeError, ValueError):
        return _numerical_error()
    if residual is None or not isfinite(root):
        return _numerical_error()
    scale = max(1.0, *(abs(value) for value in values))
    if abs(residual) > RESIDUAL_RELATIVE_TOLERANCE * scale:
        return _numerical_error()
    return ZeroLevelResult(level=root, status=ZeroLevelStatus.VALID)


def _sample_curve(
    objective: Callable[[float], float], lower: float, upper: float
) -> tuple[tuple[float, float], ...] | None:
    step = (upper - lower) / (GRID_SAMPLES - 1)
    samples: list[tuple[float, float]] = []
    for index in range(GRID_SAMPLES):
        point = lower + index * step
        value = _finite_objective_value(objective, point)
        if value is None:
            return None
        samples.append((point, value))
    return tuple(samples)


def _first_crossing_bracket(
    samples: tuple[tuple[float, float], ...]
) -> tuple[float, float] | None:
    for (left_x, left_value), (right_x, right_value) in pairwise(samples):
        if left_value * right_value < 0:
            return left_x, right_x
    for index in range(1, len(samples) - 1):
        left_x, left_value = samples[index - 1]
        _, value = samples[index]
        right_x, right_value = samples[index + 1]
        if abs(value) <= ZERO_TOLERANCE and left_value * right_value < 0:
            return left_x, right_x
    return None


def _finite_objective_value(
    objective: Callable[[float], float], point: float
) -> float | None:
    try:
        value = objective(point)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    numeric_value = float(value)
    return numeric_value if isfinite(numeric_value) else None


def _valid_bounds(lower: float, upper: float) -> bool:
    return isfinite(lower) and isfinite(upper) and lower < upper


def _numerical_error() -> ZeroLevelResult:
    return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)
