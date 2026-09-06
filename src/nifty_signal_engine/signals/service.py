"""Safety boundary for translating underlying direction into option actions."""

from typing import Literal

from nifty_signal_engine.domain.signal import SignalAction


def allowed_actions(direction: Literal["BUY", "SELL"]) -> frozenset[SignalAction]:
    """Return defined-risk actions; SELL never means writing an option."""
    if direction == "BUY":
        return frozenset(
            {SignalAction.BUY_CALL, SignalAction.CALL_DEBIT_SPREAD, SignalAction.NO_TRADE}
        )
    if direction == "SELL":
        return frozenset(
            {SignalAction.BUY_PUT, SignalAction.PUT_DEBIT_SPREAD, SignalAction.NO_TRADE}
        )
    raise ValueError("direction must be BUY or SELL")
