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

    def __init__(self, status_code: int, body: bytes, received_at: datetime) -> None:
        super().__init__(f"Dhan request failed with HTTP {status_code}")
        self.status_code = status_code
        self.body = bytes(body)
        self.received_at = received_at

    @property
    def transient(self) -> bool:
        return self.status_code == 429 or 500 <= self.status_code <= 599


class BrokerPayloadError(ValueError):
    """Dhan returned a response that cannot be safely interpreted."""


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """Unchanged broker response bytes plus local capture metadata."""

    body: bytes
    captured_at: datetime
    expiry: date | None = None
    broker_source_timestamp: datetime | None = None
    source_time_authoritative: bool = False

    def __post_init__(self) -> None:
        if (
            self.captured_at.tzinfo is None
            or self.captured_at.tzinfo.utcoffset(self.captured_at) is None
        ):
            raise BrokerPayloadError("captured_at must be timezone-aware")
        if self.source_time_authoritative and self.broker_source_timestamp is None:
            raise BrokerPayloadError(
                "authoritative source time is required when marked present"
            )
        if self.broker_source_timestamp is not None and (
            self.broker_source_timestamp.tzinfo is None
            or self.broker_source_timestamp.tzinfo.utcoffset(
                self.broker_source_timestamp
            )
            is None
        ):
            raise BrokerPayloadError("broker_source_timestamp must be timezone-aware")
        object.__setattr__(self, "captured_at", self.captured_at.astimezone(IST))
        if self.broker_source_timestamp is not None:
            object.__setattr__(
                self,
                "broker_source_timestamp",
                self.broker_source_timestamp.astimezone(IST),
            )


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
        return tuple(
            sorted(expiry for expiry in expiries if expiry >= self._clock().date())
        )

    async def fetch_option_chain(
        self, instrument: Instrument, expiry: date
    ) -> RawSnapshot:
        request = {**_request_for(instrument), "Expiry": expiry.isoformat()}
        body = await self._post_bytes("/optionchain", request)
        captured_at = self._clock()
        source_timestamp = _optional_source_timestamp(body)
        return RawSnapshot(
            body=body,
            captured_at=captured_at,
            expiry=expiry,
            broker_source_timestamp=source_timestamp,
            source_time_authoritative=source_timestamp is not None,
        )

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
            raise BrokerHTTPError(response.status_code, response.content, self._clock())
        return response.content


def is_transient_error(error: Exception) -> bool:
    """Classify only network timeout/transport and broker overload failures as retryable."""
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError)) or (
        isinstance(error, BrokerHTTPError) and error.transient
    )


def _optional_source_timestamp(body: bytes) -> datetime | None:
    """Extract only a valid documented source timestamp; leave malformed bytes raw-first."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    value = payload["data"].get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
        return None
    return timestamp.astimezone(IST)


def _request_for(instrument: Instrument) -> dict[str, int | str]:
    try:
        return dict(_INSTRUMENT_REQUESTS[instrument])
    except KeyError as error:
        raise BrokerPayloadError(f"unsupported instrument: {instrument!r}") from error
