"""
券商仿真 — 模拟委托与成交。

对上层提供统一的 submit_entry / submit_exit 接口。
内部调用 FillModel，返回 FillResult。
不维护持仓表。
"""

from __future__ import annotations

from domain.models import MarketSnapshot, Position, TradeIntent
from replay.fill_model import FillModel, FillResult


class BrokerSim:
    """模拟券商 — 委托 → 成交"""

    def __init__(self, fill_model: FillModel | None = None) -> None:
        self.fill_model = fill_model or FillModel()

    def submit_entry(
        self,
        intent: TradeIntent,
        snapshot: MarketSnapshot,
        qty: int = 1,
    ) -> FillResult:
        """
        提交买入委托。

        使用 intent.limit_price (若有) 作为限价。
        """
        return self.fill_model.simulate_buy(
            snapshot,
            qty,
            limit_price=intent.limit_price,
        )

    def submit_exit(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        qty: int | None = None,
    ) -> FillResult:
        """
        提交卖出委托。

        默认全平, 可指定部分数量。
        """
        sell_qty = qty or position.qty

        return self.fill_model.simulate_sell(
            snapshot,
            sell_qty,
            limit_price=None,
        )
