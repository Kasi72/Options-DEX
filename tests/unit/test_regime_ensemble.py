from datetime import date, datetime

import pytest

from nifty_signal_engine.features.pipeline import FeatureRow
from nifty_signal_engine.signals.baselines import DirectionProbabilities
from nifty_signal_engine.signals.ensemble import combine_predictions
from nifty_signal_engine.signals.regime import MarketRegime, classify_regime


def row(values: dict[str, object], *, valid: bool = True) -> FeatureRow:
    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")
    return FeatureRow(
        available_at=stamp, instrument="NIFTY", session_date=date(2026, 8, 26),
        schema_version="2", values=values, quality_codes=() if valid else ("BAD",),
        row_kind="COMPLETED_MINUTE", completed=True,
        bar_start=datetime.fromisoformat("2026-08-26T09:59:00+05:30"), bar_end=stamp,
    )


def test_regime_is_unknown_during_warmup() -> None:
    assert classify_regime(row({})).regime is MarketRegime.UNKNOWN


def test_regime_detects_uptrend() -> None:
    assessment = classify_regime(row({"observed_session_return": 0.012, "opening_range_breakout": 1, "realized_volatility": 0.001}))
    assert assessment.regime is MarketRegime.TREND_UP


def test_ensemble_reports_disagreement() -> None:
    a = DirectionProbabilities(up=0.8, down=0.1, no_move=0.1, instrument="NIFTY")
    b = DirectionProbabilities(up=0.1, down=0.8, no_move=0.1, instrument="NIFTY")
    result, disagreement = combine_predictions([a, b])
    assert result.up == pytest.approx(0.45)
    assert disagreement == pytest.approx(0.7)


def test_ensemble_rejects_mixed_instruments() -> None:
    a = DirectionProbabilities(up=0.8, down=0.1, no_move=0.1, instrument="NIFTY")
    b = DirectionProbabilities(up=0.1, down=0.8, no_move=0.1, instrument="BANKNIFTY")
    with pytest.raises(ValueError, match="same instrument"):
        combine_predictions([a, b])
