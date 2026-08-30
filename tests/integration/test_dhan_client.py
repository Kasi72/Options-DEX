import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from nifty_signal_engine.data.dhan_client import (
    BrokerHTTPError,
    BrokerPayloadError,
    DhanClient,
)

IST = ZoneInfo("Asia/Kolkata")
FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_fetch_expiries_uses_exchange_metadata_and_correct_nifty_request() -> None:
    expected = json.loads((FIXTURES / "dhan_expiries.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.dhan.co/v2/optionchain/expirylist"
        assert request.headers["access-token"] == "test-token"
        assert request.headers["client-id"] == "test-client"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "UnderlyingScrip": 13,
            "UnderlyingSeg": "IDX_I",
        }
        return httpx.Response(200, json=expected)

    client = DhanClient(
        access_token="test-token",
        client_id="test-client",
        transport=httpx.MockTransport(handler),
        timeout=2.5,
    )

    expiries = asyncio.run(client.fetch_expiries("NIFTY"))

    assert expiries == (date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 29))


def test_fetch_option_chain_uses_banknifty_identifier_and_expiry() -> None:
    body = (FIXTURES / "dhan_option_chain.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.dhan.co/v2/optionchain"
        assert json.loads(request.content) == {
            "UnderlyingScrip": 25,
            "UnderlyingSeg": "IDX_I",
            "Expiry": "2026-09-01",
        }
        return httpx.Response(200, content=body)

    client = DhanClient(
        access_token="test-token",
        client_id="test-client",
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 8, 30, 10, 0, tzinfo=IST),
    )

    raw = asyncio.run(client.fetch_option_chain("BANKNIFTY", date(2026, 9, 1)))

    assert raw.body == body
    assert raw.captured_at.tzinfo is not None


@pytest.mark.parametrize(
    "response", [httpx.Response(503), httpx.Response(200, content=b"{")]
)
def test_client_raises_typed_errors_for_bad_responses(response: httpx.Response) -> None:
    client = DhanClient(
        access_token="test-token",
        client_id="test-client",
        transport=httpx.MockTransport(lambda _: response),
    )

    error = BrokerHTTPError if response.status_code != 200 else BrokerPayloadError
    with pytest.raises(error):
        asyncio.run(client.fetch_expiries("NIFTY"))
