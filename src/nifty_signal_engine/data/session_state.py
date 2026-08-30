"""Session-aware state for cumulative option-chain counters."""

from dataclasses import dataclass
from datetime import date, datetime

from nifty_signal_engine.config.market_hours import session_date
from nifty_signal_engine.domain.market import OptionChainSnapshot


@dataclass(frozen=True, slots=True)
class IncrementalSnapshot:
    """Changes derived from one snapshot and the prior valid same-session one."""

    instrument: str
    timestamp: datetime
    session: date
    incremental_volume: int | None
    incremental_call_volume: int | None
    incremental_put_volume: int | None
    cross_session: bool
    counter_reset: bool = False


@dataclass(slots=True)
class _CounterState:
    session: date
    call_volume: int
    put_volume: int


class SessionState:
    """Maintain independent cumulative-volume counters per instrument."""

    def __init__(self) -> None:
        self._states: dict[str, _CounterState] = {}

    def update(self, snapshot: OptionChainSnapshot) -> IncrementalSnapshot:
        timestamp = snapshot.source_timestamp
        current_session = session_date(timestamp)
        instrument = snapshot.instrument
        call_volume = sum(
            quote.volume for quote in snapshot.quotes if quote.option_type == "CE"
        )
        put_volume = sum(
            quote.volume for quote in snapshot.quotes if quote.option_type == "PE"
        )
        previous = self._states.get(instrument)
        self._states[instrument] = _CounterState(current_session, call_volume, put_volume)

        if previous is None or previous.session != current_session:
            return IncrementalSnapshot(
                instrument, timestamp, current_session, None, None, None,
                previous is not None,
            )

        call_delta = call_volume - previous.call_volume
        put_delta = put_volume - previous.put_volume
        if call_delta < 0 or put_delta < 0:
            return IncrementalSnapshot(
                instrument, timestamp, current_session, None, None, None,
                False, True,
            )
        return IncrementalSnapshot(
            instrument, timestamp, current_session, call_delta + put_delta,
            call_delta, put_delta, False,
        )
