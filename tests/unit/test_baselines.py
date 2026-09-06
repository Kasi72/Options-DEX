from datetime import datetime

import pandas as pd
import pytest

from nifty_signal_engine.features.pipeline import FeatureRow
from nifty_signal_engine.signals.baselines import (
    DirectionProbabilities,
    LogisticBaseline,
    RuleBaseline,
)


def completed_row(
    *,
    instrument: str = "NIFTY",
    direction: int = 1,
    flow: float = 100.0,
) -> FeatureRow:
    end = datetime.fromisoformat("2026-08-26T10:01:00+05:30")
    return FeatureRow(
        available_at=end,
        instrument=instrument,
        session_date=end.date(),
        schema_version="2",
        values={
            "spot_open": 100.0,
            "spot_close": 100.0 + direction,
            "flow_dex_rupees": direction * flow,
            "oi_dex_rupees": direction * 1_000.0,
            "session_time_status": "REGULAR",
            "flow_5m_status": "WARMUP",
        },
        quality_codes=(),
        row_kind="COMPLETED_MINUTE",
        completed=True,
        bar_start=end - pd.Timedelta(minutes=1),
        bar_end=end,
    )


def training_frame(instrument: str = "NIFTY") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "instrument": [instrument] * 9,
            "momentum": [-3.0, -2.0, -1.0, -0.1, 0.0, 0.1, 1.0, 2.0, 3.0],
            "flow": [-3.0, -2.0, None, -0.1, None, 0.1, None, 2.0, 3.0],
        }
    )


def test_direction_probabilities_reject_invalid_distributions() -> None:
    for values in [
        {"up": 0.8, "down": 0.3, "no_move": -0.1},
        {"up": float("nan"), "down": 0.5, "no_move": 0.5},
        {"up": 0.6, "down": 0.3, "no_move": 0.2},
    ]:
        with pytest.raises(ValueError):
            DirectionProbabilities(**values)


@pytest.mark.parametrize(("direction", "winner"), [(1, "up"), (-1, "down")])
def test_rule_baseline_requires_confluence_and_is_uncalibrated(
    direction: int, winner: str
) -> None:
    prediction = RuleBaseline().predict(completed_row(direction=direction))
    assert getattr(prediction, winner) == 0.8
    assert prediction.instrument == "NIFTY"
    assert prediction.calibrated_up is None
    assert prediction.calibrated_down is None
    assert prediction.calibrated_no_move is None


def test_rule_baseline_abstains_on_disagreement() -> None:
    row = completed_row(direction=1, flow=-100.0)
    prediction = RuleBaseline().predict(row)
    assert prediction.no_move > prediction.up
    assert prediction.no_move > prediction.down


@pytest.mark.parametrize(
    "fault", ["flow", "session", "exposure", "incomplete", "quality"]
)
def test_rule_baseline_rejects_unusable_feature_rows(fault: str) -> None:
    row = completed_row()
    values = dict(row.values)
    updates: dict[str, object] = {}
    if fault == "flow":
        values["flow_dex_rupees"] = None
    elif fault == "session":
        values["session_time_status"] = "POST_CLOSE"
    elif fault == "exposure":
        values["oi_dex_rupees"] = None
    elif fault == "incomplete":
        updates.update(completed=False, row_kind="FLOW", bar_start=None, bar_end=None)
    else:
        updates["quality_codes"] = ("STALE_SOURCE",)
    row = row.__class__(**{**row.__dict__, "values": values, **updates})
    with pytest.raises(ValueError):
        RuleBaseline().predict(row)


def test_logistic_baseline_imputes_from_training_data_and_is_deterministic() -> None:
    frame = training_frame()
    labels = ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3
    model = LogisticBaseline(instrument="NIFTY").fit(frame, labels)
    assert model.training_medians == {"momentum": 0.0, "flow": 0.0}
    row = {"instrument": "NIFTY", "momentum": 2.5, "flow": None}
    first = model.predict_proba(row)
    second = model.predict_proba(row)
    assert first == second
    assert first.up > first.down
    assert first.calibrated_up is None


