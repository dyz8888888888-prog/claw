"""
全系统统一数据结构 — 只放数据，不放交易逻辑。

用法: from cb_monitor.domain.models import MarketSnapshot, MarketContext, ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import Decision, HoldingMode, Regime, TradeMode


@dataclass(slots=True)
class MarketSnapshot:
    """单只转债+正股快照 (每3秒刷新)"""
    ts: float
    cb_code: str
    cb_name: str
    cb_price: float
    cb_pct: float                                   # 转债涨跌幅%
    cb_open: float = 0.0
    cb_high: float = 0.0
    cb_low: float = 0.0
    cb_volume: int = 0
    cb_amount: float = 0.0
    cb_bid1: float = 0.0
    cb_ask1: float = 0.0
    cb_bid1_vol: int = 0
    cb_ask1_vol: int = 0
    cb_spread_pct: float = 0.0                      # 点差% = (ask1-bid1)/bid1
    cb_volume_ratio: float = 0.0                    # 量比

    stock_code: str = ""
    stock_name: str = ""
    stock_price: float = 0.0
    stock_pct: float = 0.0

    convert_value: float = 0.0                      # 转股价值
    premium: float = 0.0                            # 溢价率%
    issue_scale: float = 0.0                        # 剩余规模(亿)
    redeem_status: str = ""                         # normal / warning / triggered
    halt_risk: str = ""                             # safe / near_20 / near_30 / halted / unknown
    tags: tuple[str, ...] = ()                      # 结构化标签
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketContext:
    """市场上下文 — 由市场理解层产出，供策略层消费"""
    ts: float
    regime: Regime
    trade_mode: TradeMode
    enabled_strategies: tuple[str, ...]              # 今日启用的策略ID列表
    market_notes: tuple[str, ...] = ()
    total_risk_budget: float = 1.0                   # 总风险预算%
    used_risk_budget: float = 0.0                    # 已用风险预算%
    allow_overnight: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeIntent:
    """交易意图 — 策略evaluate()的产出"""
    ts: float
    strategy_id: str
    strategy_version: str
    cb_code: str
    score: float
    direction: str                                   # "long"
    holding_mode: HoldingMode
    decision: Decision
    reason_codes: tuple[str, ...]                    # 触发原因列表
    reason_text: str = ""
    expected_hold_seconds: int = 0
    expected_edge_bps: float = 0.0                   # 预期优势(基点)
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    trailing_drawdown_pct: float = 0.0
    limit_price: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    """持仓对象 — 由PositionManager管理"""
    position_id: str
    strategy_id: str
    strategy_version: str
    cb_code: str
    cb_name: str
    holding_mode: HoldingMode
    entry_ts: float
    entry_price: float
    qty: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_drawdown_pct: float = 0.0
    max_favorable_pct: float = 0.0                   # 最大浮盈%
    max_adverse_pct: float = 0.0                     # 最大浮亏%
    state: str = "open"                              # open / closed
    exit_ts: float = 0.0                             # 平仓时间
    exit_price: float = 0.0                          # 平仓价格
    exit_reason: str = ""                            # 退出原因
    exit_plan: str = ""                              # 退出计划描述
    tags: tuple[str, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CandidateRecord:
    """候选记录 — 不管做没做都记"""
    ts: float
    cb_code: str
    strategy_id: str
    strategy_version: str
    score: float
    selected: bool                                   # 是否最终被选中执行
    rejected_by: tuple[str, ...] = ()                # 被拒原因
    market_regime: str = ""
    trade_mode: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
