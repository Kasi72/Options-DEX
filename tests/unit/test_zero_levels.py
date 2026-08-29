"""Tests for honest zero-exposure levels."""

from nifty_signal_engine.calculations.zero_levels import find_zero_level
from nifty_signal_engine.domain.exposure import ZeroLevelStatus


def test_all_zero_curve_returns_null_level() -> None:
    result = find_zero_level(lambda _: 0.0, 23_000, 25_000)

    assert result.level is None
    assert result.status is ZeroLevelStatus.ALL_ZERO


def test_no_sign_change_is_not_reported_as_spot() -> None:
    result = find_zero_level(lambda x: x * x + 1, 23_000, 25_000)

    assert result.level is None
    assert result.status is ZeroLevelStatus.NO_SIGN_CHANGE


def test_sign_changing_curve_returns_brent_root() -> None:
    result = find_zero_level(lambda x: x - 24_000, 23_000, 25_000)

    assert result.level == 24_000
    assert result.status is ZeroLevelStatus.VALID


def test_non_finite_objective_returns_numerical_error() -> None:
    result = find_zero_level(lambda _: float("nan"), 23_000, 25_000)

    assert result.level is None
    assert result.status is ZeroLevelStatus.NUMERICAL_ERROR
