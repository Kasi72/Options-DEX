"""Option-only features; model Greeks use explicit configured lots and rate."""

from datetime import datetime
from typing import Literal

from nifty_signal_engine.calculations.exposures import (
    aggregate_snapshot_exposure,
    calculate_strike_exposures,
    find_wall,
)
from nifty_signal_engine.calculations.zero_levels import find_zero_level
from nifty_signal_engine.config.instruments import InstrumentConfig
from nifty_signal_engine.config.market_hours import IST, REGULAR_CLOSE
from nifty_signal_engine.domain.market import OptionChainSnapshot

FeatureValue = float | int | str | bool | None


def structure_values(
    snapshot: OptionChainSnapshot,
    config: InstrumentConfig,
    weights: tuple[int, ...],
    rate: float,
) -> dict[str, FeatureValue]:
    expiry_close = datetime.combine(snapshot.expiry, REGULAR_CLOSE, IST)
    years = (expiry_close - snapshot.source_timestamp).total_seconds() / (365 * 86400)
    if years <= 0:
        raise ValueError("EXPIRED_EXPIRY")
    oi = aggregate_snapshot_exposure(snapshot, config, years, rate, "OI")
    flow = aggregate_snapshot_exposure(
        snapshot, config, years, rate, "INCREMENTAL_VOLUME", weights
    )
    values: dict[str, FeatureValue] = {
        "oi_gex_rupees": oi.net_gex_rupees,
        "oi_gex_crore": oi.net_gex_crore,
        "oi_dex_rupees": oi.net_dex_rupees,
        "oi_dex_crore": oi.net_dex_crore,
        "flow_gex_rupees": flow.net_gex_rupees,
        "flow_gex_crore": flow.net_gex_crore,
        "flow_dex_rupees": flow.net_dex_rupees,
        "flow_dex_crore": flow.net_dex_crore,
        "dte_days": years * 365,
    }
    strikes = calculate_strike_exposures(
        snapshot.quotes, snapshot.spot, years, rate, config.lot_size, "OI"
    )
    sides: tuple[tuple[Literal["CE", "PE"], str], ...] = (("CE", "call"), ("PE", "put"))
    for side, label in sides:
        wall = find_wall(strikes, "GEX", side)
        values[f"{label}_wall_distance"] = (
            None if wall is None else wall.strike - snapshot.spot
        )
        values[f"{label}_wall_valid"] = wall is not None
        values[f"{label}_oi"] = sum(
            q.oi for q in snapshot.quotes if q.option_type == side
        )
    # Zero search is bounded to the observed strike universe, never extrapolated.
    lower, upper = (
        min(q.strike for q in snapshot.quotes),
        max(q.strike for q in snapshot.quotes),
    )
    for kind in ("gex", "dex"):

        def objective(spot: float, exposure_kind: str = kind) -> float:
            result = aggregate_snapshot_exposure(
                snapshot.model_copy(update={"spot": spot}), config, years, rate, "OI"
            )
            return (
                result.net_gex_rupees
                if exposure_kind == "gex"
                else result.net_dex_rupees
            )

        zero = find_zero_level(objective, lower, upper)
        values[f"zero_{kind}_distance"] = (
            None if zero.level is None else zero.level - snapshot.spot
        )
        values[f"zero_{kind}_status"] = zero.status.value
    for field, name in (("oi", "oi_concentration"), ("volume", "volume_concentration")):
        by_strike: dict[float, int] = {}
        for quote in snapshot.quotes:
            by_strike[quote.strike] = by_strike.get(quote.strike, 0) + getattr(
                quote, field
            )
        total = sum(by_strike.values())
        values[name] = (
            sum((v / total) ** 2 for v in by_strike.values()) if total else None
        )
    atm_strike = min(
        (q.strike for q in snapshot.quotes), key=lambda k: (abs(k - snapshot.spot), k)
    )
    call = next(
        q for q in snapshot.quotes if q.strike == atm_strike and q.option_type == "CE"
    )
    put = next(
        q for q in snapshot.quotes if q.strike == atm_strike and q.option_type == "PE"
    )
    assert call.iv is not None and put.iv is not None
    values["atm_iv"] = (call.iv + put.iv) / 2
    values["atm_skew"] = put.iv - call.iv
    values["atm_straddle"] = (
        call.ltp + put.ltp if call.ltp is not None and put.ltp is not None else None
    )
    return values
