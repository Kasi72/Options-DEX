"""Black-Scholes Greek calculations."""

from dataclasses import dataclass
from math import exp, isfinite, log, sqrt
from typing import Literal

from scipy.stats import norm  # type: ignore[import-untyped]

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class Greeks:
    """Greeks calculated at one spot, strike, and expiry state."""

    delta: float
    gamma: float
    vanna: float
    call_charm: float
    put_charm: float


def black_scholes_greeks(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
    option_type: OptionType,
) -> Greeks:
    """Return prototype-compatible Black-Scholes Greeks for a call or put."""
    _validate_numeric_inputs(spot, strike, years, rate, volatility)
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be 'CE' or 'PE'")

    sqrt_years = sqrt(years)
    d1 = (log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (
        volatility * sqrt_years
    )
    d2 = d1 - volatility * sqrt_years
    pdf_d1 = float(norm.pdf(d1))
    call_delta = float(norm.cdf(d1))
    gamma = pdf_d1 / (spot * volatility * sqrt_years)
    vanna = -pdf_d1 * d2 / volatility
    charm_term = pdf_d1 * (2 * rate * years - d2 * volatility * sqrt_years) / (
        2 * years * volatility * sqrt_years
    )
    call_charm = charm_term - rate * exp(-rate * years) * call_delta
    put_charm = charm_term + rate * exp(-rate * years) * float(norm.cdf(-d1))

    return Greeks(
        delta=call_delta if option_type == "CE" else call_delta - 1.0,
        gamma=gamma,
        vanna=vanna,
        call_charm=call_charm,
        put_charm=put_charm,
    )


def _validate_numeric_inputs(
    spot: float, strike: float, years: float, rate: float, volatility: float
) -> None:
    for name, value in (
        ("spot", spot),
        ("strike", strike),
        ("years", years),
        ("rate", rate),
        ("volatility", volatility),
    ):
        if not isfinite(value):
            raise ValueError(f"{name} must be finite")
    if spot <= 0:
        raise ValueError("spot must be positive")
    if strike <= 0:
        raise ValueError("strike must be positive")
    if years <= 0:
        raise ValueError("years must be positive")
    if volatility <= 0:
        raise ValueError("volatility must be positive")
