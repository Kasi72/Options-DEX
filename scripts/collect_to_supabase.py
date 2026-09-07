"""Scheduled Dhan → Supabase research collector.

This process is intended for GitHub Actions or another scheduler.  It never
places orders and it never exposes the Dhan token to the Streamlit client.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.dhan_client import DhanClient
from nifty_signal_engine.data.normalizer import normalize_option_chain
from nifty_signal_engine.data.supabase_rest import (
    SupabaseRestClient,
    SupabaseRestSettings,
    snapshot_row,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.monitoring.data_quality import QualityConfig, TradingCalendar, assess_snapshot

IST = ZoneInfo("Asia/Kolkata")
Instrument = Literal["NIFTY", "BANKNIFTY"]


async def collect(instrument: Instrument) -> None:
    dhan_token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    dhan_client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()
    settings = SupabaseRestSettings.from_environment(service_role=True)
    if not dhan_token or not dhan_client_id or settings is None:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, SUPABASE_URL, and "
            "SUPABASE_SERVICE_ROLE_KEY are required"
        )
    client = DhanClient(access_token=dhan_token, client_id=dhan_client_id)
    expiries = await client.fetch_expiries(instrument)
    if not expiries:
        raise SystemExit(f"no active expiry returned for {instrument}")
    raw = await client.fetch_option_chain(instrument, expiries[0])
    received_at = raw.captured_at.astimezone(IST)
    snapshot = normalize_option_chain(raw, instrument, received_at)
    repository = SupabaseRestClient(settings)
    prior_rows = repository.select(
        "market_snapshots",
        query={
            "select": "normalized_payload,source_timestamp",
            "instrument": f"eq.{instrument}",
            "session_date": f"eq.{snapshot.source_timestamp.date().isoformat()}",
            "order": "source_timestamp.desc,id.desc",
            "limit": "1",
        },
    )
    previous = None
    if prior_rows:
        payload = prior_rows[0].get("normalized_payload")
        if isinstance(payload, dict):
            try:
                previous = OptionChainSnapshot.model_validate(payload)
            except (TypeError, ValueError):
                previous = None
    # Dhan supplies a local HTTP receipt rather than an exchange timestamp.
    # It is suitable for freshness checks, while the dashboard still records
    # the provenance as non-authoritative.  Deep OTM quotes can also have very
    # wide markets or no IV; those are excluded by downstream feature gates.
    quality = assess_snapshot(
        snapshot,
        previous,
        datetime.now(IST),
        calendar=TradingCalendar({snapshot.source_timestamp.date()}),
        active_expiries=expiries,
        config=QualityConfig(
            maximum_relative_spread=1.0,
            require_authoritative_source_time=False,
            require_authoritative_quote_time=False,
            require_valid_iv=False,
        ),
    )
    row = snapshot_row(
        raw_payload=raw.body,
        normalized_payload=snapshot.model_dump(mode="json"),
        quality_payload=quality.model_dump(mode="json"),
        instrument=instrument,
        source_timestamp=snapshot.source_timestamp,
        received_at=snapshot.received_at,
        expiry=snapshot.expiry,
        source="dhan",
        source_time_authoritative=snapshot.source_time_authoritative,
    )
    repository.insert("market_snapshots", row)
    print(f"published {instrument} {snapshot.source_timestamp.isoformat()}")


def main() -> None:
    instrument = cast(Instrument, os.environ.get("INSTRUMENT", "NIFTY"))
    if instrument not in {"NIFTY", "BANKNIFTY"}:
        raise SystemExit("INSTRUMENT must be NIFTY or BANKNIFTY")
    asyncio.run(collect(instrument))


if __name__ == "__main__":
    main()
