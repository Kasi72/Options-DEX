"""Immutable research-signal contracts."""

from enum import StrEnum

from pydantic import BaseModel


class SignalAction(StrEnum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"
    NO_TRADE = "NO_TRADE"


class ResearchSignal(BaseModel, frozen=True):
    """A provisional research decision and its explicit rationale."""

    action: SignalAction = SignalAction.NO_TRADE
    reasons: tuple[str, ...] = ()
