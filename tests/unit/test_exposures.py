"""Tests for auditable option exposure aggregation."""

import pytest

from nifty_signal_engine.calculations.exposures import (
    aggregate_exposure,
    aggregate_snapshot_exposure,
    calculate_strike_exposures,
    find_wall,
)
from nifty_signal_engine.config.instruments import INSTRUMENTS
from nifty_signal_engine.domain.market import OptionChainSnapshot
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


def test_cumulative_volume_weighting_uses_quote_volume_not_open_interest() -> None:
    quote = make_quote(
        timestamp="2026-08-26T10:00:00+05:30", oi=1_000, volume=10
    )

    oi_result = aggregate_exposure((quote,), 24_300, 7 / 365, 0.07, 65, "OI")
    volume_result = aggregate_exposure(
        (quote,), 24_300, 7 / 365, 0.07, 65, "CUMULATIVE_VOLUME"
    )

    assert volume_result.call_gex_rupees == pytest.approx(
        oi_result.call_gex_rupees / 100
    )


def test_ambiguous_volume_alias_is_rejected() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")

    with pytest.raises(ValueError, match="CUMULATIVE_VOLUME"):
        aggregate_exposure((quote,), 24_300, 7 / 365, 0.07, 65, "VOLUME")  # type: ignore[arg-type]


def test_incremental_volume_requires_separate_aligned_weights() -> None:
    quotes = (
        make_quote(timestamp="2026-08-26T10:00:00+05:30", volume=1_000),
        make_quote(timestamp="2026-08-26T10:00:00+05:30", option_type="PE", volume=1_000),
    )

    with pytest.raises(ValueError, match="incremental_weights"):
        aggregate_exposure(quotes, 24_300, 7 / 365, 0.07, 65, "INCREMENTAL_VOLUME")
    with pytest.raises(ValueError, match="aligned"):
        aggregate_exposure(
            quotes, 24_300, 7 / 365, 0.07, 65, "INCREMENTAL_VOLUME", (10,)
        )


def test_incremental_volume_uses_supplied_weights_not_cumulative_volume() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30", volume=1_000)

    result = aggregate_exposure(
        (quote,), 24_300, 7 / 365, 0.07, 65, "INCREMENTAL_VOLUME", (10,)
    )
    cumulative = aggregate_exposure(
        (quote,), 24_300, 7 / 365, 0.07, 65, "CUMULATIVE_VOLUME"
    )

    assert result.call_gex_rupees == pytest.approx(cumulative.call_gex_rupees / 100)


def test_incremental_volume_rejects_negative_weights() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")

    with pytest.raises(ValueError, match="non-negative"):
        aggregate_exposure(
            (quote,), 24_300, 7 / 365, 0.07, 65, "INCREMENTAL_VOLUME", (-1,)
        )


def test_unknown_weighting_is_rejected() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")

    with pytest.raises(ValueError, match="weighting"):
        aggregate_exposure((quote,), 24_300, 7 / 365, 0.07, 65, "UNKNOWN")  # type: ignore[arg-type]


def test_mixed_expiry_metadata_is_rejected() -> None:
    dated = make_quote(timestamp="2026-08-26T10:00:00+05:30")
    undated = dated.model_copy(update={"expiry": None})

    with pytest.raises(ValueError, match="expiry"):
        aggregate_exposure((dated, undated), 24_300, 7 / 365, 0.07, 65, "OI")


def test_multiple_non_null_expiries_are_rejected() -> None:
    first = make_quote(timestamp="2026-08-26T10:00:00+05:30")
    second = first.model_copy(update={"expiry": first.expiry.replace(day=2)})

    with pytest.raises(ValueError, match="expiry"):
        aggregate_exposure((first, second), 24_300, 7 / 365, 0.07, 65, "OI")


def test_snapshot_entry_point_rejects_instrument_config_mismatch() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")
    snapshot = OptionChainSnapshot(
        instrument="NIFTY",
        source_timestamp=quote.timestamp,
        received_at=quote.timestamp,
        spot=24_300,
        expiry=quote.expiry,
        quotes=(quote,),
    )

    with pytest.raises(ValueError, match="instrument"):
        aggregate_snapshot_exposure(
            snapshot, INSTRUMENTS["BANKNIFTY"], 7 / 365, 0.07, "OI"
        )


def test_snapshot_entry_point_rejects_quote_expiry_mismatch() -> None:
    quote = make_quote(timestamp="2026-08-26T10:00:00+05:30")
    mismatched_quote = quote.model_copy(update={"expiry": quote.expiry.replace(day=2)})
    snapshot = OptionChainSnapshot(
        instrument="NIFTY",
        source_timestamp=quote.timestamp,
        received_at=quote.timestamp,
        spot=24_300,
        expiry=quote.expiry,
        quotes=(mismatched_quote,),
    )

    with pytest.raises(ValueError, match="expiry"):
        aggregate_snapshot_exposure(snapshot, INSTRUMENTS["NIFTY"], 7 / 365, 0.07, "OI")


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
