"""Pure, auditable option exposure aggregation."""

from dataclasses import dataclass
from typing import Literal

from nifty_signal_engine.calculations.greeks import black_scholes_greeks
from nifty_signal_engine.config.instruments import InstrumentConfig
from nifty_signal_engine.domain.exposure import ExposureResult
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote

Weighting = Literal["OI", "CUMULATIVE_VOLUME", "INCREMENTAL_VOLUME"]
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


def aggregate_snapshot_exposure(
    snapshot: OptionChainSnapshot,
    config: InstrumentConfig,
    years: float,
    rate: float,
    weighting: Weighting,
    incremental_weights: tuple[int, ...] | None = None,
) -> ExposureResult:
    """Aggregate one validated snapshot using its instrument configuration.

    This is the production entry point. The tuple-based aggregator below is a
    homogeneous low-level primitive retained for calculation and replay use.
    """
    if snapshot.instrument != config.instrument:
        raise ValueError("snapshot instrument must match instrument config")
    if any(quote.expiry != snapshot.expiry for quote in snapshot.quotes):
        raise ValueError("every quote expiry must match snapshot expiry")
    return aggregate_exposure(
        snapshot.quotes,
        snapshot.spot,
        years,
        rate,
        config.lot_size,
        weighting,
        incremental_weights,
    )


def aggregate_exposure(
    quotes: tuple[OptionQuote, ...],
    spot: float,
    years: float,
    rate: float,
    lot_size: int,
    weighting: Weighting,
    incremental_weights: tuple[int, ...] | None = None,
) -> ExposureResult:
    """Aggregate a homogeneous quote batch with explicit OI/volume weighting."""
    strike_exposures = calculate_strike_exposures(
        quotes, spot, years, rate, lot_size, weighting, incremental_weights
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
    incremental_weights: tuple[int, ...] | None = None,
) -> tuple[StrikeExposure, ...]:
    """Return exposure by strike for one expiry-homogeneous quote batch."""
    _validate_expiries(quotes)
    _validate_weighting(weighting)
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    weights = _resolve_weights(quotes, weighting, incremental_weights)
    totals: dict[float, dict[str, float]] = {}
    for quote, weight in zip(quotes, weights, strict=True):
        values = totals.setdefault(
            quote.strike,
            {"call_gex": 0.0, "put_gex": 0.0, "call_dex": 0.0, "put_dex": 0.0},
        )
        gex, dex = _quote_exposure(quote, spot, years, rate, lot_size, weight)
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
    selected = min(candidates, key=lambda item: item[1]) if option_type == "PE" else max(
        candidates, key=lambda item: item[1]
    )
    return ExposureWall(
        strike=selected[0],
        signed_exposure_rupees=selected[1],
        strike_universe=tuple(item.strike for item in strike_exposures),
    )


def _validate_expiries(quotes: tuple[OptionQuote, ...]) -> None:
    if len({quote.expiry for quote in quotes}) > 1:
        raise ValueError("quote batch must contain one consistent expiry metadata value")


def _validate_weighting(weighting: str) -> None:
    if weighting == "VOLUME":
        raise ValueError("'VOLUME' is ambiguous; use 'CUMULATIVE_VOLUME'")
    if weighting not in {"OI", "CUMULATIVE_VOLUME", "INCREMENTAL_VOLUME"}:
        raise ValueError("weighting must be OI, CUMULATIVE_VOLUME, or INCREMENTAL_VOLUME")


def _resolve_weights(
    quotes: tuple[OptionQuote, ...],
    weighting: Weighting,
    incremental_weights: tuple[int, ...] | None,
) -> tuple[int, ...]:
    if weighting == "OI":
        return tuple(quote.oi for quote in quotes)
    if weighting == "CUMULATIVE_VOLUME":
        return tuple(quote.volume for quote in quotes)
    if incremental_weights is None:
        raise ValueError("incremental_weights are required for INCREMENTAL_VOLUME")
    if len(incremental_weights) != len(quotes):
        raise ValueError("incremental_weights must be aligned with quotes")
    if any(weight < 0 for weight in incremental_weights):
        raise ValueError("incremental_weights must be non-negative")
    return incremental_weights


def _quote_exposure(
    quote: OptionQuote, spot: float, years: float, rate: float, lot_size: int, weight: int
) -> tuple[float, float]:
    if quote.iv is None:
        raise ValueError("quote.iv is required for exposure aggregation")
    greeks = black_scholes_greeks(
        spot, quote.strike, years, rate, quote.iv, quote.option_type
    )
    gex = greeks.gamma * weight * lot_size * spot**2 * 0.01
    dex = greeks.delta * weight * lot_size * spot
    return (gex if quote.option_type == "CE" else -gex, dex)


def _wall_value_name(exposure_kind: ExposureKind, option_type: OptionType) -> str:
    if exposure_kind not in {"GEX", "DEX"}:
        raise ValueError("exposure_kind must be 'GEX' or 'DEX'")
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be 'CE' or 'PE'")
    prefix = "call" if option_type == "CE" else "put"
    suffix = "gex" if exposure_kind == "GEX" else "dex"
    return f"{prefix}_{suffix}_rupees"
