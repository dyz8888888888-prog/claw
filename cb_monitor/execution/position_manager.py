"""
持仓管理器 — 持仓对象的生命周期。

open:   从成交创建持仓
update:  更新浮盈浮亏轨迹
close:  平仓结算
"""

from __future__ import annotations

import time
import uuid

from domain.models import Position, TradeIntent


class PositionManager:
    """持仓生命周期管理"""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    # ── 开仓 ─────────────────────────────────

    def open_from_fill(
        self,
        intent: TradeIntent,
        fill_price: float,
        qty: int = 1,
        now_ts: float | None = None,
    ) -> Position:
        """
        从成交结果创建持仓对象。

        position_id = "{strategy_id}_{cb_code}_{timestamp}"
        """
        if now_ts is None:
            now_ts = time.time()

        pid = f"{intent.strategy_id}_{intent.cb_code}_{int(now_ts)}"

        pos = Position(
            position_id=pid,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            cb_code=intent.cb_code,
            cb_name="",   # 由上层补
            holding_mode=intent.holding_mode,
            entry_ts=now_ts,
            entry_price=fill_price,
            qty=qty,
            stop_loss_pct=intent.stop_loss_pct,
            take_profit_pct=intent.take_profit_pct,
            trailing_drawdown_pct=intent.trailing_drawdown_pct,
            state="open",
        )

        self._positions[pid] = pos
        return pos

    # ── 更新浮盈浮亏轨迹 ────────────────────

    def update_marks(
        self,
        position: Position,
        current_price: float,
    ) -> Position:
        """
        更新最大浮盈/浮亏。

        调用时机: 每轮主循环获得新快照后
        """
        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100

        if pnl_pct > position.max_favorable_pct:
            position.max_favorable_pct = pnl_pct

        if pnl_pct < position.max_adverse_pct:
            position.max_adverse_pct = pnl_pct

        return position

    # ── 平仓 ─────────────────────────────────

    def close_position(
        self,
        position: Position,
        fill_price: float,
        now_ts: float | None = None,
        reason: str = "",
    ) -> dict:
        """
        平仓并返回结算信息。

        返回: {pnl_pct, gross_pnl, holding_seconds, ...}
        """
        if now_ts is None:
            now_ts = time.time()

        position.state = "closed"
        position.exit_ts = now_ts
        position.exit_price = fill_price
        position.exit_reason = reason

        pnl_pct = (fill_price - position.entry_price) / position.entry_price * 100
        holding_seconds = int(now_ts - position.entry_ts)

        return {
            "position_id": position.position_id,
            "pnl_pct": round(pnl_pct, 4),
            "gross_pnl": round(pnl_pct * position.qty, 4),
            "holding_seconds": holding_seconds,
            "max_favorable": position.max_favorable_pct,
            "max_adverse": position.max_adverse_pct,
            "exit_reason": reason,
        }

    # ── 查询 ─────────────────────────────────

    def get(self, position_id: str) -> Position | None:
        return self._positions.get(position_id)

    def get_all(self) -> list[Position]:
        return list(self._positions.values())

    def get_active(self) -> list[Position]:
        return [p for p in self._positions.values() if p.state == "open"]

    def active_count(self) -> int:
        return len(self.get_active())
