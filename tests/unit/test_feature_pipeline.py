"""Feature contract tests: every fixture is synthetic authoritative market data."""

import math
from datetime import date, timedelta

import pytest

from nifty_signal_engine.config.instruments import InstrumentConfig
from nifty_signal_engine.features.pipeline import FeaturePipeline, FeatureRow
from nifty_signal_engine.features.price import time_values
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)
from tests.factories import aware, make_chain


def sample(seconds=0, *, volume=100, spot=24300, instrument="NIFTY", day=26):
    stamp = aware(f"2026-08-{day:02d}T09:15:00+05:30") + timedelta(seconds=seconds)
    chain = make_chain(
        timestamp=stamp.isoformat(),
        call_volume=volume,
        put_volume=volume * 2,
        spot=spot,
    )
    return chain.model_copy(
        update={
            "instrument": instrument,
            "source_time_authoritative": True,
            "quotes": tuple(
                q.model_copy(
                    update={
                        "timestamp_authoritative": True,
                        "api_delta": 0.5 if q.option_type == "CE" else -0.5,
                        "api_gamma": 0.001,
                        "ltp": 100.0,
                    }
                )
                for q in chain.quotes
            ),
        }
    )


def quality(chain, *, tradable=True, checked_at=None):
    return DataQualityReport(
        tradable=tradable,
        codes=() if tradable else (DataQualityCode.INVALID_QUOTE,),
        checked_at=checked_at or chain.received_at,
    )


def feed(pipeline, seconds, **kwargs):
    chain = sample(seconds, **kwargs)
    return pipeline.on_snapshot(chain, quality(chain))


def test_incremental_volume_uses_previous_valid_snapshot_only():
    pipeline = FeaturePipeline()
    feed(pipeline, 0)
    bad = sample(10, volume=999)
    assert pipeline.on_snapshot(bad, quality(bad, tradable=False)) is None
    row = feed(pipeline, 15, volume=125)
    assert row.values["call_volume_increment"] == 25
    assert row.values["put_volume_increment"] == 50
    assert row.row_kind == "FLOW" and not row.completed


def test_row_is_deeply_immutable_and_rejects_future_or_naive_sources():
    stamp = sample().received_at
    values = {"spot_return_1m": 0.001}
    row = FeatureRow(
        available_at=stamp,
        instrument="NIFTY",
        session_date=stamp.date(),
        schema_version="1",
        values=values,
        quality_codes=(),
    )
    values["spot_return_1m"] = 99
    assert row.values["spot_return_1m"] == 0.001
    with pytest.raises(TypeError):
        row.values["spot_return_1m"] = 99
    with pytest.raises(ValueError, match="future source timestamp"):
        FeatureRow(
            available_at=stamp,
            instrument="NIFTY",
            session_date=stamp.date(),
            schema_version="1",
            values={},
            quality_codes=(),
            source_timestamps=(stamp + timedelta(seconds=1),),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        FeatureRow(
            available_at=stamp.replace(tzinfo=None),
            instrument="NIFTY",
            session_date=stamp.date(),
            schema_version="1",
            values={},
            quality_codes=(),
        )


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "receipt",
        "quote",
        "untrusted_source",
        "untrusted_quote",
        "naive_quality",
        "missing_quality",
    ],
)
def test_unavailable_inputs_cannot_enter_state(fault):
    pipeline = FeaturePipeline()
    feed(pipeline, 0)
    chain = sample(15, volume=900)
    report = quality(chain)
    future = chain.received_at + timedelta(seconds=1)
    if fault in {"source", "receipt"}:
        chain = chain.model_copy(
            update={"source_timestamp" if fault == "source" else "received_at": future}
        )
    elif fault == "quote":
        chain = chain.model_copy(
            update={
                "quotes": tuple(
                    q.model_copy(update={"timestamp": future}) for q in chain.quotes
                )
            }
        )
    elif fault == "untrusted_source":
        chain = chain.model_copy(update={"source_time_authoritative": False})
    elif fault == "untrusted_quote":
        chain = chain.model_copy(
            update={
                "quotes": tuple(
                    q.model_copy(update={"timestamp_authoritative": False})
                    for q in chain.quotes
                )
            }
        )
    elif fault == "naive_quality":
        report = report.model_copy(update={"checked_at": future.replace(tzinfo=None)})
    else:
        report = None
    assert pipeline.on_snapshot(chain, report) is None
    assert pipeline.last_rejection_codes
    assert feed(pipeline, 30, volume=130).values["call_volume_increment"] == 30


