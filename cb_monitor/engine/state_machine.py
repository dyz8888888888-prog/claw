"""
交易状态机 — 八态流转的唯一中心。

不做策略判断，不做行情计算。
只做状态转移与门控条件检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.enums import MachineState, TradeMode
from domain.models import MarketContext, Position, TradeIntent


@dataclass(slots=True)
class MachineContext:
    """状态机内部上下文"""
    state: MachineState = MachineState.DISABLED
    active_positions: list[Position] = field(default_factory=list)
    pending_intents: list[TradeIntent] = field(default_factory=list)
    last_reason: str = ""
    cooldown_until_ts: float = 0.0
    day_done: bool = False


class TradingStateMachine:
    """八态交易状态机"""

    def __init__(self) -> None:
        self.ctx = MachineContext()

    # ── 状态转换方法 ──────────────────────────

    def to_disabled(self, reason: str) -> MachineState:
        self.ctx.state = MachineState.DISABLED
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_idle(self, reason: str = "") -> MachineState:
        self.ctx.state = MachineState.IDLE
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_watching(self, reason: str = "") -> MachineState:
        self.ctx.state = MachineState.WATCHING
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_entering(self, reason: str = "") -> MachineState:
        self.ctx.state = MachineState.ENTERING
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_holding(self, reason: str = "") -> MachineState:
        self.ctx.state = MachineState.HOLDING
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_exiting(self, reason: str = "") -> MachineState:
        self.ctx.state = MachineState.EXITING
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_cooldown(self, until_ts: float, reason: str) -> MachineState:
        self.ctx.state = MachineState.COOLDOWN
        self.ctx.cooldown_until_ts = until_ts
        self.ctx.last_reason = reason
        return self.ctx.state

    def to_done(self, reason: str) -> MachineState:
        self.ctx.state = MachineState.DONE
        self.ctx.last_reason = reason
        self.ctx.day_done = True
        return self.ctx.state

    # ── 统一门控 → 状态转换 ───────────────────

    def transition(
        self,
        now_ts: float,
        market_ctx: MarketContext,
        has_candidates: bool,
        has_open_positions: bool,
        risk_allows_new_trade: bool,
        exit_required: bool,
    ) -> MachineState:
        """
        根据当前状态 + 外部条件 → 决定下一个状态。

        DISABLED → IDLE:      trade_mode != disabled 且在交易时段
        IDLE → WATCHING:      允许新交易 且 无冷却
        WATCHING → ENTERING:  有候选 且 风控放行
        ENTERING → HOLDING:   至少一笔成交
        HOLDING → EXITING:    任一持仓需退出
        EXITING → COOLDOWN:   全部离场 且 触发冷却条件
        COOLDOWN → IDLE:      超过冷却时间
        任意 → DONE:          亏损超限 / 交易结束
        """
        state = self.ctx.state

        # DISABLED → IDLE
        if state == MachineState.DISABLED:
            if market_ctx.trade_mode != TradeMode.DISABLED:
                return self.to_idle("交易模式开启")
            return state

        # IDLE → WATCHING
        if state == MachineState.IDLE:
            if now_ts < self.ctx.cooldown_until_ts:
                return state  # 仍在冷却
            if not risk_allows_new_trade:
                return state
            return self.to_watching("开始扫描候选")

        # WATCHING → ENTERING
        if state == MachineState.WATCHING:
            if has_candidates and risk_allows_new_trade:
                return self.to_entering("发现候选目标")
            return state

        # ENTERING → HOLDING
        if state == MachineState.ENTERING:
            if has_open_positions:
                return self.to_holding("进场成功")
            return state

        # HOLDING → EXITING
        if state == MachineState.HOLDING:
            if exit_required:
                return self.to_exiting("触发退出条件")
            return state

        # EXITING → COOLDOWN 或 IDLE
        if state == MachineState.EXITING:
            if not has_open_positions:
                # 全部离场后判断是否需要冷却
                if market_ctx.trade_mode in (TradeMode.PROBE, TradeMode.DEFENSE):
                    return self.to_cooldown(
                        now_ts + 900,  # 冷却15分钟
                        "策略要求离场后冷却"
                    )
                return self.to_idle("持仓已清")
            return state

        # COOLDOWN → IDLE
        if state == MachineState.COOLDOWN:
            if now_ts >= self.ctx.cooldown_until_ts:
                return self.to_idle("冷却结束")
            return state

        # DONE 不可逆
        if state == MachineState.DONE:
            return state

        return state

    # ── 便捷方法 ──────────────────────────────

    @property
    def current(self) -> MachineState:
        return self.ctx.state

    @property
    def reason(self) -> str:
        return self.ctx.last_reason

    @property
    def is_trading_allowed(self) -> bool:
        """当前是否允许发起新交易"""
        return self.ctx.state in (MachineState.IDLE, MachineState.WATCHING)

    def force_done(self, reason: str) -> MachineState:
        """强制终止当日交易 (亏损超限/熔断)"""
        return self.to_done(reason)

    def snapshot(self) -> dict:
        """返回当前状态快照 (供仪表盘读取)"""
        return {
            "state": self.ctx.state,
            "reason": self.ctx.last_reason,
            "cooldown_until": self.ctx.cooldown_until_ts,
            "day_done": self.ctx.day_done,
            "position_count": len(self.ctx.active_positions),
        }
