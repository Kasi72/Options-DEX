from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from nifty_signal_engine.data.supabase_rest import (
    SupabaseConfigurationError,
    SupabaseRestSettings,
    snapshot_row,
)


def test_settings_require_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    with pytest.raises(SupabaseConfigurationError):
        SupabaseRestSettings.from_environment()


def test_snapshot_row_is_content_addressed_and_serializable() -> None:
    timestamp = datetime(2026, 9, 7, 9, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    row = snapshot_row(
        raw_payload=b"raw",
        normalized_payload={"instrument": "NIFTY"},
        quality_payload={"tradable": False, "codes": []},
        instrument="NIFTY",
        source_timestamp=timestamp,
        received_at=timestamp,
        expiry=date(2026, 9, 10),
        source="test",
        source_time_authoritative=False,
    )
    assert row["raw_sha256"]
    assert row["session_date"] == "2026-09-07"
    assert row["raw_payload"]["encoding"] == "hex"
