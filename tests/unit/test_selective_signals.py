from datetime import datetime, timedelta

import pytest

from nifty_signal_engine.domain.signal import SignalAction
from nifty_signal_engine.features.pipeline import FeatureRow
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)
from nifty_signal_engine.signals.baselines import DirectionProbabilities
from nifty_signal_engine.signals.selective import (
    EconomicsAssessment,
    SelectiveDecision,
)
from nifty_signal_engine.signals.service import allowed_actions


def quality(*, tradable: bool = True) -> DataQualityReport:
    return DataQualityReport(
        tradable=tradable,
        codes=() if tradable else (DataQualityCode.STALE_SOURCE,),
        checked_at=datetime.fromisoformat("2026-08-26T10:00:00+05:30"),
        details={},
    )


def prediction(**updates: object) -> DirectionProbabilities:
    values: dict[str, object] = {
        "instrument": "NIFTY",
        "up": 0.9,
        "down": 0.05,
        "no_move": 0.05,
        "calibrated_up": None,
        "calibrated_down": None,
        "calibrated_no_move": None,
    }
    values.update(updates)
    return DirectionProbabilities(**values)


def provenance(stamp: datetime) -> dict[str, object]:
    return {
        "feature_available_at": stamp,
        "model_version": "logistic-v1",
        "feature_schema_version": "2",
        "horizon_minutes": 15,
        "training_window_start": stamp - timedelta(days=60),
        "training_window_end": stamp - timedelta(days=10),
        "training_window_id": "train-2026-q2",
        "calibration_id": "calibration-2026-08",
        "calibration_completed_at": stamp - timedelta(days=5),
        "validation_report_id": "walk-forward-2026-08",
    }


def decision_provenance(stamp: datetime) -> dict[str, object]:
    return {
        "model_version": "logistic-v1",
        "feature_schema_version": "2",
        "horizon_minutes": 15,
        "training_window_id": "train-2026-q2",
        "calibration_id": "calibration-2026-08",
        "validation_report_id": "walk-forward-2026-08",
        "validation_completed_at": stamp - timedelta(days=1),
    }


def feature_row(
    *, instrument: str = "NIFTY", stamp: datetime | None = None
) -> FeatureRow:
    available = stamp or datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    return FeatureRow(
        available_at=available,
        instrument=instrument,
        session_date=available.date(),
        schema_version="2",
        values={},
        quality_codes=(),
        row_kind="COMPLETED_MINUTE",
        completed=True,
        bar_start=available - timedelta(minutes=1),
        bar_end=available,
    )


def test_bearish_direction_maps_to_buy_put_not_naked_sell() -> None:
    assert allowed_actions("SELL") == frozenset(
        {
            SignalAction.BUY_PUT,
            SignalAction.PUT_DEBIT_SPREAD,
            SignalAction.NO_TRADE,
        }
    )


def test_bullish_direction_maps_only_to_defined_risk_actions() -> None:
    assert allowed_actions("BUY") == frozenset(
        {
            SignalAction.BUY_CALL,
            SignalAction.CALL_DEBIT_SPREAD,
            SignalAction.NO_TRADE,
        }
    )


def test_unvalidated_model_cannot_emit_trade() -> None:
    signal = SelectiveDecision(validated=False).evaluate(prediction(), quality())
    assert signal.mode == "RESEARCH"
    assert signal.action is SignalAction.NO_TRADE
    assert "MODEL_NOT_PROMOTED" in signal.reasons


def test_missing_production_gates_are_all_explicit() -> None:
    signal = SelectiveDecision(
        validated=True,
        buy_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.9},
        sell_thresholds={"NIFTY": 0.85, "BANKNIFTY": 0.95},
    ).evaluate(prediction(), quality())
    assert signal.action is SignalAction.NO_TRADE
    assert set(signal.reasons) >= {
        "CALIBRATION_UNAVAILABLE",
        "MODEL_DISAGREEMENT_UNAVAILABLE",
        "META_LABEL_UNAVAILABLE",
        "CONFORMAL_UNAVAILABLE",
        "SEQUENTIAL_EVIDENCE_UNAVAILABLE",
        "ECONOMICS_UNAVAILABLE",
    }
    assert signal.buy_threshold == 0.8
    assert signal.sell_threshold == 0.85
    assert signal.model_disagreement is None
    assert signal.meta_label_probability is None
    assert signal.conformal_accepted is None
    assert signal.sequential_evidence_accepted is None
    assert signal.expected_value_after_costs is None


