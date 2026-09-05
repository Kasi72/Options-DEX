from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]
import pytest

from nifty_signal_engine.signals.labels import DirectionLabel, label_triple_barrier


def test_first_barrier_hit_wins() -> None:
    index = pd.to_datetime(
        [
            "2026-08-26T10:00:00+05:30",
            "2026-08-26T10:01:00+05:30",
            "2026-08-26T10:02:00+05:30",
        ]
    )
    prices = pd.Series([100.0, 101.1, 98.5], index=index)

    result = label_triple_barrier(prices, prices.index[0], 180, 0.01, -0.01)

    assert result.label is DirectionLabel.UP
    assert result.barrier_time == prices.index[1]
    assert result.terminal_return == pytest.approx(0.011)
    assert result.mfe == pytest.approx(0.011)
    assert result.mae == 0.0


def test_same_ohlc_bar_crossing_both_barriers_is_ambiguous() -> None:
    index = pd.to_datetime(
        [
            "2026-08-26T10:00:00+05:30",
            "2026-08-26T10:01:00+05:30",
            "2026-08-26T10:02:00+05:30",
        ]
    )
    prices = pd.DataFrame(
        {
            "close": [100.0, 100.5, 100.0],
            "high": [100.0, 101.5, 100.0],
            "low": [100.0, 98.5, 100.0],
        },
        index=index,
    )

    result = label_triple_barrier(prices, index[0], 120, 0.01, -0.01)

    assert result.label is DirectionLabel.AMBIGUOUS
    assert result.barrier_time == index[1]
    assert result.terminal_return == pytest.approx(0.005)
    assert result.mfe == pytest.approx(0.015)
    assert result.mae == pytest.approx(-0.015)


def test_no_barrier_at_the_vertical_horizon_is_no_move() -> None:
    index = pd.date_range("2026-08-26T10:00:00+05:30", periods=3, freq="min")
    prices = pd.Series([100.0, 100.2, 99.9], index=index)

    result = label_triple_barrier(prices, index[0], 120, 0.01, -0.01)

    assert result.label is DirectionLabel.NO_MOVE
    assert result.barrier_time is None
    assert result.terminal_return == pytest.approx(-0.001)
    assert result.mfe == pytest.approx(0.002)
    assert result.mae == pytest.approx(-0.001)


def test_missing_exact_vertical_horizon_is_insufficient_future_data() -> None:
    index = pd.date_range("2026-08-26T10:00:00+05:30", periods=2, freq="min")
    prices = pd.Series([100.0, 100.2], index=index)

    result = label_triple_barrier(prices, index[0], 120, 0.01, -0.01)

    assert result.label is DirectionLabel.INSUFFICIENT_FUTURE_DATA
    assert result.barrier_time is None
    assert result.terminal_return is None
    assert result.mfe is None
    assert result.mae is None


def test_prices_after_horizon_cannot_affect_the_label_or_excursions() -> None:
    index = pd.date_range("2026-08-26T10:00:00+05:30", periods=4, freq="min")
    prices = pd.Series([100.0, 100.2, 99.9, 105.0], index=index)

    result = label_triple_barrier(prices, index[0], 120, 0.01, -0.01)

    assert result.label is DirectionLabel.NO_MOVE
    assert result.terminal_return == pytest.approx(-0.001)
    assert result.mfe == pytest.approx(0.002)
    assert result.mae == pytest.approx(-0.001)


@pytest.mark.parametrize(
    ("index", "values"),
    [
        (pd.to_datetime(["2026-08-26T10:01:00+05:30", "2026-08-26T10:00:00+05:30"]), [100.0, 100.0]),
        (pd.to_datetime(["2026-08-26T10:00:00+05:30", "2026-08-26T10:00:00+05:30"]), [100.0, 100.0]),
        (pd.date_range("2026-08-26T10:00:00+05:30", periods=2, freq="min"), [100.0, float("nan")]),
        (pd.date_range("2026-08-26T10:00:00+05:30", periods=2, freq="min"), [100.0, float("inf")]),
    ],
)
def test_rejects_malformed_price_paths(
    index: pd.DatetimeIndex, values: list[float]
) -> None:
    prices = pd.Series(values, index=index)

    with pytest.raises(ValueError):
        label_triple_barrier(prices, index[0], 60, 0.01, -0.01)


def test_rejects_a_naive_origin() -> None:
    index = pd.date_range("2026-08-26T10:00:00+05:30", periods=2, freq="min")
    prices = pd.Series([100.0, 100.0], index=index)

    with pytest.raises(ValueError, match="timezone-aware"):
        label_triple_barrier(prices, pd.Timestamp("2026-08-26T10:00:00"), 60, 0.01, -0.01)


def test_rejects_naive_price_timestamps() -> None:
    index = pd.date_range("2026-08-26T10:00:00", periods=2, freq="min")
    prices = pd.Series([100.0, 100.0], index=index)

    with pytest.raises(ValueError, match="timezone-aware"):
        label_triple_barrier(
            prices,
            pd.Timestamp("2026-08-26T10:00:00+05:30"),
            60,
            0.01,
            -0.01,
        )


def test_aware_utc_path_is_normalized_to_asia_kolkata_outcome_timestamps() -> None:
    index = pd.to_datetime(
        ["2026-08-26T04:30:00Z", "2026-08-26T04:31:00Z", "2026-08-26T04:32:00Z"]
    )
    prices = pd.Series([100.0, 101.1, 100.0], index=index)

    result = label_triple_barrier(
        prices,
        pd.Timestamp("2026-08-26T10:00:00+05:30"),
        120,
        0.01,
        -0.01,
    )

    assert result.label is DirectionLabel.UP
    assert result.barrier_time == pd.Timestamp("2026-08-26T10:01:00+05:30")
    assert str(result.barrier_time.tz) == "Asia/Kolkata"
