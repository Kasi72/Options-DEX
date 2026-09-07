"""Chronological, cost-aware research replay and validation contracts."""

from nifty_signal_engine.backtesting.reporting import (
    PromotionArtifact,
    PromotionPolicy,
    PromotionRegistry,
    build_promotion_artifact,
)
from nifty_signal_engine.backtesting.splits import Fold, PurgedWalkForward
from nifty_signal_engine.backtesting.walk_forward import FoldReport, evaluate_fold

__all__ = [
    "Fold",
    "FoldReport",
    "PromotionArtifact",
    "PromotionPolicy",
    "PromotionRegistry",
    "PurgedWalkForward",
    "build_promotion_artifact",
    "evaluate_fold",
]
