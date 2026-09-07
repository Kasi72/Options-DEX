from datetime import timedelta

import pandas as pd
import pytest

from nifty_signal_engine.backtesting.metrics import direction_metrics
from nifty_signal_engine.backtesting.splits import PurgedWalkForward


def test_splits_are_chronological_and_embargoed() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    observations = pd.DataFrame(
        {"feature": range(240), "label": ["UP", "DOWN"] * 120}, index=index
    )
    fold = next(PurgedWalkForward(horizon="60min", embargo="60min").split(observations))
    assert fold.train.index.max() < fold.validation.index.min()
    assert fold.validation.index.max() < fold.calibration.index.min()
    assert fold.calibration.index.max() < fold.test.index.min()
    assert fold.test.index.min() - fold.calibration.index.max() >= pd.Timedelta("60min")
    assert fold.metadata["purged"] is True
    blocks = (fold.train, fold.validation, fold.calibration, fold.test)
    days = [{timestamp.date() for timestamp in block.index} for block in blocks]
    assert not any(days[left].intersection(days[right]) for left in range(4) for right in range(left + 1, 4))


def test_purge_removes_rows_whose_label_horizon_touches_next_block() -> None:
    index = pd.date_range("2026-01-01", periods=8 * 4, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame({"label": ["UP", "DOWN"] * 16}, index=index)
    fold = next(
        PurgedWalkForward(
            horizon=timedelta(minutes=5),
            embargo=timedelta(minutes=2),
            train_size=2,
            validation_size=2,
            calibration_size=2,
            test_size=2,
        ).split(frame)
    )
    assert fold.train.index.max() + pd.Timedelta(minutes=5) < fold.validation.index.min()


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame({"label": ["UP"]}),
        pd.DataFrame({"label": ["UP"]}, index=pd.DatetimeIndex(["2026-01-01"])),
    ],
)
def test_invalid_or_insufficient_indexes_fail_closed(frame: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        next(PurgedWalkForward(horizon="1h").split(frame))


def test_non_integer_fold_count_is_rejected() -> None:
    with pytest.raises(TypeError):
        PurgedWalkForward(horizon="1h", n_splits=1.0)  # type: ignore[arg-type]


def test_zero_embargo_keeps_trailing_session_out_of_next_block() -> None:
    index = pd.date_range("2026-01-01", periods=20, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame({"label": ["UP", "DOWN"] * 10}, index=index)
    fold = next(PurgedWalkForward(horizon="1h", embargo=0, train_size=4,
                                  validation_size=4, calibration_size=4,
                                  test_size=4).split(frame))
    assert len(fold.train) == 4
    assert fold.train.index[-1] < fold.validation.index[0]


def test_default_is_expanding_across_multiple_folds() -> None:
    index = pd.date_range("2026-01-01", periods=720, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame({"label": ["UP", "DOWN"] * 360}, index=index)
    folds = list(PurgedWalkForward(horizon="1h", n_splits=2,
                                   train_size=100, validation_size=30,
                                   calibration_size=30, test_size=30).split(frame))
    assert len(folds) == 2
    assert len(folds[1].train) > len(folds[0].train)
    assert folds[1].metadata["rolling"] is False


def test_unknown_outcome_is_not_silently_mapped_to_negative() -> None:
    with pytest.raises(ValueError):
        direction_metrics(["UP", "AMBIGUOUS"], [0.9, 0.1], direction="BUY")
