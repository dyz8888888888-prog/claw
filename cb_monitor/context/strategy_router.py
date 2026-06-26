"""
策略路由器 — 按市场环境开关策略。

只输出策略ID列表，不依赖策略实例。
"""

from __future__ import annotations

from domain.enums import Regime
from domain.models import MarketContext


class StrategyRouter:
    """策略路由 — regime + trade_mode → 启停策略"""

    # ── 市场环境 → 允许的策略 ──

    REGIME_MAP: dict[Regime, tuple[str, ...]] = {
        Regime.OVERHEAT: ("board_spillover", "volume_follow"),
        Regime.ACTIVE:   ("volume_follow", "board_spillover"),
        Regime.MILD:     ("volume_follow",),
        Regime.EBB:      ("tailwash_overnight",),
        Regime.FREEZE:   (),
    }

    # ── 公共接口 ──────────────────────────────

    def enabled_strategy_ids(self, market_ctx: MarketContext) -> tuple[str, ...]:
        """返回当前应启用的策略ID列表"""
        regime_ids = self.REGIME_MAP.get(market_ctx.regime, ())

        # 防守/暂停日强制禁止隔夜策略
        from domain.enums import TradeMode
        if market_ctx.trade_mode in (TradeMode.DEFENSE, TradeMode.DISABLED):
            # 过滤掉隔夜策略
            overnight_strategies = {"tailwash_overnight"}
            regime_ids = tuple(s for s in regime_ids if s not in overnight_strategies)

        if market_ctx.trade_mode == TradeMode.DISABLED:
            return ()

        return regime_ids

    def apply_to(self, market_ctx: MarketContext) -> MarketContext:
        """将路由结果写回 MarketContext"""
        ids = self.enabled_strategy_ids(market_ctx)
        return MarketContext(
            ts=market_ctx.ts,
            regime=market_ctx.regime,
            trade_mode=market_ctx.trade_mode,
            enabled_strategies=ids,
            market_notes=market_ctx.market_notes,
            total_risk_budget=market_ctx.total_risk_budget,
            used_risk_budget=market_ctx.used_risk_budget,
            allow_overnight=market_ctx.allow_overnight and any("tailwash" in s for s in ids),
            extras=market_ctx.extras,
        )
