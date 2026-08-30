import csv
import math
from pathlib import Path

import pytest

from nifty_signal_engine.calculations.rolling import rolling_zscore


def test_zscore_is_unavailable_before_minimum_history() -> None:
    assert rolling_zscore(10.0, [1.0] * 19, minimum=20, window=20) is None


def test_zscore_uses_only_the_latest_window_of_prior_history() -> None:
    result = rolling_zscore(5.0, [100.0, 1.0, 2.0, 3.0], minimum=3, window=3)
    assert result == (5.0 - 2.0) / math.sqrt(2 / 3)


def test_zscore_is_unavailable_when_prior_history_has_no_variation() -> None:
    assert rolling_zscore(10.0, [1.0] * 20, minimum=20, window=20) is None


def test_prototype_first_row_zscores_are_unavailable_without_prior_session_history() -> None:
    root = Path(__file__).parents[2]
    with (root / "reference" / "nifty_vol_gex_dex_log.csv").open(newline="") as stream:
        first = next(row for row in csv.DictReader(stream) if row["Timestamp"])
    assert rolling_zscore(float(first["Vol_GEX_ZScore"]), []) is None
    assert rolling_zscore(float(first["Vol_DEX_ZScore"]), []) is None
    assert float(first["Net_Vol_GEX_Cr"]) == float(first["Call_Vol_GEX_Cr"]) + float(
        first["Put_Vol_GEX_Cr"]
    )
    assert float(first["Vol_GD"]) == pytest.approx(float(first["Spot_Price"]) - float(
        first["Vol_Zero_Gamma_Level"]
    ))
    assert float(first["Vol_DD"]) == pytest.approx(float(first["Spot_Price"]) - float(
        first["Vol_Zero_Delta_Level"]
    ))
