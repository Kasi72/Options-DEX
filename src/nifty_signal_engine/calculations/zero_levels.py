"""Honest zero-exposure root finding from a sampled finite curve."""

from collections.abc import Callable
from itertools import pairwise
from math import isfinite, ulp
from numbers import Real

from scipy.optimize import brentq  # type: ignore[import-untyped]

from nifty_signal_engine.domain.exposure import ZeroLevelResult, ZeroLevelStatus

GRID_SAMPLES = 101
ZERO_TOLERANCE = 1e-12
RESIDUAL_ABSOLUTE_FLOOR = 1e-15
RESIDUAL_ULP_FACTOR = 32


def find_zero_level(
    objective: Callable[[float], float], lower: object, upper: object
) -> ZeroLevelResult:
    """Return the lowest sampled sign-changing Brent root or an honest status.

    ``ALL_ZERO`` means every finite deterministic grid sample lies within
    ``ZERO_TOLERANCE``. A valid root has a genuine sampled sign bracket and a
    near-machine-precision residual; this cannot prove black-box continuity.
    """
    bounds = _normalise_bounds(lower, upper)
    if bounds is None:
        return _numerical_error()
    lower_bound, upper_bound = bounds
    samples = _sample_curve(objective, lower_bound, upper_bound)
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
    except Exception:  # noqa: BLE001 - objective failures map to a typed status.
        return _numerical_error()
    if residual is None or not isfinite(root):
        return _numerical_error()
    scale = max(1.0, *(abs(value) for value in values))
    residual_tolerance = max(RESIDUAL_ABSOLUTE_FLOOR, RESIDUAL_ULP_FACTOR * ulp(scale))
    if abs(residual) > residual_tolerance:
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
    except Exception:  # noqa: BLE001 - objective failures map to a typed status.
        return None
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    numeric_value = float(value)
    return numeric_value if isfinite(numeric_value) else None


def _normalise_bounds(lower: object, upper: object) -> tuple[float, float] | None:
    if (
        not isinstance(lower, Real)
        or isinstance(lower, bool)
        or not isinstance(upper, Real)
        or isinstance(upper, bool)
    ):
        return None
    lower_bound = float(lower)
    upper_bound = float(upper)
    if not isfinite(lower_bound) or not isfinite(upper_bound) or lower_bound >= upper_bound:
        return None
    return lower_bound, upper_bound


def _numerical_error() -> ZeroLevelResult:
    return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)