def test_logistic_baseline_accepts_a_feature_only_row_from_its_training_frame() -> None:
    frame = training_frame().drop(columns="instrument")
    model = LogisticBaseline(instrument="NIFTY").fit(
        frame, ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3
    )
    result = model.predict_proba(frame.iloc[-1])
    assert result.instrument == "NIFTY"
    assert result.up > result.down


def test_logistic_baseline_exposes_auditable_fitted_parameters() -> None:
    model = LogisticBaseline(instrument="NIFTY").fit(
        training_frame(), ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3
    )
    assert model.feature_names == ("momentum", "flow")
    assert set(model.coefficients) == {"DOWN", "NO_MOVE", "UP"}
    assert all(len(weights) == 2 for weights in model.coefficients.values())
    assert set(model.intercepts) == {"DOWN", "NO_MOVE", "UP"}


def test_logistic_scaler_is_train_fitted_and_stable_across_feature_units() -> None:
    labels = ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3
    ordinary = training_frame()
    heterogeneous = ordinary.assign(flow=ordinary["flow"] * 1_000_000_000)
    ordinary_model = LogisticBaseline(instrument="NIFTY").fit(ordinary, labels)
    heterogeneous_model = LogisticBaseline(instrument="NIFTY").fit(
        heterogeneous, labels
    )

    ordinary_result = ordinary_model.predict_proba(
        {"instrument": "NIFTY", "momentum": 2.5, "flow": 1.5}
    )
    heterogeneous_result = heterogeneous_model.predict_proba(
        {"instrument": "NIFTY", "momentum": 2.5, "flow": 1_500_000_000}
    )

    assert heterogeneous_result.up == pytest.approx(ordinary_result.up, abs=1e-12)
    assert ordinary_model.training_scales == pytest.approx(
        {"momentum": 1.764463405, "flow": 1.700326766}
    )
    ordinary.loc[:, "momentum"] = 1_000_000
    assert ordinary_model.training_scales == pytest.approx(
        {"momentum": 1.764463405, "flow": 1.700326766}
    )


def test_logistic_prediction_carries_canonical_training_and_feature_provenance() -> None:
    frame = training_frame().drop(columns="instrument")
    frame.index = pd.date_range("2026-06-01", periods=len(frame), freq="D", tz="UTC")
    model = LogisticBaseline(
        instrument="NIFTY",
        model_version="logistic-v1",
        horizon_minutes=15,
        training_window_id="train-2026-q2",
    ).fit(frame, ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3)
    base = completed_row()
    row = base.__class__(
        **{
            **base.__dict__,
            "values": {**base.values, "momentum": 2.5, "flow": 1.5},
        }
    )

    result = model.predict_proba(row)

    assert result.feature_available_at == row.available_at
    assert result.feature_schema_version == "2"
    assert result.model_version == "logistic-v1"
    assert result.horizon_minutes == 15
    assert result.training_window_id == "train-2026-q2"
    assert str(result.training_window_start.tzinfo) == "Asia/Kolkata"
    assert str(result.training_window_end.tzinfo) == "Asia/Kolkata"


def test_logistic_baseline_keeps_instrument_models_separate() -> None:
    model = LogisticBaseline(instrument="NIFTY").fit(
        training_frame(), ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3
    )
    with pytest.raises(ValueError, match="instrument"):
        model.predict_proba(
            {"instrument": "BANKNIFTY", "momentum": 1.0, "flow": 1.0}
        )
    with pytest.raises(ValueError, match="single configured instrument"):
        LogisticBaseline(instrument="NIFTY").fit(
            training_frame("BANKNIFTY"),
            ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3,
        )


@pytest.mark.parametrize(
    ("frame", "labels", "message"),
    [
        (pd.DataFrame(), [], "not be empty"),
        (training_frame(), ["UP"], "same length"),
        (training_frame(), ["UP"] * 9, "three classes"),
        (
            training_frame().assign(momentum=float("inf")),
            ["DOWN"] * 3 + ["NO_MOVE"] * 3 + ["UP"] * 3,
            "infinite",
        ),
    ],
)
def test_logistic_baseline_rejects_invalid_training_inputs(
    frame: pd.DataFrame, labels: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LogisticBaseline(instrument="NIFTY").fit(frame, labels)
