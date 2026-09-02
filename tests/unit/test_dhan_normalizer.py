import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nifty_signal_engine.data.dhan_client import RawSnapshot
from nifty_signal_engine.data.normalizer import (
    BrokerPayloadError,
    normalize_option_chain,
)

IST = ZoneInfo("Asia/Kolkata")
FIXTURES = Path(__file__).parents[1] / "fixtures"


def normalize_fixture(name: str = "dhan_option_chain.json"):
    raw = RawSnapshot(
        body=(FIXTURES / name).read_bytes(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )
    return normalize_option_chain(
        raw, "NIFTY", datetime(2026, 8, 30, 10, 0, 1, tzinfo=IST)
    )


def test_normalizer_converts_iv_percent_and_preserves_api_greeks() -> None:
    snapshot = normalize_fixture()

    call = next(quote for quote in snapshot.quotes if quote.option_type == "CE")

    assert 0 < call.iv < 1
    assert call.api_gamma == pytest.approx(0.0012)
    assert call.api_delta == pytest.approx(0.54)
    assert call.bid <= call.ask


def test_normalizer_keeps_available_side_when_strike_has_no_put() -> None:
    snapshot = normalize_fixture()

    at_24400 = [quote for quote in snapshot.quotes if quote.strike == 24400]

    assert [quote.option_type for quote in at_24400] == ["CE"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["data"].pop("last_price"),
        lambda payload: payload["data"]["oc"].__setitem__("not-a-strike", {}),
        lambda payload: payload["data"]["oc"]["24300.000000"]["ce"].__setitem__(
            "implied_volatility", 150.0
        ),
    ],
)
def test_normalizer_rejects_malformed_or_ambiguous_quotes(mutate) -> None:
    payload = json.loads((FIXTURES / "dhan_option_chain.json").read_text())
    mutate(payload)
    raw = RawSnapshot(
        body=json.dumps(payload).encode(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    with pytest.raises(BrokerPayloadError):
        normalize_option_chain(raw, "NIFTY", datetime(2026, 8, 30, 10, 1, tzinfo=IST))


def test_normalizer_rejects_naive_received_timestamp() -> None:
    raw = RawSnapshot(
        body=(FIXTURES / "dhan_option_chain.json").read_bytes(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    with pytest.raises(BrokerPayloadError):
        normalize_option_chain(
            raw,
            "NIFTY",
            datetime(2026, 8, 30, 10, 1),  # noqa: DTZ001 - intentional invalid input
        )


def test_normalizer_rejects_duplicate_normalized_contract_identity() -> None:
    payload = json.loads((FIXTURES / "dhan_option_chain.json").read_text())
    payload["data"]["oc"]["24300.0"] = {
        "ce": payload["data"]["oc"]["24300.000000"]["ce"]
    }
    raw = RawSnapshot(
        body=json.dumps(payload).encode(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    with pytest.raises(BrokerPayloadError, match="duplicate option contract"):
        normalize_option_chain(raw, "NIFTY", datetime(2026, 8, 30, 10, 1, tzinfo=IST))


def test_normalizer_rejects_received_at_before_capture() -> None:
    raw = RawSnapshot(
        body=(FIXTURES / "dhan_option_chain.json").read_bytes(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    with pytest.raises(BrokerPayloadError, match="received_at cannot precede"):
        normalize_option_chain(raw, "NIFTY", datetime(2026, 8, 30, 9, 59, tzinfo=IST))


def test_normalizer_compares_aware_timestamps_as_absolute_instants() -> None:
    raw = RawSnapshot(
        body=(FIXTURES / "dhan_option_chain.json").read_bytes(),
        captured_at=datetime(2026, 8, 30, 10, 0, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    snapshot = normalize_option_chain(
        raw, "NIFTY", datetime.fromisoformat("2026-08-30T04:30:00+00:00")
    )

    assert snapshot.received_at == raw.captured_at


def test_normalizer_canonicalizes_aware_timestamps_to_india() -> None:
    raw = RawSnapshot(
        body=(FIXTURES / "dhan_option_chain.json").read_bytes(),
        captured_at=datetime.fromisoformat("2026-08-30T04:30:00+00:00"),
        expiry=date(2026, 9, 1),
    )

    snapshot = normalize_option_chain(
        raw, "NIFTY", datetime.fromisoformat("2026-08-30T04:30:01+00:00")
    )

    assert getattr(snapshot.source_timestamp.tzinfo, "key", None) == "Asia/Kolkata"
    assert getattr(snapshot.received_at.tzinfo, "key", None) == "Asia/Kolkata"
    assert snapshot.source_timestamp.isoformat() == "2026-08-30T10:00:00+05:30"
    assert snapshot.received_at.isoformat() == "2026-08-30T10:00:01+05:30"
    assert {
        getattr(quote.timestamp.tzinfo, "key", None) for quote in snapshot.quotes
    } == {"Asia/Kolkata"}
    assert {quote.timestamp.isoformat() for quote in snapshot.quotes} == {
        "2026-08-30T10:00:00+05:30"
    }


def test_normalizer_uses_broker_source_time_not_local_http_receipt() -> None:
    """Replacing exchange time with the local receipt must fail this provenance contract."""
    payload = json.loads((FIXTURES / "dhan_option_chain.json").read_text())
    payload["data"]["timestamp"] = "2026-08-30T09:59:58+05:30"
    raw = RawSnapshot(
        body=json.dumps(payload).encode(),
        captured_at=datetime(2026, 8, 30, 10, 0, 1, tzinfo=IST),
        expiry=date(2026, 9, 1),
    )

    snapshot = normalize_option_chain(
        raw, "NIFTY", datetime(2026, 8, 30, 10, 0, 2, tzinfo=IST)
    )

    assert snapshot.source_time_authoritative is True
    assert snapshot.source_timestamp.isoformat() == "2026-08-30T09:59:58+05:30"
    assert snapshot.received_at.isoformat() == "2026-08-30T10:00:02+05:30"
    assert {quote.timestamp for quote in snapshot.quotes} == {snapshot.source_timestamp}
