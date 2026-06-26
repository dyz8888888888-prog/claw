"""
退出规则引擎 — 纯判断，不改持仓。

每个静态方法返回 (Decision, reason) 或 None。
不同策略调用不同的检查组合。
"""

from __future__ import annotations

import time

from domain.enums import Decision
from domain.models import MarketSnapshot, Position


class ExitRules:
    """退出规则 — 每条规则独立、可组合"""

    @staticmethod
    def check_time_limit(
        position: Position,
        max_hold_seconds: int,
        now_ts: float | None = None,
    ) -> tuple[str, str] | None:
        """超过最长持有时间 → exit"""
        if now_ts is None:
            now_ts = time.time()
        hold = int(now_ts - position.entry_ts)
        if hold >= max_hold_seconds:
            return ("exit", f"超时 {hold}s/{max_hold_seconds}s")
        return None

    @staticmethod
    def check_hard_stop(
        position: Position,
        snapshot: MarketSnapshot,
        stop_loss_pct: float,
    ) -> tuple[str, str] | None:
        """触发硬止损 → exit"""
        pnl = (snapshot.cb_price - position.entry_price) / position.entry_price * 100
        if pnl <= stop_loss_pct:
            return ("exit", f"硬止损 {pnl:.1f}%")
        return None

    @staticmethod
    def check_hard_take_profit(
        position: Position,
        snapshot: MarketSnapshot,
        take_profit_pct: float,
    ) -> tuple[str, str] | None:
        """触发硬止盈 → exit"""
        pnl = (snapshot.cb_price - position.entry_price) / position.entry_price * 100
        if pnl >= take_profit_pct:
            return ("exit", f"止盈 {pnl:.1f}%")
        return None

    @staticmethod
    def check_trail_stop(
        position: Position,
        snapshot: MarketSnapshot,
        trail_drawdown_pct: float,
    ) -> tuple[str, str] | None:
        """移动止盈 — 从最高点回撤超过阈值 → exit"""
        if position.max_favorable_pct <= 0:
            return None
        current_pnl = (snapshot.cb_price - position.entry_price) / position.entry_price * 100
        drawdown = position.max_favorable_pct - current_pnl
        if drawdown >= trail_drawdown_pct:
            return ("exit", f"移动止盈 最高{position.max_favorable_pct:.1f}% 回撤{drawdown:.1f}%")
        return None

    @staticmethod
    def check_momentum_decay(
        snapshot: MarketSnapshot,
        min_volume_ratio: float = 1.5,
    ) -> tuple[str, str] | None:
        """量比衰减 → exit"""
        if snapshot.cb_volume_ratio < min_volume_ratio:
            return ("exit", f"量比衰减至{snapshot.cb_volume_ratio:.1f}x")
        return None

    @staticmethod
    def check_seal_broken(
        seal_intact: bool,
    ) -> tuple[str, str] | None:
        """封板打开 → exit (封板溢出策略专用)"""
        if not seal_intact:
            return ("exit", "封板打开")
        return None

    @staticmethod
    def check_overnight_exit(
        position: Position,
        current_time_hhmm: int,  # 如 930 或 1450
        exit_plan: str = "open",
    ) -> tuple[str, str] | None:
        """隔夜退出检查 (尾盘策略专用)"""
        # 次日9:30开盘走
        if exit_plan == "open" and 930 <= current_time_hhmm <= 935:
            return ("exit", "隔夜次日开盘退出")
        # 次日14:50强制平仓
        if current_time_hhmm >= 1450:
            return ("exit", "隔夜强制平仓")
        return None

    # ── 策略专用组合 ─────────────────────────

    @classmethod
    def for_volume_follow(
        cls,
        position: Position,
        snapshot: MarketSnapshot,
        params: dict,
        now_ts: float | None = None,
    ) -> tuple[str, str]:
        """
        放量跟随策略的退出规则组合。
        优先级: 硬止损 > 超时 > 硬止盈 > 移动止盈 > 量比衰减 > hold
        """
        checks = [
            (cls.check_hard_stop, (position, snapshot, params.get("stop_loss_pct", -1.0))),
            (cls.check_time_limit, (position, params.get("max_hold_seconds", 300), now_ts)),
            (cls.check_hard_take_profit, (position, snapshot, params.get("take_profit_pct", 1.5))),
            (cls.check_trail_stop, (position, snapshot, params.get("trail_drawdown_pct", 0.5))),
            (cls.check_momentum_decay, (snapshot,)),
        ]

        for check_fn, args in checks:
            result = check_fn(*args)
            if result:
                return result

        return ("hold", "")
