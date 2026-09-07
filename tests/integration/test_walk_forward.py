import pandas as pd
import pytest

from nifty_signal_engine.backtesting.reporting import (
    PromotionArtifact,
    PromotionPolicy,
    PromotionRegistry,
    build_promotion_artifact,
)
from nifty_signal_engine.backtesting.splits import PurgedWalkForward
from nifty_signal_engine.backtesting.walk_forward import evaluate_fold


class DeterministicModel:
    model_version = "test-model-v1"

    def fit(self, frame: pd.DataFrame, labels: object) -> "DeterministicModel":
        return self

    def predict_proba(self, frame: pd.DataFrame) -> list[list[float]]:
        values = frame["feature"].to_numpy(dtype=float)
        low, high = float(values.min()), float(values.max())
        p_up = (values - low + 1) / (high - low + 2)
        return [[1 - float(value), 0.0, float(value)] for value in p_up]


class OneArgumentUnfittedFactoryModel(DeterministicModel):
    fit_calls = 0

    def fit(self, frame: pd.DataFrame, labels: object) -> "OneArgumentUnfittedFactoryModel":
        self.fit_calls += 1
        return self


class NoFitModel:
    def predict_proba(self, frame: pd.DataFrame) -> list[list[float]]:
        return [[0.5, 0.0, 0.5] for _ in range(len(frame))]


def test_walk_forward_calibrates_and_reports_both_directions_without_test_fit() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "feature": range(240),
            "label": ["UP", "DOWN"] * 120,
            "instrument": ["NIFTY"] * 240,
            "return": [1.0] * 240,
        },
        index=index,
    )
    fold = next(PurgedWalkForward(horizon="1h", embargo="1h").split(frame))
    report = evaluate_fold(fold, DeterministicModel)
    assert set(report.directions) == {"BUY", "SELL"}
    assert report.buy is not None and report.sell is not None
    assert report.buy.calibration is not None
    assert {candidate.method for candidate in report.buy.calibration_candidates} == {
        "sigmoid",
        "isotonic",
        "temperature",
    }
    assert report.buy.headline_suppressed
    assert report.promotion_artifacts["BUY"].verified is False
    assert not PromotionRegistry().is_promoted("NIFTY", 60, "BUY")


def test_registry_does_not_accept_caller_forged_verified_flag() -> None:
    forged = PromotionArtifact(
        artifact_id="forged",
        instrument="NIFTY",
        horizon_minutes=60,
        direction="BUY",
        model_version="m",
        calibration_id="c",
        validation_report_id="v",
        verified=True,
    )
    registry = PromotionRegistry()
    registry.register(forged)
    assert registry.resolve("NIFTY", 60, "BUY") is None


def test_one_argument_factory_is_always_fitted_on_train() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"feature": range(240), "label": ["UP", "DOWN"] * 120,
         "instrument": ["NIFTY"] * 240}, index=index
    )
    fold = next(PurgedWalkForward(horizon="1h", embargo="1h").split(frame))
    created: list[OneArgumentUnfittedFactoryModel] = []

    def factory(train: pd.DataFrame) -> OneArgumentUnfittedFactoryModel:
        model = OneArgumentUnfittedFactoryModel()
        created.append(model)
        return model

    evaluate_fold(fold, factory)
    assert len(created) == 1
    assert created[0].fit_calls == 1


def test_evaluation_rejects_missing_instrument_or_mixed_label_columns() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"feature": range(240), "label": ["UP", "DOWN"] * 120,
         "instrument": ["NIFTY"] * 240}, index=index
    )
    fold = next(PurgedWalkForward(horizon="1h", embargo="1h").split(frame))
    missing_instrument = fold.validation.drop(columns="instrument")
    with pytest.raises(ValueError, match="instrument"):
        evaluate_fold(fold.__class__(fold.fold_id, fold.train, missing_instrument,
                                    fold.calibration, fold.test, fold.horizon,
                                    fold.embargo, fold.embargo_windows, fold.metadata),
                      DeterministicModel)
    mixed_labels = fold.validation.rename(columns={"label": "target"})
    with pytest.raises(ValueError, match="label column"):
        evaluate_fold(fold.__class__(fold.fold_id, fold.train, mixed_labels,
                                    fold.calibration, fold.test, fold.horizon,
                                    fold.embargo, fold.embargo_windows, fold.metadata),
                      DeterministicModel)


def test_evaluation_rejects_factory_without_fit() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"feature": range(240), "label": ["UP", "DOWN"] * 120,
         "instrument": ["NIFTY"] * 240}, index=index
    )
    fold = next(PurgedWalkForward(horizon="1h", embargo="1h").split(frame))
    with pytest.raises(TypeError, match="callable fit"):
        evaluate_fold(fold, NoFitModel)


def test_registry_accepts_only_derived_verified_evidence() -> None:
    index = pd.date_range("2026-01-01", periods=240, freq="1D", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"feature": range(240), "label": ["UP", "DOWN"] * 120,
         "instrument": ["NIFTY"] * 240, "return": [1.0] * 240},
        index=index,
    )
    fold = next(PurgedWalkForward(horizon="1h", embargo="1h").split(frame))
    report = evaluate_fold(fold, DeterministicModel, governance_minimum=1,
                           policy=PromotionPolicy(minimum_samples=1, minimum_profit_factor=1.0))
    assert report.buy is not None
    artifact = build_promotion_artifact(
        report,
        report.buy,
        policy=PromotionPolicy(minimum_samples=1, minimum_profit_factor=1.0),
    )
    registry = PromotionRegistry()
    registry.register(artifact)
    assert registry.resolve("NIFTY", 60, "BUY") == artifact


def test_builder_rejects_fabricated_report_or_metrics() -> None:
    with pytest.raises(ValueError):
        build_promotion_artifact(None, object())  # type: ignore[arg-type]
