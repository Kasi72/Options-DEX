"""Honest zero-exposure root finding."""

from collections.abc import Callable
from math import isfinite

from scipy.optimize import brentq  # type: ignore[import-untyped]

from nifty_signal_engine.domain.exposure import ZeroLevelResult, ZeroLevelStatus


def find_zero_level(
    objective: Callable[[float], float], lower: float, upper: float
) -> ZeroLevelResult:
    """Find a bracketed zero, returning an explicit status when no one exists."""
    if not _valid_bounds(lower, upper):
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)

    try:
        lower_value = objective(lower)
        midpoint_value = objective((lower + upper) / 2)
        upper_value = objective(upper)
    except (ArithmeticError, TypeError, ValueError):
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)

    if not all(isfinite(value) for value in (lower_value, midpoint_value, upper_value)):
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)
    if lower_value == midpoint_value == upper_value == 0.0:
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.ALL_ZERO)
    if lower_value == 0.0:
        return ZeroLevelResult(level=lower, status=ZeroLevelStatus.VALID)
    if upper_value == 0.0:
        return ZeroLevelResult(level=upper, status=ZeroLevelStatus.VALID)
    if lower_value * upper_value > 0:
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NO_SIGN_CHANGE)

    try:
        root = brentq(objective, lower, upper)
    except (ArithmeticError, RuntimeError, TypeError, ValueError):
        return ZeroLevelResult(level=None, status=ZeroLevelStatus.NUMERICAL_ERROR)
    return ZeroLevelResult(level=root, status=ZeroLevelStatus.VALID)


def _valid_bounds(lower: float, upper: float) -> bool:
    return isfinite(lower) and isfinite(upper) and lower < upper
