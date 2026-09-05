"""Research-only triple-barrier outcome labels.

Labels describe a realized underlying-price path. They are deliberately not
signals, recommendations, or order instructions.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import pandas as pd  # type: ignore[import-untyped]

from nifty_signal_engine.config.market_hours import IST


class DirectionLabel(StrEnum):
    """Directional research targets, including outcomes excluded from fitting."""

    UP = "UP"
    DOWN = "DOWN"
    NO_MOVE = "NO_MOVE"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT_FUTURE_DATA = "INSUFFICIENT_FUTURE_DATA"


@dataclass(frozen=True)
class TripleBarrierOutcome:
    """A realized outcome after an origin, never an executable instruction."""

    label: DirectionLabel
    barrier_time: pd.Timestamp | None
    terminal_return: float | None
    mfe: float | None
    mae: float | None


def label_triple_barrier(
    prices: pd.Series | pd.DataFrame,
    origin: datetime | pd.Timestamp,
    horizon: float | timedelta,
    upper_return: float,
    lower_return: float,
) -> TripleBarrierOutcome:
    """Label the first known barrier hit in a timestamped price path.

    ``prices`` may be close-only ``Series`` or OHLC ``DataFrame`` with
    ``close``, ``high``, and ``low`` columns. OHLC data are used to detect a
    same-bar dual crossing; because their intrabar order is unknown, that path
    is labelled ``AMBIGUOUS`` rather than guessed.
    """
    frame, _ = _validated_prices(prices)
    duration = _duration(horizon)
    _validate_barriers(upper_return, lower_return)
    origin_time = _origin_in_index_timezone(origin, frame.index)
    if origin_time not in frame.index:
        raise ValueError("origin must be present in prices")

    end = origin_time + duration
    observed = frame.loc[origin_time:end]
    origin_price = float(observed.iloc[0].close)
    upper_price = origin_price * (1 + upper_return)
    lower_price = origin_price * (1 + lower_return)

    for timestamp, row in observed.iloc[1:].iterrows():
        crossed_up = float(row.high) >= upper_price
        crossed_down = float(row.low) <= lower_price
        terminal_return = float(row.close) / origin_price - 1
        if crossed_up and crossed_down:
            return _outcome(
                DirectionLabel.AMBIGUOUS,
                timestamp,
                terminal_return,
                observed.loc[:timestamp],
                origin_price,
            )
        if crossed_up:
            return _outcome(
                DirectionLabel.UP,
                timestamp,
                terminal_return,
                observed.loc[:timestamp],
                origin_price,
            )
        if crossed_down:
            return _outcome(
                DirectionLabel.DOWN,
                timestamp,
                terminal_return,
                observed.loc[:timestamp],
                origin_price,
            )

    if observed.index[-1] != end:
        return TripleBarrierOutcome(
            DirectionLabel.INSUFFICIENT_FUTURE_DATA, None, None, None, None
        )
    return _outcome(
        DirectionLabel.NO_MOVE,
        None,
        float(observed.iloc[-1].close) / origin_price - 1,
        observed,
        origin_price,
    )


def _validated_prices(prices: pd.Series | pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    if isinstance(prices, pd.Series):
        frame = prices.to_frame(name="close")
        has_ranges = False
    elif isinstance(prices, pd.DataFrame):
        if not {"close", "high", "low"}.issubset(prices.columns):
            raise ValueError("OHLC prices require close, high, and low columns")
        frame = prices.loc[:, ["close", "high", "low"]].copy()
        has_ranges = True
    else:
        raise TypeError("prices must be a pandas Series or OHLC DataFrame")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("prices must have timezone-aware timestamps")
    # Canonicalize once so origins, calculations, and returned timestamps all
    # share the engine-wide Asia/Kolkata contract while retaining each instant.
    frame.index = frame.index.tz_convert(IST)
    if frame.empty:
        raise ValueError("prices must not be empty")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("price timestamps must be strictly increasing")
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if not all(
        math.isfinite(float(value)) and float(value) > 0 for value in frame.to_numpy().flat
    ):
        raise ValueError("prices must be positive finite values")
    if has_ranges and (
        (frame.high < frame.low).any()
        or (frame.close > frame.high).any()
        or (frame.close < frame.low).any()
    ):
        raise ValueError("OHLC ranges must contain their close")
    if not has_ranges:
        frame["high"] = frame.close
        frame["low"] = frame.close
    return frame, has_ranges


def _duration(horizon: float | timedelta) -> timedelta:
    if isinstance(horizon, timedelta):
        duration = horizon
    elif isinstance(horizon, (int, float)) and math.isfinite(horizon):
        duration = timedelta(seconds=horizon)
    else:
        raise ValueError("horizon must be a finite number of seconds or timedelta")
    if duration <= timedelta(0):
        raise ValueError("horizon must be positive")
    return duration


def _validate_barriers(upper_return: float, lower_return: float) -> None:
    if not all(math.isfinite(value) for value in (upper_return, lower_return)):
        raise ValueError("barriers must be finite")
    if not 0 < upper_return or not -1 < lower_return < 0:
        raise ValueError("barriers must straddle zero and preserve positive prices")


def _origin_in_index_timezone(
    origin: datetime | pd.Timestamp, index: pd.DatetimeIndex
) -> pd.Timestamp:
    timestamp = pd.Timestamp(origin)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("origin must be timezone-aware")
    return timestamp.tz_convert(index.tz)


def _outcome(
    label: DirectionLabel,
    barrier_time: pd.Timestamp | None,
    terminal_return: float,
    path: pd.DataFrame,
    origin_price: float,
) -> TripleBarrierOutcome:
    return TripleBarrierOutcome(
        label,
        barrier_time,
        terminal_return,
        float(path.high.max()) / origin_price - 1,
        float(path.low.min()) / origin_price - 1,
    )
