"""可转债日报 — 行情感知评分系统"""

from .market_classifier import (
    MarketType,
    MarketClassifier,
    MarketSnapshot,
    ClassificationResult,
    get_weight_config,
    format_classification_for_report,
    SNAPSHOT_CAPABILITY,
)
from .scorer import (
    AdaptiveScorer,
    CandidateScore,
    ScoringResult,
    build_snapshot_from_market_data,
)
from .pipeline import ReportGenerator
