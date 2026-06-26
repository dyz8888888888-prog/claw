"""
成交仿真 — 第一版简化滑点模型

买: ask1 成交, 超量按额外滑点惩罚
卖: bid1 成交, 超量按额外滑点惩罚
限价: 不满足则不成交
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.models import MarketSnapshot


@dataclass(slots=True)
class FillResult:
    """成交结果"""
    filled: bool
    fill_price: float | None = None
    fill_qty: int = 0
    slippage_cost: float = 0.0           # 滑点成本 (与理论价差)
    reason: str = ""


class FillModel:
    """成交滑点模型 — 第一版简化"""

    # 超一档量时的额外滑点 (bps)
    EXTRA_SLIPPAGE_BPS = 2.0              # 超量部分多加 0.02%
    MAX_QTY_SINGLE_LEVEL = 500            # 一档可容纳手数(估)

    def simulate_buy(
        self,
        snapshot: MarketSnapshot,
        qty: int,
        limit_price: float | None = None,
    ) -> FillResult:
        """
        模拟买入成交。

        - 基础价: ask1
        - 限价: 若 ask1 > limit_price → 不成交
        - 超量: qty > MAX_QTY → 额外滑点
        """
        if snapshot.cb_ask1 <= 0:
            return FillResult(False, reason="无卖一价")

        base_price = snapshot.cb_ask1

        # 限价检查
        if limit_price is not None and base_price > limit_price:
            return FillResult(False, reason=f"ask1={base_price:.2f}>{limit_price:.2f}")

        # 超量滑点
        extra_levels = max(0, qty // self.MAX_QTY_SINGLE_LEVEL)
        slippage = extra_levels * self.EXTRA_SLIPPAGE_BPS / 10000 * base_price
        fill_price = base_price + slippage

        return FillResult(
            filled=True,
            fill_price=fill_price,
            fill_qty=qty,
            slippage_cost=slippage,
            reason="",
        )

    def simulate_sell(
        self,
        snapshot: MarketSnapshot,
        qty: int,
        limit_price: float | None = None,
    ) -> FillResult:
        """
        模拟卖出成交。

        - 基础价: bid1
        - 限价: 若 bid1 < limit_price → 不成交
        - 超量: qty > MAX_QTY → 额外滑点 (向下)
        """
        if snapshot.cb_bid1 <= 0:
            return FillResult(False, reason="无买一价")

        base_price = snapshot.cb_bid1

        # 限价检查
        if limit_price is not None and base_price < limit_price:
            return FillResult(False, reason=f"bid1={base_price:.2f}<{limit_price:.2f}")

        # 超量滑点
        extra_levels = max(0, qty // self.MAX_QTY_SINGLE_LEVEL)
        slippage = extra_levels * self.EXTRA_SLIPPAGE_BPS / 10000 * base_price
        fill_price = base_price - slippage

        return FillResult(
            filled=True,
            fill_price=fill_price,
            fill_qty=qty,
            slippage_cost=slippage,
            reason="",
        )
