"""
风险引擎 — 判断"能不能做"，不是"值不值得做"。

输入: 账户状态摘要 + 策略意图 + 交易模式
输出: RiskResult (ALLOW / DENY / COOLDOWN + 被拒原因)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.enums import HoldingMode, RiskCheck, TradeMode
from domain.models import MarketContext, Position, TradeIntent


@dataclass(slots=True)
class RiskLimits:
    """风控硬边界 — 按模式有不同默认值。百分数点: 2.0 = 2%"""
    max_positions: int = 2
    max_single_position_weight: float = 0.5         # 单仓最大占比
    max_daily_loss_pct: float = 2.0                 # 单日最大亏损 2%
    max_consecutive_losses: int = 3                  # 连亏上限
    per_cb_daily_trades: int = 1                     # 同债单日最多交易次数
    cooldown_minutes_after_loss: int = 15            # 亏损后冷却分钟


@dataclass(slots=True)
class RiskResult:
    """风控检查结果"""
    status: RiskCheck
    reason_codes: tuple[str, ...] = ()
    cooldown_until_ts: float = 0.0


class RiskEngine:
    """风控引擎 — 每次开仓/持仓都过一遍"""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    # ── 按模式的限制覆盖 ──

    MODE_LIMITS = {
        TradeMode.ATTACK:   {"max_positions": 2, "max_daily_loss_pct": 2.5, "allow_overnight": True},
        TradeMode.PROBE:    {"max_positions": 1, "max_daily_loss_pct": 2.0, "allow_overnight": False},
        TradeMode.DEFENSE:  {"max_positions": 1, "max_daily_loss_pct": 1.5, "allow_overnight": False},
        TradeMode.DISABLED: {"max_positions": 0, "max_daily_loss_pct": 0,   "allow_overnight": False},
    }

    def _mode_limits(self, mode: TradeMode) -> dict:
        """合并全局限制 + 模式覆盖"""
        base = {
            "max_positions": self.limits.max_positions,
            "max_daily_loss_pct": self.limits.max_daily_loss_pct,
            "allow_overnight": False,
        }
        base.update(self.MODE_LIMITS.get(mode, {}))
        return base

    # ── 主要检查方法 ──────────────────────────

    def check_new_trade(
        self,
        intent: TradeIntent,
        market_ctx: MarketContext,
        account_ctx: dict,
        positions: list[Position],
    ) -> RiskResult:
        """
        检查是否允许开新仓。

        account_ctx 约定:
          - daily_pnl_pct: float    当日累计盈亏%
          - consecutive_losses: int 连续亏损次数
          - used_cb_trade_count: dict[str, int]  单债当日已交易次数
          - available_risk_budget: float  剩余风险预算
          - now_ts: float
        """
        reasons: list[str] = []

        now_ts = account_ctx.get("now_ts", 0)
        daily_pnl = account_ctx.get("daily_pnl_pct", 0)
        consecutive_losses = account_ctx.get("consecutive_losses", 0)
        cb_trade_count = account_ctx.get("used_cb_trade_count", {})
        available_budget = account_ctx.get("available_risk_budget", 1.0)

        ml = self._mode_limits(market_ctx.trade_mode)

        # 1. 今日亏损超限 → DENY
        max_loss = ml["max_daily_loss_pct"]
        if daily_pnl <= -max_loss:
            reasons.append("daily_loss_limit")

        # 2. 连亏超过上限 → COOLDOWN
        if consecutive_losses >= self.limits.max_consecutive_losses:
            return RiskResult(
                RiskCheck.COOLDOWN,
                ("consecutive_losses",),
                now_ts + self.limits.cooldown_minutes_after_loss * 60,
            )

        # 3. 当前持仓数超限 → DENY
        active = [p for p in positions if p.state == "open"]
        if len(active) >= ml["max_positions"]:
            reasons.append("max_positions")

        # 4. 单债当日交易次数超限 → DENY
        cb_trades = cb_trade_count.get(intent.cb_code, 0)
        if cb_trades >= self.limits.per_cb_daily_trades:
            reasons.append(f"per_cb_limit:{intent.cb_code}")

        # 5. 风险预算不足 → DENY
        if available_budget <= 0:
            reasons.append("budget_exhausted")

        # 6. 隔夜策略检查 → DENY
        if intent.holding_mode == HoldingMode.OVERNIGHT_CARRY and not ml["allow_overnight"]:
            reasons.append("overnight_not_allowed")

        if reasons:
            return RiskResult(RiskCheck.DENY, tuple(reasons))

        return RiskResult(RiskCheck.ALLOW)

    def check_holdings(
        self,
        market_ctx: MarketContext,
        account_ctx: dict,
        positions: list[Position],
    ) -> RiskResult:
        """
        检查当前持仓是否有风险 (不单独检查某一只)。

        如果触发全局风控条件，返回 DENY 或 COOLDOWN，
        调度器应根据此结果强制平仓或进入冷却。
        """
        reasons: list[str] = []
        now_ts = account_ctx.get("now_ts", 0)
        daily_pnl = account_ctx.get("daily_pnl_pct", 0)
        consecutive_losses = account_ctx.get("consecutive_losses", 0)

        ml = self._mode_limits(market_ctx.trade_mode)

        # 亏损超限 → 强制全部退出
        if daily_pnl <= -ml["max_daily_loss_pct"]:
            reasons.append("daily_loss_limit_forced_exit")

        # 连亏超限 → 强制冷却
        if consecutive_losses >= self.limits.max_consecutive_losses:
            return RiskResult(
                RiskCheck.COOLDOWN,
                ("consecutive_losses_forced",),
                now_ts + self.limits.cooldown_minutes_after_loss * 60,
            )

        if reasons:
            return RiskResult(RiskCheck.DENY, tuple(reasons))

        return RiskResult(RiskCheck.ALLOW)

    # ── 推荐今日模式 ──

    def recommend_day_mode(
        self,
        today_pnl_pct: float,
        consecutive_losses: int,
        last_5day_pnl_pct: float,
    ) -> TradeMode:
        """根据账户状态推荐今日交易模式。百分数点: -3.0 = -3%"""
        if consecutive_losses >= self.limits.max_consecutive_losses:
            return TradeMode.DISABLED
        if last_5day_pnl_pct < -3.0 and consecutive_losses >= 2:
            return TradeMode.DEFENSE
        if today_pnl_pct < -1.0 or consecutive_losses >= 1:
            return TradeMode.PROBE
        if last_5day_pnl_pct > 2.0 and consecutive_losses == 0:
            return TradeMode.ATTACK
        return TradeMode.PROBE
