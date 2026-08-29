"""Behavioural tests for explicit rupee/crore conversions."""

from nifty_signal_engine.calculations.units import crore_to_rupees, rupees_to_crore


def test_crore_round_trip_has_no_extra_thousand_scale() -> None:
    """Catch a crore conversion that accidentally applies a second scale."""
    assert rupees_to_crore(10_000_000) == 1.0
    assert crore_to_rupees(1.0) == 10_000_000


def test_conversion_round_trip_preserves_a_fractional_crore_value() -> None:
    """Catch loss of fractional exposure while converting between units."""
    assert rupees_to_crore(crore_to_rupees(2.75)) == 2.75