def test_quality_availability_delays_rows_including_completed_bar():
    pipeline = FeaturePipeline()
    for second in [0, 15, 30, 45]:
        feed(pipeline, second, volume=100 + second)
    chain = sample(60, volume=160)
    checked = chain.received_at + timedelta(seconds=8)
    flow = pipeline.on_snapshot(chain, quality(chain, checked_at=checked))
    assert flow.available_at == checked
    completed = pipeline.drain_completed()
    assert len(completed) == 1 and completed[0].available_at == checked
    assert all(t <= checked for t in completed[0].source_timestamps)


def test_completed_rows_wait_for_close_and_do_not_include_next_minute_spot():
    pipeline = FeaturePipeline()
    for second, spot in [(0, 24300), (15, 24310), (30, 24290), (45, 24320)]:
        feed(pipeline, second, volume=100 + second, spot=spot)
        assert pipeline.drain_completed() == ()
    feed(pipeline, 60, volume=160, spot=25000)
    (row,) = pipeline.drain_completed()
    assert row.completed and row.row_kind == "COMPLETED_MINUTE"
    assert row.bar_end == aware("2026-08-26T09:16:00+05:30")
    assert row.values["spot_open"] == 24300
    assert row.values["spot_high"] == 24320
    assert row.values["spot_low"] == 24290
    assert row.values["spot_close"] == 24320
    assert row.values["call_volume_increment"] == 45
    assert pipeline.drain_completed() == ()


@pytest.mark.parametrize("seconds", [[0, 45, 60], [30, 45, 60], [0, 15, 30, 180]])
def test_sparse_minutes_never_become_model_ready(seconds):
    pipeline = FeaturePipeline()
    for second in seconds:
        feed(pipeline, second, volume=100 + second)
    assert pipeline.drain_completed() == ()


@pytest.mark.parametrize("fault", ["reset", "order", "missing_contract", "invalid"])
def test_invalid_observation_invalidates_bar_but_not_valid_baseline(fault):
    pipeline = FeaturePipeline()
    feed(pipeline, 0)
    feed(pipeline, 15, volume=115)
    bad = sample(10 if fault == "order" else 30, volume=1 if fault == "reset" else 130)
    if fault == "missing_contract":
        bad = bad.model_copy(update={"quotes": bad.quotes[:1]})
    assert pipeline.on_snapshot(bad, quality(bad, tradable=fault != "invalid")) is None
    assert feed(pipeline, 45, volume=145).values["call_volume_increment"] == 30
    feed(pipeline, 60, volume=160)
    assert pipeline.drain_completed() == ()


def test_state_is_isolated_by_instrument_expiry_session_and_config():
    pipeline = FeaturePipeline(
        configs={
            "NIFTY": InstrumentConfig(instrument="NIFTY", lot_size=1),
            "BANKNIFTY": InstrumentConfig(instrument="BANKNIFTY", lot_size=2),
        }
    )
    feed(pipeline, 0)
    feed(pipeline, 0, instrument="BANKNIFTY", volume=500)
    row = feed(pipeline, 15, volume=125)
    bank = feed(pipeline, 15, instrument="BANKNIFTY", volume=525)
    assert (
        bank.values["call_volume_increment"]
        == row.values["call_volume_increment"]
        == 25
    )
    assert bank.values["oi_dex_rupees"] == pytest.approx(
        row.values["oi_dex_rupees"] * 2
    )
    assert feed(pipeline, 0, day=27, volume=1) is None
    changed = sample(30, volume=140)
    changed = changed.model_copy(
        update={
            "expiry": date(2026, 9, 8),
            "quotes": tuple(
                q.model_copy(update={"expiry": date(2026, 9, 8)})
                for q in changed.quotes
            ),
        }
    )
    assert pipeline.on_snapshot(changed, quality(changed)) is None


