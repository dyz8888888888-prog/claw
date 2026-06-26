"""
策略基类 — 所有策略必须实现的统一接口。

evaluate(): 看一只债 → 返回可选的交易意图
manage():   管一个仓 → 返回持有/减仓/退出

调度器只通过这两个方法驱动策略，不关心策略内部逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.models import MarketContext, MarketSnapshot, Position, TradeIntent


@dataclass(slots=True)
class ManageAction:
    """策略manage()返回的持仓动作"""
    action: str                    # "hold" / "reduce" / "exit"
    reason: str
    limit_price: float | None = None
    reduce_ratio: float = 0.0     # 减仓比例 0.0-1.0


class BaseStrategy(Protocol):
    """策略Protocol — 不强制继承，但所有策略必须符合此接口"""

    strategy_id: str
    strategy_version: str
    holding_mode: str              # "intraday_flat" / "overnight_carry"

    def enabled(self, market_ctx: MarketContext) -> bool:
        """该策略在当前市场上下文中是否允许执行"""
        ...

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        market_ctx: MarketContext,
        account_ctx: dict,
    ) -> TradeIntent | None:
        """
        评估单个标的 → 返回交易意图 (或None表示无机会)
        
        account_ctx 字段约定:
          - current_positions: list[Position]
          - today_trades: int
          - today_pnl_pct: float
          - consecutive_losses: int
        """
        ...

    def manage(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        market_ctx: MarketContext,
        account_ctx: dict,
    ) -> ManageAction:
        """
        管理已有持仓 → 返回持仓动作建议
        
        只允许: hold / reduce / exit
        """
        ...


class StrategyBase:
    """策略抽象基类 — 提供默认 enabled() 实现"""

    strategy_id: str = "base"
    strategy_version: str = "0.0.1"
    holding_mode: str = "intraday_flat"

    def enabled(self, market_ctx: MarketContext) -> bool:
        return self.strategy_id in market_ctx.enabled_strategies
