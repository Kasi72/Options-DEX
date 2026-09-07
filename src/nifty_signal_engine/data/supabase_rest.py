"""Small privilege-separated Supabase REST boundary.

The dashboard uses a publishable/anon key for SELECT only.  The scheduled
collector uses a service key for INSERT.  No credentials are persisted here.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
from types import SimpleNamespace

from nifty_signal_engine.monitoring.data_quality import DataQualityReport


class SupabaseConfigurationError(RuntimeError):
    """Required Supabase settings are missing or malformed."""


# Streamlit Cloud currently executes the entrypoint through a loader that may
# not register this module in ``sys.modules`` before applying ``dataclass``.
# ``slots=True`` asks dataclasses to resolve postponed annotations through that
# module and raises an AttributeError there.  Frozen semantics are retained;
# regular dataclass layout keeps the adapter compatible with that loader.
@dataclass(frozen=True)
class SupabaseRestSettings:
    url: str
    key: str

    @classmethod
    def from_environment(cls, *, service_role: bool = False) -> "SupabaseRestSettings | None":
        url = os.environ.get("SUPABASE_URL", "").strip()
        key_name = "SUPABASE_SERVICE_ROLE_KEY" if service_role else "SUPABASE_ANON_KEY"
        key = os.environ.get(key_name, "").strip()
        if not url and not key:
            return None
        if not url or not key:
            raise SupabaseConfigurationError(f"SUPABASE_URL and {key_name} are both required")
        return cls(url=url.rstrip("/"), key=key)


class SupabaseRestClient:
    """Minimal REST client with bounded timeouts and explicit operations."""

    def __init__(self, settings: SupabaseRestSettings, *, timeout: float = 10.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._base = f"{settings.url}/rest/v1"
        self._headers = {
            "apikey": settings.key,
            "Authorization": f"Bearer {settings.key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    def select(
        self,
        table: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        response = httpx.get(
            f"{self._base}/{table}",
            headers=self._headers,
            params=dict(query or {}),
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise SupabaseConfigurationError("Supabase response must be a list of objects")
        return payload

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        response = httpx.post(
            f"{self._base}/{table}",
            headers={**self._headers, "Prefer": "return=minimal"},
            json=dict(row),
            timeout=self._timeout,
        )
        response.raise_for_status()


def snapshot_row(
    *,
    raw_payload: bytes,
    normalized_payload: Mapping[str, Any],
    quality_payload: Mapping[str, Any],
    instrument: str,
    source_timestamp: datetime,
    received_at: datetime,
    expiry: date,
    source: str,
    source_time_authoritative: bool,
) -> dict[str, Any]:
    """Build a JSON-serializable, content-addressed market snapshot row."""
    if instrument not in {"NIFTY", "BANKNIFTY"}:
        raise ValueError("unsupported instrument")
    return {
        "instrument": instrument,
        "session_date": source_timestamp.date().isoformat(),
        "source_timestamp": source_timestamp.isoformat(),
        "received_at": received_at.isoformat(),
        "expiry": expiry.isoformat(),
        "source": source,
        "source_time_authoritative": bool(source_time_authoritative),
        "raw_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "raw_payload": {"encoding": "hex", "value": raw_payload.hex()},
        "normalized_payload": json.loads(json.dumps(normalized_payload, default=str)),
        "quality_payload": json.loads(json.dumps(quality_payload, default=str)),
        "published": True,
    }


def latest_snapshot_rows(
    client: SupabaseRestClient,
    instrument: str,
    session_date: date,
) -> Sequence[dict[str, Any]]:
    """Read published snapshots for one instrument/session in source order."""
    return client.select(
        "market_snapshots",
        query={
            "select": "*",
            "instrument": f"eq.{instrument}",
            "session_date": f"eq.{session_date.isoformat()}",
            "order": "source_timestamp.asc,id.asc",
        },
    )


class SupabaseSnapshotReader:
    """Read-only dashboard health reader over the published snapshot table."""

    def __init__(self, client: SupabaseRestClient) -> None:
        self._client = client

    def _rows(self, instrument: str, session_date: date) -> list[dict[str, Any]]:
        return list(latest_snapshot_rows(self._client, instrument, session_date))

    def iter_audited_session(
        self, instrument: str, session_date: date
    ) -> Sequence[tuple[object, DataQualityReport]]:
        result: list[tuple[object, DataQualityReport]] = []
        for row in self._rows(instrument, session_date):
            quality = row.get("quality_payload")
            if not isinstance(quality, Mapping):
                continue
            try:
                report = DataQualityReport.model_validate(quality)
            except (TypeError, ValueError):
                continue
            source_timestamp = row.get("source_timestamp")
            if not isinstance(source_timestamp, str):
                continue
            try:
                parsed = datetime.fromisoformat(source_timestamp)
            except ValueError:
                continue
            result.append((SimpleNamespace(source_timestamp=parsed), report))
        return tuple(result)

    def session_quality_summary(self, instrument: str, session_date: date) -> Mapping[str, object]:
        rows = self._rows(instrument, session_date)
        codes: dict[str, int] = {}
        tradable = 0
        audited = tuple(self.iter_audited_session(instrument, session_date))
        latest_report = audited[-1][1] if audited else None
        for _, report in audited:
            if report.tradable and not report.codes:
                tradable += 1
            for code in report.codes:
                codes[code.value] = codes.get(code.value, 0) + 1
        latest_codes = {
            code.value: 1 for code in (latest_report.codes if latest_report else ())
        }
        return {
            "snapshot_count": len(rows),
            "tradable_count": tradable,
            # ``codes`` is intentionally current-state only; older rejected
            # observations belong in the diagnostic history field.
            "codes": latest_codes,
            "historical_codes": codes,
            "latest_tradable": bool(latest_report and latest_report.tradable and not latest_report.codes),
            "latest_codes": [code.value for code in latest_report.codes] if latest_report else [],
        }
