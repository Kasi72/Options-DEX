"""Immutable research-signal contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class SignalAction(StrEnum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"
    NO_TRADE = "NO_TRADE"


class ResearchSignal(BaseModel, frozen=True):
    """A provisional research decision and its explicit rationale."""

    mode: Literal["RESEARCH"] = "RESEARCH"
    action: SignalAction = SignalAction.NO_TRADE
    instrument: Literal["NIFTY", "BANKNIFTY"] | None = None
    direction: Literal["BUY", "SELL"] | None = None
    reasons: tuple[str, ...] = ()
    buy_threshold: float | None = None
    sell_threshold: float | None = None
    model_disagreement: float | None = None
    meta_label_probability: float | None = None
    conformal_accepted: bool | None = None
    sequential_evidence_accepted: bool | None = None
    economics_accepted: bool | None = None
    expected_value_after_costs: float | None = None
    feature_available_at: datetime | None = None
    model_version: str | None = None
    feature_schema_version: str | None = None
    horizon_minutes: int | None = None
    training_window_start: datetime | None = None
    training_window_end: datetime | None = None
    training_window_id: str | None = None
    calibration_id: str | None = None
    calibration_completed_at: datetime | None = None
    validation_report_id: str | None = None
    validation_completed_at: datetime | None = None
