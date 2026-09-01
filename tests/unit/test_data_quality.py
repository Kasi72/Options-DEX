from datetime import timedelta

from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    assess_snapshot,
)
from tests.factories import aware, make_chain


def test_stale_snapshot_is_non_tradable() -> None:
    """Increasing the freshness allowance must not make a minute-old feed tradable."""
    current = make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100)

    report = assess_snapshot(current, None, aware("2026-08-26T09:16:00+05:30"))

    assert report.tradable is False
    assert DataQualityCode.STALE_SOURCE in report.codes


def test_preopen_zero_volume_is_non_tradable_not_an_exception() -> None:
    """Removing the pre-open gate must not turn an unopened market into a trade."""
    current = make_chain(timestamp="2026-08-26T09:10:00+05:30")

    report = assess_snapshot(current, None, aware("2026-08-26T09:10:01+05:30"))

    assert report.tradable is False
    assert DataQualityCode.PREOPEN in report.codes
    assert DataQualityCode.ZERO_VOLUME in report.codes


def test_incomplete_strike_invalid_quote_and_iv_are_explicit_non_tradable_codes() -> (
    None
):
    """Dropping quote validation must not make a one-sided malformed chain usable."""
    chain = make_chain(
        timestamp="2026-08-26T10:00:00+05:30", call_volume=10, put_volume=10
    )
    malformed_call = chain.quotes[0].model_copy(
        update={"bid": 102.0, "ask": 101.0, "iv": 1.2}
    )
    one_sided = chain.model_copy(update={"quotes": (malformed_call,)})

    report = assess_snapshot(
        one_sided, None, chain.source_timestamp + timedelta(seconds=1)
    )

    assert report.tradable is False
    assert DataQualityCode.INCOMPLETE_STRIKES in report.codes
    assert DataQualityCode.INVALID_QUOTE in report.codes
    assert DataQualityCode.INVALID_IV in report.codes


def test_out_of_order_and_counter_reset_are_recorded_per_instrument_session() -> None:
    """Ignoring time order or counter decreases must not produce incremental features."""
    previous = make_chain(
        timestamp="2026-08-26T10:01:00+05:30", call_volume=100, put_volume=100
    )
    current = make_chain(
        timestamp="2026-08-26T10:00:00+05:30", call_volume=90, put_volume=110
    )

    report = assess_snapshot(current, previous, aware("2026-08-26T10:00:01+05:30"))

    assert report.tradable is False
    assert DataQualityCode.OUT_OF_ORDER in report.codes
    assert DataQualityCode.COUNTER_RESET in report.codes
    assert report.details["order"] == "source_timestamp_not_after_previous"
    assert report.details["counter_reset"] == "cumulative_volume_decreased"
