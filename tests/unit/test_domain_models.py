from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from nifty_signal_engine.config.instruments import INSTRUMENTS, InstrumentConfig
from nifty_signal_engine.config.settings import RuntimeSettings
from nifty_signal_engine.domain.exposure import (
    DataQualityStatus,
    ExposureResult,
    ZeroLevelResult,
    ZeroLevelStatus,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot, OptionQuote
from nifty_signal_engine.domain.signal import ResearchSignal, SignalAction

IST = ZoneInfo("Asia/Kolkata")


def test_quote_requires_timezone_and_is_frozen() -> None:
    """Removing timezone validation or frozen configuration must fail here."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        OptionQuote(
            timestamp=datetime.fromisoformat("2026-08-26T09:16:00"),
            strike=24_300,
            option_type="CE",
        )

    quote = OptionQuote(
        timestamp=datetime(2026, 8, 26, 9, 16, tzinfo=IST),
        strike=24_300,
        option_type="CE",
    )
    with pytest.raises(ValidationError, match="frozen"):
        quote.oi = 1  # type: ignore[misc]


def test_snapshot_requires_timezone_aware_timestamps() -> None:
    """A normalizer dropping snapshot timezone information must be rejected."""
    quote = OptionQuote(
        timestamp=datetime(2026, 8, 26, 9, 16, tzinfo=IST),
        strike=24_300,
        option_type="CE",
    )

    with pytest.raises(ValidationError, match="timezone-aware"):
        OptionChainSnapshot(
            instrument="NIFTY",
            source_timestamp=datetime.fromisoformat("2026-08-26T09:16:00"),
            received_at=datetime(2026, 8, 26, 9, 16, tzinfo=IST),
            spot=24_300,
            expiry=date(2026, 9, 1),
            quotes=(quote,),
        )


def test_instruments_runtime_and_domain_results_are_immutable() -> None:
    """Changing model configuration or results after creation must be impossible."""
    nifty = InstrumentConfig(instrument="NIFTY", lot_size=75)
    assert INSTRUMENTS["NIFTY"].instrument == "NIFTY"
    assert RuntimeSettings().timezone == "Asia/Kolkata"
    assert nifty.lot_size == 75
    assert DataQualityStatus.VALID.value == "VALID"
    assert ExposureResult(net_gex_rupees=1.0, net_dex_rupees=-2.0).gex_crore == 1e-7
    assert ZeroLevelResult(level=None, status=ZeroLevelStatus.NO_SIGN_CHANGE).level is None
    assert ResearchSignal(action=SignalAction.NO_TRADE, reasons=("MODEL_NOT_PROMOTED",)).action is SignalAction.NO_TRADE
