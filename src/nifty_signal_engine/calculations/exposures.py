"""Pure, auditable option exposure aggregation."""

from dataclasses import dataclass
from typing import Literal

from nifty_signal_engine.calculations.greeks import black_scholes_greeks
from nifty_signal_engine.domain.exposure import ExposureResult
from nifty_signal_engine.domain.market import OptionQuote

Weighting = Literal["OI", "VOLUME"]
ExposureKind = Literal["GEX", "DEX"]
OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class StrikeExposure:
    """Signed call and put exposure at one strike for a single weighting layer."""

    strike: float
    call_gex_rupees: float = 0.0
    put_gex_rupees: float = 0.0
    call_dex_rupees: float = 0.0
    put_dex_rupees: float = 0.0


@dataclass(frozen=True)
class ExposureWall:
    """The selected extreme exposure and the strike set it was selected from."""

    strike: float
    signed_exposure_rupees: float
    strike_universe: tuple[float, ...]


def aggregate_exposure(
    quotes: tuple[OptionQuote, ...],
    spot: float,
    years: float,
    rate: float,
    lot_size: int,
    weighting: Weighting,
) -> ExposureResult:
    """Aggregate signed GEX and DEX using either OI or cumulative volume."""
    strike_exposures = calculate_strike_exposures(
        quotes, spot, years, rate, lot_size, weighting
    )
    call_gex = sum(item.call_gex_rupees for item in strike_exposures)
    put_gex = sum(item.put_gex_rupees for item in strike_exposures)
    call_dex = sum(item.call_dex_rupees for item in strike_exposures)
    put_dex = sum(item.put_dex_rupees for item in strike_exposures)
    return ExposureResult(
        call_gex_rupees=call_gex,
        put_gex_rupees=put_gex,
        net_gex_rupees=call_gex + put_gex,
        call_dex_rupees=call_dex,
        put_dex_rupees=put_dex,
        net_dex_rupees=call_dex + put_dex,
    )


def calculate_strike_exposures(
    quotes: tuple[OptionQuote, ...],
    spot: float,
    years: float,
    rate: float,
    lot_size: int,
    weighting: Weighting,
) -> tuple[StrikeExposure, ...]:
    """Return signed exposure by strike; volume is intentionally cumulative here."""
    _validate_weighting(weighting)
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")

    totals: dict[float, dict[str, float]] = {}
    for quote in quotes:
        values = totals.setdefault(
            quote.strike,
            {
                "call_gex": 0.0,
                "put_gex": 0.0,
                "call_dex": 0.0,
                "put_dex": 0.0,
            },
        )
        gex, dex = _quote_exposure(quote, spot, years, rate, lot_size, weighting)
        if quote.option_type == "CE":
            values["call_gex"] += gex
            values["call_dex"] += dex
        else:
            values["put_gex"] += gex
            values["put_dex"] += dex

    return tuple(
        StrikeExposure(
            strike=strike,
            call_gex_rupees=values["call_gex"],
            put_gex_rupees=values["put_gex"],
            call_dex_rupees=values["call_dex"],
            put_dex_rupees=values["put_dex"],
        )
        for strike, values in sorted(totals.items())
    )


def find_wall(
    strike_exposures: tuple[StrikeExposure, ...],
    exposure_kind: ExposureKind,
    option_type: OptionType,
) -> ExposureWall | None:
    """Find the strongest signed call or put wall, or return ``None`` if absent."""
    if not strike_exposures:
        return None

    value_name = _wall_value_name(exposure_kind, option_type)
    candidates = [
        (item.strike, getattr(item, value_name))
        for item in strike_exposures
        if getattr(item, value_name) != 0.0
    ]
    if not candidates:
        return None

    selected = max(candidates, key=lambda item: item[1])
    if option_type == "PE":
        selected = min(candidates, key=lambda item: item[1])
    return ExposureWall(
        strike=selected[0],
        signed_exposure_rupees=selected[1],
        strike_universe=tuple(item.strike for item in strike_exposures),
    )


def _quote_exposure(
    quote: OptionQuote,
    spot: float,
    years: float,
    rate: float,
    lot_size: int,
    weighting: Weighting,
) -> tuple[float, float]:
    if quote.iv is None:
        raise ValueError("quote.iv is required for exposure aggregation")
    weight = quote.oi if weighting == "OI" else quote.volume
    greeks = black_scholes_greeks(
        spot, quote.strike, years, rate, quote.iv, quote.option_type
    )
    gex = greeks.gamma * weight * lot_size * spot**2 * 0.01
    dex = greeks.delta * weight * lot_size * spot
    return (gex if quote.option_type == "CE" else -gex, dex)


def _validate_weighting(weighting: str) -> None:
    if weighting not in {"OI", "VOLUME"}:
        raise ValueError("weighting must be 'OI' or 'VOLUME'")


def _wall_value_name(exposure_kind: ExposureKind, option_type: OptionType) -> str:
    if exposure_kind not in {"GEX", "DEX"}:
        raise ValueError("exposure_kind must be 'GEX' or 'DEX'")
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be 'CE' or 'PE'")
    prefix = "call" if option_type == "CE" else "put"
    suffix = "gex" if exposure_kind == "GEX" else "dex"
    return f"{prefix}_{suffix}_rupees"
