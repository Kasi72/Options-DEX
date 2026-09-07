"""Chronological, purged walk-forward data splits.

The splitter is deliberately small and framework independent.  It accepts a
timestamp-indexed frame and never shuffles or sorts caller data; an invalid
index fails closed instead of silently changing the chronology of an audit.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Final

import pandas as pd  # type: ignore[import-untyped]

from nifty_signal_engine.config.market_hours import IST


def _duration(value: str | float | timedelta | pd.Timedelta, name: str) -> pd.Timedelta:
    try:
        result = pd.Timedelta(value, unit="s") if isinstance(value, (int, float)) else pd.Timedelta(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive duration") from exc
    if result <= pd.Timedelta(0) or pd.isna(result):
        raise ValueError(f"{name} must be a positive duration")
    return result


@dataclass(frozen=True, slots=True)
class Fold:
    """One immutable description of a chronological evaluation fold."""

    fold_id: int
    train: pd.DataFrame
    validation: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame
    horizon: pd.Timedelta
    embargo: pd.Timedelta
    embargo_windows: tuple[tuple[pd.Timestamp, pd.Timestamp], ...] = ()
    metadata: MappingProxyType[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def train_end(self) -> pd.Timestamp:
        return self.train.index[-1]

    @property
    def test_start(self) -> pd.Timestamp:
        return self.test.index[0]

    @property
    def embargo_start(self) -> pd.Timestamp | None:
        return self.embargo_windows[0][0] if self.embargo_windows else None

    @property
    def embargo_end(self) -> pd.Timestamp | None:
        return self.embargo_windows[-1][1] if self.embargo_windows else None

    def __post_init__(self) -> None:
        for name, frame in (
            ("train", self.train),
            ("validation", self.validation),
            ("calibration", self.calibration),
            ("test", self.test),
        ):
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError(f"{name} block must not be empty")
        if self.horizon <= pd.Timedelta(0) or self.embargo < pd.Timedelta(0):
            raise ValueError("horizon and embargo have invalid durations")
        indexes = [frame.index for frame in (self.train, self.validation, self.calibration, self.test)]
        if any(not isinstance(index, pd.DatetimeIndex) for index in indexes):
            raise ValueError("fold blocks require DatetimeIndex values")
        if any(index.tz is None for index in indexes):
            raise ValueError("fold timestamps must be timezone-aware")
        if any(str(index.tz) != str(IST) for index in indexes):
            raise ValueError("fold timestamps must use Asia/Kolkata")
        if any(not index.is_monotonic_increasing or index.has_duplicates for index in indexes):
            raise ValueError("fold blocks must be strictly chronological")
        if not (
            self.train.index[-1] < self.validation.index[0]
            and self.validation.index[-1] < self.calibration.index[0]
            and self.calibration.index[-1] < self.test.index[0]
        ):
            raise ValueError("fold blocks must be non-overlapping and chronological")
        days = [set(index.date) for index in indexes]
        if any(days[left].intersection(days[right]) for left in range(4) for right in range(left + 1, 4)):
            raise ValueError("fold blocks must not split an IST trading day")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


_DEFAULT_TRAIN: Final[float] = 0.50
_DEFAULT_VALIDATION: Final[float] = 0.15
_DEFAULT_CALIBRATION: Final[float] = 0.15
_DEFAULT_TEST: Final[float] = 0.20


class PurgedWalkForward:
    """Generate expanding chronological folds with horizon purging and embargo.

    Block sizes count complete IST sessions (calendar trading days), so an
    intraday frame is never cut in the middle of a day.  Set ``rolling=True``
    for fixed-width rolling training windows; the default is expanding training
    from the first available session.
    """

    def __init__(
        self,
        *,
        horizon: str | float | timedelta | pd.Timedelta,
        embargo: str | float | timedelta | pd.Timedelta = "0s",
        train_size: float = _DEFAULT_TRAIN,
        validation_size: float = _DEFAULT_VALIDATION,
        calibration_size: float = _DEFAULT_CALIBRATION,
        test_size: float = _DEFAULT_TEST,
        n_splits: int = 1,
        step_size: int | None = None,
        rolling: bool = False,
    ) -> None:
        self.horizon = _duration(horizon, "horizon")
        self.embargo = pd.Timedelta(0) if embargo == 0 or embargo == "0s" else _duration(embargo, "embargo")
        self.sizes = (train_size, validation_size, calibration_size, test_size)
        if any(isinstance(size, bool) or not isinstance(size, (int, float)) or size <= 0 for size in self.sizes):
            raise ValueError("block sizes must be positive numbers")
        if any(isinstance(size, float) and size > 1 for size in self.sizes):
            raise ValueError("fractional block sizes must be in (0, 1]")
        if not isinstance(n_splits, int) or isinstance(n_splits, bool):
            raise TypeError("n_splits must be an integer")
        if n_splits <= 0:
            raise ValueError("n_splits must be positive")
        if step_size is not None and (isinstance(step_size, bool) or not isinstance(step_size, int) or step_size <= 0):
            raise ValueError("step_size must be positive")
        self.n_splits = n_splits
        self.step_size = step_size
        if not isinstance(rolling, bool):
            raise TypeError("rolling must be boolean")
        self.rolling = rolling

    def split(self, frame: pd.DataFrame) -> Iterator[Fold]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError("frame must be a non-empty DataFrame")
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError("frame must have a timezone-aware DatetimeIndex")
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError("frame timestamps must be strictly chronological")
        # Canonicalize the index for metadata while preserving the row order and
        # frame values.  All engine timestamps use IST.
        source = frame.copy()
        source.index = source.index.tz_convert(IST)
        sessions = self._sessions(source)
        counts = self._counts(len(sessions))
        base = 0
        yielded = 0
        while yielded < self.n_splits:
            blocks = self._window(source, sessions, base, counts)
            if blocks is None:
                raise ValueError(
                    "frame does not contain enough complete IST sessions for the requested folds"
                )
            train, validation, calibration, test, windows = blocks
            yield Fold(
                fold_id=yielded,
                train=train,
                validation=validation,
                calibration=calibration,
                test=test,
                horizon=self.horizon,
                embargo=self.embargo,
                embargo_windows=windows,
                metadata=MappingProxyType(
                    {
                        "horizon": str(self.horizon),
                        "embargo": str(self.embargo),
                        "purged": True,
                        "rolling": self.rolling,
                        "instrument": self._instrument(source),
                    }
                ),
            )
            yielded += 1
            base += self.step_size if self.step_size is not None else counts[3]

    def _counts(self, length: int) -> tuple[int, int, int, int]:
        result: list[int] = []
        for size in self.sizes:
            count = int(size) if isinstance(size, int) else max(1, int(length * float(size)))
            result.append(count)
        return tuple(result)  # type: ignore[return-value]

    def _window(
        self, source: pd.DataFrame, sessions: list[tuple[object, int, int]], base: int, counts: tuple[int, int, int, int]
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, tuple[tuple[pd.Timestamp, pd.Timestamp], ...]] | None:
        train_n, validation_n, calibration_n, test_n = counts
        train_start_session = base if self.rolling else 0
        if train_start_session + train_n > len(sessions):
            return None
        train_end_session = train_start_session + train_n - 1 + (0 if self.rolling else base)
        if train_end_session >= len(sessions):
            return None
        train_end_position = sessions[train_end_session][2]
        validation_start = self._after(source.index[train_end_position], self.embargo)
        validation_positions = self._session_positions_after(source, sessions, validation_start, validation_n)
        if validation_positions is None:
            return None
        validation_start_session, validation_end_session = validation_positions
        validation_start_pos, validation_end_pos = sessions[validation_start_session][1], sessions[validation_end_session][2]
        calibration_start = self._after(source.index[validation_end_pos], self.embargo)
        calibration_positions = self._session_positions_after(source, sessions, calibration_start, calibration_n)
        if calibration_positions is None:
            return None
        calibration_start_session, calibration_end_session = calibration_positions
        calibration_start_pos, calibration_end_pos = sessions[calibration_start_session][1], sessions[calibration_end_session][2]
        test_start = self._after(source.index[calibration_end_pos], self.embargo)
        test_positions = self._session_positions_after(source, sessions, test_start, test_n)
        if test_positions is None:
            return None
        test_start_session, test_end_session = test_positions
        test_start_pos, test_end_pos = sessions[test_start_session][1], sessions[test_end_session][2]
        train = source.iloc[sessions[train_start_session][1] : train_end_position + 1]
        validation = source.iloc[validation_start_pos : validation_end_pos + 1]
        calibration = source.iloc[calibration_start_pos : calibration_end_pos + 1]
        test = source.iloc[test_start_pos : test_end_pos + 1]
        # A label at t can consume prices through t+horizon.  Remove any row
        # whose label window would touch a later block; this is the purge.
        train = self._purge(train, validation.index[0])
        validation = self._purge(validation, calibration.index[0])
        calibration = self._purge(calibration, test.index[0])
        if any(block.empty for block in (train, validation, calibration, test)):
            return None
        windows = self._windows(source, train_end_position, validation_start_pos, validation_end_pos, calibration_start_pos, calibration_end_pos, test_start_pos)
        return train, validation, calibration, test, windows

    def _purge(self, frame: pd.DataFrame, next_start: pd.Timestamp) -> pd.DataFrame:
        dates = frame.index.tz_convert(IST).date
        keep_dates = {
            day
            for day in dict.fromkeys(dates)
            if frame.loc[dates == day].index[-1] + self.horizon < next_start
        }
        return frame.loc[[day in keep_dates for day in dates]]

    @staticmethod
    def _after(timestamp: pd.Timestamp, duration: pd.Timedelta) -> pd.Timestamp:
        return timestamp + duration

    @staticmethod
    def _session_positions_after(source: pd.DataFrame, sessions: list[tuple[object, int, int]], start: pd.Timestamp, count: int) -> tuple[int, int] | None:
        starts = source.index[[session[1] for session in sessions]]
        # Right-sided lookup is essential at an exact session boundary: with
        # zero embargo and one row per session, ``left`` would select the
        # trailing train session again and create overlap/shrinkage.
        first = int(starts.searchsorted(start, side="right"))
        end = first + count - 1
        if first >= len(sessions) or end >= len(sessions):
            return None
        return first, end

    def _windows(self, source: pd.DataFrame, train_end: int, validation_start: int, validation_end: int, calibration_start: int, calibration_end: int, test_start: int) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
        boundaries = (train_end, validation_start, validation_end, calibration_start, calibration_end, test_start)
        windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        for left, right in ((boundaries[0], boundaries[1]), (boundaries[2], boundaries[3]), (boundaries[4], boundaries[5])):
            if right > left:
                windows.append((source.index[left], source.index[right]))
        return tuple(windows)

    @staticmethod
    def _sessions(source: pd.DataFrame) -> list[tuple[object, int, int]]:
        """Return complete IST calendar-day groups in source order."""
        days = source.index.tz_convert(IST).date
        sessions: list[tuple[object, int, int]] = []
        start = 0
        for position in range(1, len(source) + 1):
            if position == len(source) or days[position] != days[start]:
                sessions.append((days[start], start, position - 1))
                start = position
        return sessions

    @staticmethod
    def _instrument(frame: pd.DataFrame) -> str | None:
        if "instrument" not in frame:
            return None
        values = frame["instrument"].dropna().unique()
        return str(values[0]) if len(values) == 1 else None
