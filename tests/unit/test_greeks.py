"""Behavioural tests for Black-Scholes Greeks."""

from math import isclose, isfinite

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nifty_signal_engine.calculations.greeks import black_scholes_greeks


def test_call_put_delta_parity_and_gamma_equality() -> None:
    """Catch a call/put calculation that breaks Black-Scholes parity."""
    call = black_scholes_greeks(100, 100, 0.25, 0.07, 0.20, "CE")
    put = black_scholes_greeks(100, 100, 0.25, 0.07, 0.20, "PE")

    assert isclose(call.delta - put.delta, 1.0, abs_tol=1e-12)
    assert isclose(call.gamma, put.gamma, rel_tol=1e-12)


@pytest.mark.parametrize("invalid_value", [0.0, -0.01])
@pytest.mark.parametrize("parameter", ["spot", "strike", "years", "volatility"])
def test_rejects_non_positive_required_inputs(
    parameter: str, invalid_value: float
) -> None:
    """Catch silently converted invalid market inputs."""
    with pytest.raises(ValueError):
        if parameter == "spot":
            black_scholes_greeks(invalid_value, 100.0, 0.25, 0.07, 0.20, "CE")
        elif parameter == "strike":
            black_scholes_greeks(100.0, invalid_value, 0.25, 0.07, 0.20, "CE")
        elif parameter == "years":
            black_scholes_greeks(100.0, 100.0, invalid_value, 0.07, 0.20, "CE")
        else:
            black_scholes_greeks(100.0, 100.0, 0.25, 0.07, invalid_value, "CE")


@given(
    spot=st.floats(min_value=90.0, max_value=110.0, allow_nan=False),
    strike=st.floats(min_value=90.0, max_value=110.0, allow_nan=False),
    years=st.floats(min_value=0.1, max_value=1.0, allow_nan=False),
    volatility=st.floats(min_value=0.1, max_value=1.0, allow_nan=False),
)
def test_valid_greeks_are_finite_and_respect_delta_gamma_bounds(
    spot: float, strike: float, years: float, volatility: float
) -> None:
    """Catch non-finite results or invalid option-direction bounds."""
    call = black_scholes_greeks(spot, strike, years, 0.07, volatility, "CE")
    put = black_scholes_greeks(spot, strike, years, 0.07, volatility, "PE")

    assert all(
        isfinite(value)
        for value in (
            call.delta,
            call.gamma,
            call.vanna,
            call.call_charm,
            call.put_charm,
            put.delta,
            put.gamma,
            put.vanna,
            put.call_charm,
            put.put_charm,
        )
    )
    assert 0.0 < call.delta < 1.0
    assert -1.0 < put.delta < 0.0
    assert call.gamma > 0.0
    assert put.gamma > 0.0
