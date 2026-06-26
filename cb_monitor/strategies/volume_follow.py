"""
放量跟随策略 — 第一个实装日内策略

定位: intraday_flat, 60-300秒动量跟随
只在温和/发酵/高潮市场启用

evaluate(): 检查放量真实性、流动性、临停风险 → TradeIntent
manage():   硬止损/移动止盈/超时退出 → ManageAction
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from domain.enums import Decision, HoldingMode, Regime
from domain.models import MarketContext, MarketSnapshot, Position, TradeIntent
from strategies.base import ManageAction, StrategyBase


@dataclass(slots=True)
class VolumeFollowParams:
    """放量跟随策略参数 — 可配置、可版本化"""
    min_cb_volume_ratio: float = 3.0             # 最低量比
    min_cb_amount: float = 30_000_000            # 最低成交额(元)
    min_cb_pct: float = 0.3                      # 最低涨幅%
    max_cb_spread_pct: float = 0.25              # 最大点差%
    reject_halt_risk: tuple[str, ...] = ("halted", "near_30")
    allowed_regimes: tuple[Regime, ...] = (
        Regime.MILD,
        Regime.ACTIVE,
        Regime.OVERHEAT,
    )
    max_hold_seconds: int = 300                  # 最长持有时长
    stop_loss_pct: float = -1.0                  # 硬止损
    take_profit_pct: float = 1.5                 # 硬止盈
    trailing_drawdown_pct: float = 0.5           # 移动止盈回撤


class VolumeFollowStrategy(StrategyBase):
    """放量跟随策略"""

    strategy_id = "volume_follow"
    strategy_version = "0.1.0"
    holding_mode = HoldingMode.INTRADAY_FLAT.value

    def __init__(self, params: VolumeFollowParams | None = None) -> None:
        self.params = params or VolumeFollowParams()

    # ── 启停检查 ─────────────────────────────

    def enabled(self, market_ctx: MarketContext) -> bool:
        """当前市场是否允许此策略"""
        regime_ok = market_ctx.regime in self.params.allowed_regimes
        strategy_enabled = self.strategy_id in market_ctx.enabled_strategies
        return regime_ok and strategy_enabled

    # ── 候选评估 ─────────────────────────────

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        market_ctx: MarketContext,
        account_ctx: dict,
    ) -> TradeIntent | None:
        """
        评估单只转债 → 放量跟随机会?

        硬性条件 (任一不满足 → None):
          1. 成交额 >= 3000万
          2. 量比 >= 3.0x
          3. 转债涨幅 > 0 (不放量砸盘)
          4. 点差 <= 0.25%
          5. halt_risk 不在拒绝列表
        """
        p = self.params
        now_ts = time.time()

        # 1. 成交额过滤
        if snapshot.cb_amount < p.min_cb_amount:
            return None

        # 2. 量比过滤
        if snapshot.cb_volume_ratio < p.min_cb_volume_ratio:
            return None

        # 3. 方向过滤 (不放量下跌)
        if snapshot.cb_pct <= 0:
            return None

        # 4. 点差过滤 (流动性检查)
        if snapshot.cb_spread_pct > p.max_cb_spread_pct:
            return None

        # 5. 临停风险
        if snapshot.halt_risk in p.reject_halt_risk:
            return None

        # ── 评分 ──
        vol_score = min(snapshot.cb_volume_ratio / p.min_cb_volume_ratio * 30, 40)
        pct_score = min(snapshot.cb_pct / 1.5 * 30, 30)
        liq_score = max(0, 30 - snapshot.cb_spread_pct * 100)
        score = round(vol_score + pct_score + liq_score, 1)

        # ── 构建意图 ──
        return TradeIntent(
            ts=now_ts,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            cb_code=snapshot.cb_code,
            score=score,
            direction="long",
            holding_mode=self.holding_mode,
            decision=Decision.ENTER,
            reason_codes=("volume_surge",),
            reason_text=f"放量{snapshot.cb_volume_ratio:.1f}x 涨{snapshot.cb_pct:.1f}% 点差{snapshot.cb_spread_pct:.2f}%",
            expected_hold_seconds=p.max_hold_seconds,
            expected_edge_bps=round(snapshot.cb_pct * 100, 0),
            stop_loss_pct=p.stop_loss_pct,
            take_profit_pct=p.take_profit_pct,
            trailing_drawdown_pct=p.trailing_drawdown_pct,
        )

    # ── 持仓管理 ─────────────────────────────

    def manage(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        market_ctx: MarketContext,
        account_ctx: dict,
    ) -> ManageAction:
        """
        管理持仓 → 退出条件:

        1. 硬止损: 当前亏损 < stop_loss_pct
        2. 硬止盈: 当前盈利 > take_profit_pct
        3. 移动止盈: 从最高点回撤 > trailing_drawdown_pct
        4. 超时: 持有时间 > max_hold_seconds
        5. 量比衰减: cb_volume_ratio < 1.5
        """
        now_ts = time.time()
        p = self.params

        entry_price = position.entry_price
        current_price = snapshot.cb_price
        current_pnl_pct = (current_price - entry_price) / entry_price * 100

        # 1. 硬止损
        if current_pnl_pct <= p.stop_loss_pct:
            return ManageAction("exit", f"硬止损 {current_pnl_pct:.1f}%")

        # 2. 硬止盈
        if current_pnl_pct >= p.take_profit_pct:
            return ManageAction("exit", f"止盈 {current_pnl_pct:.1f}%")

        # 3. 移动止盈 (从最高点回撤)
        if position.max_favorable_pct > 0:
            drawdown = position.max_favorable_pct - current_pnl_pct
            if drawdown >= p.trailing_drawdown_pct:
                return ManageAction(
                    "exit",
                    f"移动止盈 最高{position.max_favorable_pct:.1f}% 现{current_pnl_pct:.1f}% 回撤{drawdown:.1f}%",
                )

        # 4. 超时退出
        hold_seconds = int(now_ts - position.entry_ts)
        if hold_seconds >= p.max_hold_seconds:
            return ManageAction("exit", f"超时 {hold_seconds}s")

        # 5. 量比衰减 (动量衰竭)
        if snapshot.cb_volume_ratio < 1.5:
            return ManageAction("exit", f"量比衰减至{snapshot.cb_volume_ratio:.1f}x")

        return ManageAction("hold", "继续持有")
