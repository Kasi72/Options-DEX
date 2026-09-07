"""Research-only target labels, baselines, and selective decisions."""

from nifty_signal_engine.signals.baselines import (
    DirectionProbabilities,
    LogisticBaseline,
    RuleBaseline,
)
from nifty_signal_engine.signals.ensemble import combine_predictions
from nifty_signal_engine.signals.regime import (
    MarketRegime,
    RegimeAssessment,
    classify_regime,
)
from nifty_signal_engine.signals.selective import EconomicsAssessment, SelectiveDecision
from nifty_signal_engine.signals.service import allowed_actions

__all__ = [
    "DirectionProbabilities",
    "EconomicsAssessment",
    "LogisticBaseline",
    "MarketRegime",
    "RegimeAssessment",
    "RuleBaseline",
    "SelectiveDecision",
    "allowed_actions",
    "classify_regime",
    "combine_predictions",
]