def test_features_have_explicit_units_real_values_and_honest_missing_status():
    pipeline = FeaturePipeline()
    feed(pipeline, 0)
    row = feed(pipeline, 15, volume=125, spot=24310)
    # Binding 5m/acceleration correction expands the original approximate range.
    assert 60 < len(row.values) <= 80
    assert row.values["observed_session_return"] == pytest.approx(24310 / 24300 - 1)
    assert row.values["call_oi"] == 100 and row.values["put_oi"] == 100
    assert row.values["oi_concentration"] == 1
    assert row.values["atm_iv"] == 0.15 and row.values["atm_straddle"] == 200
    assert row.values["oi_gex_crore"] == pytest.approx(
        row.values["oi_gex_rupees"] / 1e7
    )
    assert row.values["flow_dex_crore"] == pytest.approx(
        row.values["flow_dex_rupees"] / 1e7
    )
    for field in ["vwap_distance", "vwap_slope", "futures_basis", "cross_index_return"]:
        assert row.values[field] is None
    for field in ["vwap_status", "futures_status", "cross_index_status"]:
        assert row.values[field] == "MISSING_INPUT"
    assert row.values["opening_range_status"] == "WARMUP"
    assert row.values["spot_return_1m"] is None


def test_realized_volatility_and_one_minute_return_use_contiguous_completed_bars():
    pipeline = FeaturePipeline()
    for second in range(0, 181, 15):
        feed(pipeline, second, volume=100 + second, spot=100 + second // 60)
    rows = pipeline.drain_completed()
    assert len(rows) == 3
    assert rows[0].values["spot_return_1m"] is None
    assert rows[1].values["spot_return_1m"] == pytest.approx(0.01)
    assert rows[1].values["realized_volatility"] is None
    assert rows[2].values["realized_volatility"] == pytest.approx(
        abs(math.log(101 / 100) - math.log(102 / 101)) / 2
    )


def test_opening_range_available_at_fifteenth_close_not_one_minute_later():
    pipeline = FeaturePipeline()
    for second in range(0, 901, 15):
        feed(pipeline, second, volume=100 + second, spot=24300 + second)
    rows = pipeline.drain_completed()
    assert rows[-2].values["opening_range_status"] == "WARMUP"
    assert rows[-1].values["opening_range_status"] == "VALID"
    assert rows[-1].values["opening_range_low"] == 24300
    assert rows[-1].values["opening_range_high"] == 25185


def test_price_history_does_not_bridge_unseen_minutes():
    pipeline = FeaturePipeline()
    for second in [0, 15, 30, 45, 60, 75, 90, 105, 120, 300, 315, 330, 345, 360]:
        feed(pipeline, second, volume=100 + second, spot=24300 + second)
    rows = pipeline.drain_completed()
    assert rows[-1].bar_start == aware("2026-08-26T09:20:00+05:30")
    assert rows[-1].values["spot_return_1m"] is None
    assert rows[-1].values["realized_volatility"] is None


def test_baseline_quality_delay_is_never_backdated_by_later_report():
    pipeline = FeaturePipeline()
    first = sample(0)
    pipeline.on_snapshot(
        first, quality(first, checked_at=first.received_at + timedelta(seconds=20))
    )
    second = sample(15, volume=125)
    assert pipeline.on_snapshot(second, quality(second)) is None
    assert pipeline.last_rejection_codes == ("OUT_OF_ORDER",)


def test_quote_time_does_not_define_counter_interval_for_bar_aggregation():
    pipeline = FeaturePipeline()
    for second in [0, 15, 30, 45, 60]:
        chain = sample(second, volume=100 + second)
        chain = chain.model_copy(
            update={
                "quotes": tuple(
                    q.model_copy(
                        update={"timestamp": q.timestamp - timedelta(seconds=10)}
                    )
                    for q in chain.quotes
                )
            }
        )
        pipeline.on_snapshot(chain, quality(chain))
    (row,) = pipeline.drain_completed()
    assert row.values["call_volume_increment"] == 45


def test_row_rejects_invalid_bar_boundaries_and_nonfinite_values():
    stamp = sample().received_at
    for start, end in [(stamp, stamp), (stamp, stamp - timedelta(minutes=1))]:
        with pytest.raises(ValueError, match="bar boundaries"):
            FeatureRow(
                available_at=stamp,
                instrument="NIFTY",
                session_date=stamp.date(),
                schema_version="1",
                values={},
                quality_codes=(),
                completed=True,
                row_kind="COMPLETED_MINUTE",
                bar_start=start,
                bar_end=end,
            )
    with pytest.raises(ValueError, match="finite scalar"):
        FeatureRow(
            available_at=stamp,
            instrument="NIFTY",
            session_date=stamp.date(),
            schema_version="1",
            values={"bad": float("nan")},
            quality_codes=(),
        )


@pytest.mark.parametrize("fault", ["stale", "duplicate", "observation", "invalid_iv"])
def test_bad_audits_and_contract_fields_fail_closed(fault):
    pipeline = FeaturePipeline()
    feed(pipeline, 0)
    chain = sample(15, volume=900)
    report = quality(chain)
    if fault == "stale":
        report = quality(chain, checked_at=chain.received_at + timedelta(seconds=31))
    elif fault == "observation":
        report = report.model_copy(
            update={"details": {"decision_scope": "observation"}}
        )
    elif fault == "duplicate":
        chain = chain.model_copy(update={"quotes": chain.quotes + (chain.quotes[0],)})
    else:
        chain = chain.model_copy(
            update={
                "quotes": tuple(
                    q.model_copy(update={"iv": float("nan")}) for q in chain.quotes
                )
            }
        )
    assert pipeline.on_snapshot(chain, report) is None
    assert pipeline.last_rejection_codes
    assert feed(pipeline, 30, volume=130).values["call_volume_increment"] == 30


def test_completed_option_changes_are_close_to_close_not_last_poll_changes():
    pipeline = FeaturePipeline()
    for second in range(0, 121, 15):
        chain = sample(second, volume=100 + second)
        chain = chain.model_copy(
            update={
                "quotes": tuple(
                    q.model_copy(update={"ltp": 100 + second}) for q in chain.quotes
                )
            }
        )
        pipeline.on_snapshot(chain, quality(chain))
    first, second = pipeline.drain_completed()
    assert first.values["atm_straddle_change"] is None
    assert second.values["atm_straddle_change"] == 120


def test_unobserved_opening_range_is_incomplete_after_warmup_window():
    pipeline = FeaturePipeline()
    for second in range(900, 961, 15):
        feed(pipeline, second, volume=100 + second)
    (row,) = pipeline.drain_completed()
    assert row.values["opening_range_status"] == "INCOMPLETE"
    assert row.values["opening_range_high"] is None


def test_entirely_unseen_minutes_reset_all_completed_history():
    pipeline = FeaturePipeline()
    for second in [0, 15, 30, 45, 300, 315, 330, 345, 360, 375, 390, 405, 420]:
        feed(pipeline, second, volume=100 + second, spot=24300 + second)
    rows = pipeline.drain_completed()
    assert len(rows) == 3
    assert rows[1].values["spot_return_1m"] is None
    assert rows[1].values["atm_straddle_change"] is None
    assert rows[2].values["realized_volatility"] is None


def variable_flow_rows() -> tuple[list, list]:
    """Produce completed bars with call flow 3, 6, ..., 21 contracts."""
    volumes = {
        0: 100,
        15: 101,
        30: 102,
        45: 103,
        60: 104,
        75: 106,
        90: 108,
        105: 110,
        120: 112,
        135: 115,
        150: 118,
        165: 121,
        180: 124,
        195: 128,
        210: 132,
        225: 136,
        240: 140,
        255: 145,
        270: 150,
        285: 155,
        300: 160,
        315: 166,
        330: 172,
        345: 178,
        360: 184,
        375: 191,
        390: 198,
        405: 205,
        420: 212,
    }
    pipeline = FeaturePipeline()
    flow = [
        feed(pipeline, second, volume=volume, spot=24300)
        for second, volume in volumes.items()
    ]
    return flow, list(pipeline.drain_completed())


def test_five_minute_flow_windows_use_exactly_five_completed_bars():
    _, rows = variable_flow_rows()
    assert len(rows) == 7
    for row in rows[:4]:
        assert row.values["flow_5m_status"] == "WARMUP"
        assert row.values["call_volume_increment_5m"] is None
        assert row.values["flow_gex_rupees_5m"] is None
    fifth, sixth = rows[4:6]
    assert fifth.values["flow_5m_status"] == "VALID"
    assert fifth.values["call_volume_increment_5m"] == 45
    assert fifth.values["put_volume_increment_5m"] == 90
    assert fifth.values["flow_gex_rupees_5m"] == pytest.approx(-16657085.717024812)
    assert fifth.values["flow_gex_crore_5m"] == pytest.approx(-1.6657085717024812)
    assert fifth.values["flow_dex_rupees_5m"] == pytest.approx(-40042322.10141834)
    assert fifth.values["flow_dex_crore_5m"] == pytest.approx(-4.004232210141834)
    assert sixth.values["call_volume_increment_5m"] == 60
    assert sixth.values["put_volume_increment_5m"] == 120


def test_acceleration_compares_only_prior_completed_windows():
    flow, rows = variable_flow_rows()
    assert all(
        row is None or row.values["flow_5m_status"] == "COMPLETED_BARS_ONLY"
        for row in flow
    )
    first, second, fifth, sixth = rows[0], rows[1], rows[4], rows[5]
    assert first.values["flow_acceleration_1m_status"] == "WARMUP"
    assert first.values["call_volume_acceleration_1m"] is None
    assert second.values["flow_acceleration_1m_status"] == "VALID"
    assert second.values["call_volume_acceleration_1m"] == 3
    assert second.values["put_volume_acceleration_1m"] == 6
    assert second.values["flow_gex_acceleration_1m_rupees"] == pytest.approx(
        -1110431.2866242242
    )
    assert second.values["flow_dex_acceleration_1m_rupees"] == pytest.approx(
        -2669485.76291964
    )
    assert fifth.values["flow_acceleration_5m_status"] == "WARMUP"
    assert sixth.values["flow_acceleration_5m_status"] == "VALID"
    assert sixth.values["call_volume_acceleration_5m"] == 15
    assert sixth.values["put_volume_acceleration_5m"] == 30
    assert sixth.values["flow_gex_acceleration_5m_rupees"] == pytest.approx(
        -5553388.9609455895
    )
    assert sixth.values["flow_dex_acceleration_5m_rupees"] == pytest.approx(
        -13347500.123912761
    )


def test_rejected_data_resets_five_minute_windows_without_mutating_counter_baseline():
    pipeline = FeaturePipeline()
    for second in range(0, 301, 15):
        feed(pipeline, second, volume=100 + second, spot=24300)
    before = pipeline.drain_completed()
    assert before[-1].values["flow_5m_status"] == "VALID"
    bad = sample(315, volume=999, spot=24300)
    assert pipeline.on_snapshot(bad, quality(bad, tradable=False)) is None
    # Increment still uses the last valid source counter, while rolling state is reset.
    row = feed(pipeline, 330, volume=445, spot=24300)
    assert row.values["call_volume_increment"] == 45
    assert row.values["call_volume_increment_5m"] is None
    for second in [345, 360, 375, 390, 405, 420]:
        feed(pipeline, second, volume=100 + second, spot=24300)
    after = pipeline.drain_completed()
    assert after[-1].values["flow_5m_status"] == "WARMUP"


def test_minutes_to_close_uses_available_time_and_fixed_ist_session_contract():
    pipeline = FeaturePipeline()
    first = sample(0)
    pipeline.on_snapshot(first, quality(first))
    current = sample(15, volume=125)
    row = pipeline.on_snapshot(
        current,
        quality(current, checked_at=current.received_at + timedelta(seconds=15)),
    )
    assert row.values["minutes_to_close"] == pytest.approx(374.5)
    assert (
        time_values(
            aware("2026-08-26T09:00:00+05:30"), aware("2026-08-26T09:00:00+05:30")
        )["session_time_status"]
        == "PREOPEN"
    )
    assert (
        time_values(
            aware("2026-08-26T15:30:00+05:30"), aware("2026-08-26T15:30:00+05:30")
        )["session_time_status"]
        == "POST_CLOSE"
    )
    # Current market-hours contract has no early-close override; 14:00 is regular.
    early = time_values(
        aware("2026-08-26T14:00:00+05:30"), aware("2026-08-26T14:00:00+05:30")
    )
    assert early == {
        "minutes_since_open": 285.0,
        "minutes_to_close": 90.0,
        "session_time_status": "REGULAR",
    }


def test_pipeline_rejects_preopen_and_postclose_before_time_features():
    pipeline = FeaturePipeline()
    for second in [-900, 22500]:
        chain = sample(second)
        assert pipeline.on_snapshot(chain, quality(chain)) is None
        assert pipeline.last_rejection_codes == ("OUTSIDE_SESSION",)


def test_time_features_reject_naive_source_or_availability():
    aware_time = aware("2026-08-26T10:00:00+05:30")
    with pytest.raises(ValueError, match="timezone-aware"):
        time_values(aware_time.replace(tzinfo=None), aware_time)
    with pytest.raises(ValueError, match="timezone-aware"):
        time_values(aware_time, aware_time.replace(tzinfo=None))


def test_observed_return_discloses_whether_true_session_open_was_seen():
    exact = FeaturePipeline()
    feed(exact, 0, spot=24300)
    assert (
        feed(exact, 15, volume=125, spot=24310).values["observed_session_return_status"]
        == "SESSION_OPEN_OBSERVED"
    )
    late = FeaturePipeline()
    feed(late, 15, spot=24310)
    row = feed(late, 30, volume=125, spot=24320)
    assert row.values["observed_session_return"] == pytest.approx(24320 / 24310 - 1)
    assert row.values["observed_session_return_status"] == "OBSERVED_WINDOW"


@pytest.mark.parametrize(("instrument", "day"), [("BANKNIFTY", 26), ("NIFTY", 27)])
def test_five_minute_state_is_isolated_by_instrument_and_session(instrument, day):
    pipeline = FeaturePipeline()
    for second in range(0, 301, 15):
        feed(pipeline, second, volume=100 + second, spot=24300)
    assert pipeline.drain_completed()[-1].values["flow_5m_status"] == "VALID"
    for second in range(0, 61, 15):
        feed(
            pipeline,
            second,
            volume=500 + second,
            spot=49000 if instrument == "BANKNIFTY" else 24300,
            instrument=instrument,
            day=day,
        )
    (isolated,) = pipeline.drain_completed()
    assert isolated.values["flow_5m_status"] == "WARMUP"
    assert isolated.values["flow_acceleration_1m_status"] == "WARMUP"
