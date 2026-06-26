"""
事件回放器 — 按时间顺序重放历史行情事件。

不耦合策略逻辑，只提供"按时间步进"的能力。
上层通过 on_event 回调注入策略/状态机。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from domain.models import MarketSnapshot


@dataclass(slots=True)
class ReplayEvent:
    """单条回放事件"""
    ts: float
    cb_code: str
    snapshot: MarketSnapshot


class EventReplayer:
    """按时间顺序回放行情事件"""

    def iter_events(
        self,
        snapshots: list[MarketSnapshot],
    ) -> list[ReplayEvent]:
        """
        输入: 一批快照
        输出: 按 ts 排序的 ReplayEvent 列表
        """
        events = [
            ReplayEvent(ts=s.ts, cb_code=s.cb_code, snapshot=s)
            for s in snapshots
        ]
        events.sort(key=lambda e: e.ts)
        return events

    def replay(
        self,
        events: list[ReplayEvent],
        on_event: Callable[[ReplayEvent], None],
    ) -> None:
        """
        逐条回放事件，每条事件触发一次 on_event 回调。

        上层在 on_event 中可以:
          - 更新快照到 shared_state
          - 调策略 evaluate/manage
          - 写账本
        """
        for event in events:
            on_event(event)