def test_bad_quality_cannot_be_overridden_by_other_gates() -> None:
    signal = SelectiveDecision(
        validated=True,
        buy_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.9},
        sell_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.9},
        maximum_model_disagreement=0.2,
        model_disagreement=0.1,
        meta_label_threshold=0.7,
        meta_label_probability=0.9,
        conformal_accepted=True,
        sequential_evidence_accepted=True,
    ).evaluate(
        prediction(
            calibrated_up=0.9,
            calibrated_down=0.05,
            calibrated_no_move=0.05,
        ),
        quality(tradable=False),
        EconomicsAssessment(
            accepted=True,
            action=SignalAction.BUY_CALL,
            expected_value_after_costs=25.0,
        ),
    )
    assert signal.action is SignalAction.NO_TRADE
    assert "DATA_QUALITY_FAILED" in signal.reasons
    assert DataQualityCode.STALE_SOURCE.value in signal.reasons


def test_quality_codes_fail_closed_even_if_tradable_flag_is_inconsistent() -> None:
    inconsistent = quality().model_copy(
        update={"codes": (DataQualityCode.STALE_SOURCE,)}
    )
    signal = SelectiveDecision().evaluate(prediction(), inconsistent)
    assert "DATA_QUALITY_FAILED" in signal.reasons
    assert DataQualityCode.STALE_SOURCE.value in signal.reasons


def test_instrument_specific_threshold_is_used() -> None:
    decision = SelectiveDecision(
        validated=True,
        buy_thresholds={"NIFTY": 0.91, "BANKNIFTY": 0.8},
        sell_thresholds={"NIFTY": 0.9, "BANKNIFTY": 0.8},
    )
    signal = decision.evaluate(
        prediction(calibrated_up=0.9, calibrated_down=0.05, calibrated_no_move=0.05),
        quality(),
    )
    assert signal.action is SignalAction.NO_TRADE
    assert "BUY_THRESHOLD_NOT_MET" in signal.reasons


def test_equal_up_down_probabilities_have_no_directional_edge() -> None:
    signal = SelectiveDecision(
        validated=True,
        buy_thresholds={"NIFTY": 0.4, "BANKNIFTY": 0.4},
        sell_thresholds={"NIFTY": 0.4, "BANKNIFTY": 0.4},
        maximum_model_disagreement=0.2,
        model_disagreement=0.1,
        meta_label_threshold=0.7,
        meta_label_probability=0.9,
        conformal_accepted=True,
        sequential_evidence_accepted=True,
    ).evaluate(
        prediction(
            up=0.45,
            down=0.45,
            no_move=0.1,
            calibrated_up=0.45,
            calibrated_down=0.45,
            calibrated_no_move=0.1,
        ),
        quality(),
        EconomicsAssessment(
            accepted=True,
            action=SignalAction.BUY_PUT,
            expected_value_after_costs=25.0,
        ),
    )
    assert signal.action is SignalAction.NO_TRADE
    assert signal.direction is None
    assert "NO_DIRECTIONAL_EDGE" in signal.reasons


def test_all_available_gates_without_provenance_still_abstain() -> None:
    signal = SelectiveDecision(
        validated=True,
        buy_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.9},
        sell_thresholds={"NIFTY": 0.85, "BANKNIFTY": 0.95},
        maximum_model_disagreement=0.2,
        model_disagreement=0.1,
        meta_label_threshold=0.7,
        meta_label_probability=0.9,
        conformal_accepted=True,
        sequential_evidence_accepted=True,
    ).evaluate(
        prediction(
            calibrated_up=0.9,
            calibrated_down=0.05,
            calibrated_no_move=0.05,
        ),
        quality(),
        EconomicsAssessment(
            accepted=True,
            action=SignalAction.BUY_CALL,
            expected_value_after_costs=25.0,
        ),
    )
    assert signal.mode == "RESEARCH"
    assert signal.action is SignalAction.NO_TRADE
    assert set(signal.reasons) >= {
        "FEATURE_ROW_MISSING",
        "FEATURE_TIME_MISSING",
        "MODEL_VERSION_MISSING",
        "FEATURE_SCHEMA_VERSION_MISSING",
        "HORIZON_MISSING",
        "TRAINING_WINDOW_MISSING",
        "TRAINING_WINDOW_ID_MISSING",
        "CALIBRATION_ID_MISSING",
        "VALIDATION_REPORT_ID_MISSING",
    }


