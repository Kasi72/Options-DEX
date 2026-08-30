from nifty_signal_engine.data.session_state import SessionState
from tests.factories import make_chain


def test_first_snapshot_of_new_session_has_no_change() -> None:
    state = SessionState()
    state.update(make_chain(timestamp="2026-08-26T15:29:00+05:30", call_volume=1000))
    result = state.update(make_chain(timestamp="2026-08-27T09:15:00+05:30", call_volume=10))
    assert result.incremental_volume is None
    assert result.cross_session is True


def test_incremental_volume_is_computed_within_a_session() -> None:
    state = SessionState()
    state.update(make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100))
    result = state.update(make_chain(timestamp="2026-08-26T09:16:00+05:30", call_volume=125, put_volume=20))
    assert result.incremental_volume == 45
    assert result.incremental_call_volume == 25
    assert result.incremental_put_volume == 20
    assert result.cross_session is False


def test_counter_reset_makes_change_unavailable() -> None:
    state = SessionState()
    state.update(make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100))
    result = state.update(make_chain(timestamp="2026-08-26T09:16:00+05:30", call_volume=10))
    assert result.incremental_volume is None
    assert result.counter_reset is True


def test_instruments_keep_independent_counters() -> None:
    state = SessionState()
    nifty = make_chain(timestamp="2026-08-26T09:15:00+05:30", call_volume=100)
    bank = nifty.model_copy(update={"instrument": "BANKNIFTY"})
    state.update(nifty)
    first_bank = state.update(bank)
    assert first_bank.incremental_volume is None
