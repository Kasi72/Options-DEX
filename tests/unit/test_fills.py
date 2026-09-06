from nifty_signal_engine.backtesting.fills import (
    ExecutableQuote,
    FillSimulator,
    RejectionCode,
    TradeCandidate,
)
from tests.factories import aware


def test_long_entry_uses_next_ask_not_signal_ltp() -> None:
    candidate = TradeCandidate(
        signal_time=aware("2026-08-26T10:00:00+05:30"),
        contract_id="NIFTY-20260901-24300-CE",
    )
    quote = ExecutableQuote(
        timestamp=aware("2026-08-26T10:00:15+05:30"),
        contract_id=candidate.contract_id,
        bid=99,
        ask=101,
    )

    fill = FillSimulator().enter_long(candidate, quote)

    assert fill.price == 101
    assert fill.executed_at == quote.timestamp
    assert fill.bid == 99 and fill.ask == 101


def test_crossed_or_missing_quote_rejects_entry() -> None:
    candidate = TradeCandidate(
        signal_time=aware("2026-08-26T10:00:00+05:30"),
        contract_id="NIFTY-20260901-24300-CE",
    )
    quote = ExecutableQuote(
        timestamp=aware("2026-08-26T10:00:15+05:30"),
        contract_id=candidate.contract_id,
        bid=102,
        ask=101,
    )

    result = FillSimulator().enter_long(candidate, quote)

    assert result.code is RejectionCode.INVALID_QUOTE


def test_quote_at_or_before_signal_is_never_an_executable_next_quote() -> None:
    candidate = TradeCandidate(
        signal_time=aware("2026-08-26T10:00:00+05:30"), contract_id="contract"
    )
    quote = ExecutableQuote(
        timestamp=candidate.signal_time, contract_id="contract", bid=99, ask=101
    )

    result = FillSimulator().enter_long(candidate, quote)

    assert result.code is RejectionCode.NOT_NEXT_QUOTE
