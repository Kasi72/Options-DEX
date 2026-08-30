"""Small, credential-injected boundary for Dhan option-chain responses."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx


Instrument = Literal["NIFTY", "BANKNIFTY"]
IST = ZoneInfo("Asia/Kolkata")
_INSTRUMENT_REQUESTS: dict[Instrument, dict[str, int | str]] = {
    "NIFTY": {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
    "BANKNIFTY": {"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
}


class BrokerHTTPError(RuntimeError):
    """Dhan returned a non-success HTTP response."""


class BrokerPayloadError(ValueError):
    """Dhan returned a response that cannot be safely interpreted."""


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """Unchanged broker response bytes plus local capture metadata."""

    body: bytes
    captured_at: datetime
    expiry: date | None = None

    def __post_init__(self) -> None:
        if (
            self.captured_at.tzinfo is None
            or self.captured_at.tzinfo.utcoffset(self.captured_at) is None
        ):
            raise BrokerPayloadError("captured_at must be timezone-aware")


class DhanClient:
    """HTTP-only Dhan adapter; it never normalizes or submits orders."""

    _base_url = "https://api.dhan.co/v2"

    def __init__(
        self,
        *,
        access_token: str,
        client_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._headers = {
            "access-token": access_token,
            "client-id": client_id,
            "Content-Type": "application/json",
        }
        self._transport = transport
        self._timeout = timeout
        self._clock = clock or (lambda: datetime.now(IST))

    async def fetch_expiries(self, instrument: Instrument) -> tuple[date, ...]:
        payload = await self._post("/optionchain/expirylist", _request_for(instrument))
        values = payload.get("data")
        if not isinstance(values, list):
            raise BrokerPayloadError("expiry response data must be a list")
        try:
            expiries = tuple(
                date.fromisoformat(value) for value in values if isinstance(value, str)
            )
        except ValueError as error:
            raise BrokerPayloadError(
                "expiry response contains an invalid date"
            ) from error
        if len(expiries) != len(values) or len(set(expiries)) != len(expiries):
            raise BrokerPayloadError("expiry response is malformed or ambiguous")
        return expiries

    async def fetch_option_chain(
        self, instrument: Instrument, expiry: date
    ) -> RawSnapshot:
        request = {**_request_for(instrument), "Expiry": expiry.isoformat()}
        body = await self._post_bytes("/optionchain", request)
        return RawSnapshot(body=body, captured_at=self._clock(), expiry=expiry)

    async def _post(
        self, path: str, payload: dict[str, int | str]
    ) -> dict[str, object]:
        body = await self._post_bytes(path, payload)
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BrokerPayloadError("broker response is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise BrokerPayloadError("broker response root must be an object")
        return parsed

    async def _post_bytes(self, path: str, payload: dict[str, int | str]) -> bytes:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = await client.post(path, json=payload)
        if response.status_code != httpx.codes.OK:
            raise BrokerHTTPError(
                f"Dhan request failed with HTTP {response.status_code}"
            )
        return response.content


def _request_for(instrument: Instrument) -> dict[str, int | str]:
    try:
        return dict(_INSTRUMENT_REQUESTS[instrument])
    except KeyError as error:
        raise BrokerPayloadError(f"unsupported instrument: {instrument!r}") from error
