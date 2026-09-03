from datetime import date, timedelta

from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    QualityConfig,
    TradingCalendar,
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


def test_fresh_receipt_cannot_mask_a_stale_authoritative_broker_timestamp() -> None:
    """Replacing broker time with local receipt time must not make a stale feed tradable."""
    source = aware("2026-08-26T09:15:00+05:30")
    current = make_chain(
        timestamp="2026-08-26T10:00:00+05:30", call_volume=10, put_volume=10
    )
    current = current.model_copy(
        update={
            "source_timestamp": source,
            "source_time_authoritative": True,
            "received_at": aware("2026-08-26T10:00:00+05:30"),
            "quotes": tuple(
                quote.model_copy(update={"timestamp": source})
                for quote in current.quotes
            ),
        }
    )

    report = assess_snapshot(
        current,
        current,
        aware("2026-08-26T10:00:01+05:30"),
        calendar=TradingCalendar({date(2026, 8, 26)}),
        active_expiries=(date(2026, 9, 1),),
        config=QualityConfig(minimum_paired_strikes=1),
    )

    assert report.tradable is False
    assert DataQualityCode.STALE_SOURCE in report.codes
    assert DataQualityCode.STALE_RECEIPT not in report.codes


def test_missing_authoritative_source_time_is_explicitly_non_tradable() -> None:
    """Treating local capture timestamps as authoritative must fail this provenance gate."""
    current = make_chain(
        timestamp="2026-08-26T10:00:00+05:30", call_volume=10, put_volume=10
    )

    report = assess_snapshot(
        current,
        None,
        aware("2026-08-26T10:00:01+05:30"),
        calendar=TradingCalendar({date(2026, 8, 26)}),
        active_expiries=(date(2026, 9, 1),),
        config=QualityConfig(minimum_paired_strikes=1),
    )

    assert report.tradable is False
    assert DataQualityCode.SOURCE_TIME_UNAVAILABLE in report.codes
    assert DataQualityCode.QUOTE_TIME_UNAVAILABLE in report.codes


def test_cross_instrument_baseline_is_never_used() -> None:
    current = make_chain(timestamp="2026-08-26T10:00:00+05:30", call_volume=10)
    previous = current.model_copy(update={"instrument": "BANKNIFTY"})

    report = assess_snapshot(current, previous, current.source_timestamp)

    assert report.tradable is False
    assert DataQualityCode.BASELINE_INSTRUMENT_MISMATCH in report.codes


def test_first_snapshot_after_session_rollover_requires_a_new_baseline() -> None:
    """Skipping a cross-session baseline must not make volume deltas tradeable."""
    previous = make_chain(
        timestamp="2026-08-26T15:29:00+05:30", call_volume=100, put_volume=100
    )
    current = make_chain(
        timestamp="2026-08-27T10:00:00+05:30", call_volume=10, put_volume=10
    )
    for snapshot in (previous, current):
        object.__setattr__(snapshot, "source_time_authoritative", True)

    report = assess_snapshot(
        current,
        previous,
        aware("2026-08-27T10:00:01+05:30"),
        calendar=TradingCalendar({date(2026, 8, 26), date(2026, 8, 27)}),
        active_expiries=(date(2026, 9, 1),),
        config=QualityConfig(minimum_paired_strikes=1),
    )

    assert report.tradable is False
    assert DataQualityCode.SESSION_RESET in report.codes


def test_too_wide_relative_spread_and_insufficient_paired_strikes_are_non_tradable() -> (
    None
):
    """Removing the spread or depth limit must not make an unusable book eligible."""
    current = make_chain(
        timestamp="2026-08-26T10:00:00+05:30", call_volume=10, put_volume=10
    )
    current = current.model_copy(
        update={
            "source_time_authoritative": True,
            "quotes": (
                current.quotes[0].model_copy(update={"bid": 1.0, "ask": 10_000.0}),
                current.quotes[1],
            ),
        }
    )

    report = assess_snapshot(
        current,
        current,
        aware("2026-08-26T10:00:01+05:30"),
        calendar=TradingCalendar({date(2026, 8, 26)}),
        active_expiries=(date(2026, 9, 1),),
        config=QualityConfig(minimum_paired_strikes=2, maximum_relative_spread=0.2),
    )

    assert report.tradable is False
    assert DataQualityCode.EXCESSIVE_SPREAD in report.codes
    assert DataQualityCode.INSUFFICIENT_PAIRED_STRIKES in report.codes


def test_weekend_and_unavailable_calendar_fail_closed() -> None:
    """Assuming calendar validity from a weekday must not permit a trade."""
    current = make_chain(
        timestamp="2026-08-29T10:00:00+05:30", call_volume=10, put_volume=10
    )
    current = current.model_copy(update={"source_time_authoritative": True})

    report = assess_snapshot(
        current,
        current,
        aware("2026-08-29T10:00:01+05:30"),
        active_expiries=(date(2026, 9, 1),),
        config=QualityConfig(minimum_paired_strikes=1),
    )

    assert report.tradable is False
    assert DataQualityCode.WEEKEND in report.codes
    assert DataQualityCode.CALENDAR_UNAVAILABLE in report.codes