def test_matching_provenance_allows_only_a_research_action() -> None:
    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    signal = SelectiveDecision(
        validated=True,
        **decision_provenance(stamp),
        buy_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.9},
        sell_thresholds={"NIFTY": 0.85, "BANKNIFTY": 0.95},
        maximum_model_disagreement=0.2,
        model_disagreement=0.1,
        meta_label_threshold=0.7,
        meta_label_probability=0.9,
        conformal_accepted=True,
        sequential_evidence_accepted=True,
    ).evaluate(
        prediction(
            calibrated_up=0.9,
            calibrated_down=0.05,
            calibrated_no_move=0.05,
            **provenance(stamp),
        ),
        quality().model_copy(update={"details": {"instrument": "NIFTY"}}),
        EconomicsAssessment(
            accepted=True,
            action=SignalAction.BUY_CALL,
            expected_value_after_costs=25.0,
        ),
        row=feature_row(stamp=stamp),
    )
    assert signal.mode == "RESEARCH"
    assert signal.action is SignalAction.BUY_CALL
    assert signal.direction == "BUY"
    assert signal.reasons == ()
    assert signal.validation_report_id == "walk-forward-2026-08"
    assert signal.training_window_id == "train-2026-q2"
    assert signal.feature_available_at == stamp
    assert str(signal.validation_completed_at.tzinfo) == "Asia/Kolkata"
    assert signal.model_dump(mode="json")["feature_available_at"] == stamp.isoformat()


@pytest.mark.parametrize(
    "mismatch", ["row_instrument", "quality_instrument", "time", "model_identity"]
)
def test_prediction_quality_and_row_provenance_must_match(mismatch: str) -> None:
    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    row = feature_row(stamp=stamp)
    report = quality().model_copy(update={"details": {"instrument": "NIFTY"}})
    if mismatch == "row_instrument":
        row = feature_row(instrument="BANKNIFTY", stamp=stamp)
    elif mismatch == "quality_instrument":
        report = report.model_copy(update={"details": {"instrument": "BANKNIFTY"}})
    elif mismatch == "time":
        report = report.model_copy(update={"checked_at": stamp - timedelta(seconds=1)})
    prediction_provenance = provenance(stamp)
    if mismatch == "model_identity":
        prediction_provenance["model_version"] = "different-model"
    signal = SelectiveDecision(
        validated=True,
        **decision_provenance(stamp),
    ).evaluate(
        prediction(**prediction_provenance),
        report,
        row=row,
    )
    assert signal.action is SignalAction.NO_TRADE
    expected = {
        "time": "QUALITY_PREDATES_FEATURES",
        "model_identity": "MODEL_VERSION_MISMATCH",
    }.get(mismatch, "INSTRUMENT_MISMATCH")
    assert expected in signal.reasons


def test_validation_identity_and_chronology_are_required_for_promotion() -> None:
    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    signal = SelectiveDecision(
        validated=True,
        **(
            decision_provenance(stamp)
            | {
                "validation_report_id": "different-report",
                "validation_completed_at": stamp + timedelta(seconds=1),
            }
        ),
    ).evaluate(
        prediction(**provenance(stamp)),
        quality().model_copy(update={"details": {"instrument": "NIFTY"}}),
        row=feature_row(stamp=stamp),
    )
    assert signal.action is SignalAction.NO_TRADE
    assert "VALIDATION_REPORT_MISMATCH" in signal.reasons
    assert "VALIDATION_AFTER_PREDICTION" in signal.reasons


def test_economics_cannot_select_wrong_side_action() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        EconomicsAssessment(
            accepted=True,
            action=SignalAction.BUY_PUT,
            expected_value_after_costs=25.0,
        ).for_direction("BUY")


def test_selective_configuration_rejects_unknown_or_shared_thresholds() -> None:
    with pytest.raises(ValueError):
        SelectiveDecision(
            buy_thresholds={"NIFTY": 0.8},
            sell_thresholds={"NIFTY": 0.8},
        )
    with pytest.raises(ValueError):
        SelectiveDecision(
            buy_thresholds={"NIFTY": 1.1, "BANKNIFTY": 0.8},
            sell_thresholds={"NIFTY": 0.8, "BANKNIFTY": 0.8},
        )


def test_naive_quality_timestamp_fails_closed() -> None:
    naive = quality().model_copy(
        update={"checked_at": datetime.fromisoformat("2026-08-26T10:00:00")}
    )
    signal = SelectiveDecision().evaluate(prediction(), naive)
    assert signal.action is SignalAction.NO_TRADE
    assert "QUALITY_TIMESTAMP_INVALID" in signal.reasons


def test_naive_quality_timestamp_with_provenance_does_not_break_chronology_gate() -> None:
    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    naive = quality().model_copy(
        update={
            "checked_at": datetime.fromisoformat("2026-08-26T10:00:00"),
            "details": {"instrument": "NIFTY"},
        }
    )
    signal = SelectiveDecision(
        validated=True,
        **decision_provenance(stamp),
    ).evaluate(
        prediction(**provenance(stamp)),
        naive,
        row=feature_row(stamp=stamp),
    )
    assert signal.action is SignalAction.NO_TRADE
    assert "QUALITY_TIMESTAMP_INVALID" in signal.reasons
