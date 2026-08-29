"""Tests for auditable option exposure aggregation."""

import pytest

from nifty_signal_engine.calculations.exposures import (
    aggregate_exposure,
    calculate_strike_exposures,
    find_wall,
)
from tests.factories import make_quote


def test_net_gex_is_call_plus_signed_put() -> None:
    quotes = (
        make_quote(timestamp="2026-08-26T10:00:00+05:30", option_type="CE", oi=1_000),
        make_quote(timestamp="2026-08-26T10:00:00+05:30", option_type="PE", oi=900),
    )

    result = aggregate_exposure(quotes, 24_300, 7 / 365, 0.07, 65, "OI")

    assert result.call_gex_rupees > 0
    assert result.put_gex_rupees < 0
    assert result.net_gex_rupees == pytest.approx(
        result.call_gex_rupees + result.put_gex_rupees
    )
    assert result.net_gex_crore == pytest.approx(result.net_gex_rupees / 10_000_000)


def test_volume_weighting_uses_cumulative_volume_not_open_interest() -> None:
    quote = make_quote(
        timestamp="2026-08-26T10:00:00+05:30", oi=1_000, volume=10
    )

    oi_result = aggregate_exposure((quote,), 24_300, 7 / 365, 0.07, 65, "OI")
    volume_result = aggregate_exposure(
        (quote,), 24_300, 7 / 365, 0.07, 65, "VOLUME"
    )

    assert volume_result.call_gex_rupees == pytest.approx(
        oi_result.call_gex_rupees / 100
    )


def test_unknown_weighting_is_rejected() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")

    with pytest.raises(ValueError, match="weighting"):
        aggregate_exposure((quote,), 24_300, 7 / 365, 0.07, 65, "UNKNOWN")  # type: ignore[arg-type]


def test_put_gex_wall_preserves_signed_exposure_and_universe() -> None:
    quotes = (
        make_quote(timestamp="2026-08-26T10:00:00+05:30", strike=24_200, option_type="PE", oi=500),
        make_quote(timestamp="2026-08-26T10:00:00+05:30", strike=24_300, option_type="PE", oi=1_000),
    )
    strikes = calculate_strike_exposures(quotes, 24_300, 7 / 365, 0.07, 65, "OI")

    wall = find_wall(strikes, "GEX", "PE")

    assert wall is not None
    assert wall.strike == 24_300
    assert wall.signed_exposure_rupees < 0
    assert wall.strike_universe == (24_200, 24_300)


def test_missing_wall_is_none_not_zero() -> None:
    assert find_wall((), "GEX", "CE") is None
