"""Explicit exposure-unit conversions."""

RUPEES_PER_CRORE = 10_000_000


def rupees_to_crore(value: float) -> float:
    """Convert a rupee exposure value to crore."""
    return value / RUPEES_PER_CRORE


def crore_to_rupees(value: float) -> float:
    """Convert a crore exposure value to rupees."""
    return value * RUPEES_PER_CRORE
