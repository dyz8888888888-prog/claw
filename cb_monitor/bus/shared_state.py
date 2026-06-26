"""
共享状态总线 — 线程安全的"当前态"。

只存当前态，不存历史。历史数据进 ledger。
调度器写，仪表盘读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from domain.models import CandidateRecord, MarketContext, MarketSnapshot, Position


@dataclass(slots=True)
class RuntimeView:
    """当前运行态快照 (只读)"""
    last_cycle_ts: float = 0.0
    market_ctx: MarketContext | None = None
    snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)
    candidates: list[CandidateRecord] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    state: str = "disabled"
    state_reason: str = ""
    risk_summary: dict = field(default_factory=dict)
    enabled_strategies: tuple[str, ...] = ()


class SharedState:
    """线程安全内存总线"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._view = RuntimeView()

    # ── 读接口 (不加锁快照) ──────────────────

    def get_view(self) -> RuntimeView:
        """返回当前态快照"""
        with self._lock:
            # 返回浅拷贝，避免外部修改污染内部
            return RuntimeView(
                last_cycle_ts=self._view.last_cycle_ts,
                market_ctx=self._view.market_ctx,
                snapshots=dict(self._view.snapshots),
                candidates=list(self._view.candidates),
                positions=list(self._view.positions),
                state=self._view.state,
                state_reason=self._view.state_reason,
                risk_summary=dict(self._view.risk_summary),
                enabled_strategies=self._view.enabled_strategies,
            )

    def get_snapshot(self, code: str) -> MarketSnapshot | None:
        """返回单只快照"""
        with self._lock:
            return self._view.snapshots.get(code)

    def get_market_context(self) -> MarketContext | None:
        """返回当前市场上下文"""
        with self._lock:
            return self._view.market_ctx

    # ── 写接口 (加锁) ────────────────────────

    def update_market_ctx(self, market_ctx: MarketContext) -> None:
        with self._lock:
            self._view.market_ctx = market_ctx
            self._view.enabled_strategies = market_ctx.enabled_strategies
            self._view.last_cycle_ts = market_ctx.ts

    def update_snapshots(self, snapshots: dict[str, MarketSnapshot]) -> None:
        with self._lock:
            self._view.snapshots = snapshots
            if snapshots:
                self._view.last_cycle_ts = max(
                    self._view.last_cycle_ts,
                    max(s.ts for s in snapshots.values()),
                )

    def update_candidates(self, candidates: list[CandidateRecord]) -> None:
        with self._lock:
            self._view.candidates = candidates

    def update_positions(self, positions: list[Position]) -> None:
        with self._lock:
            self._view.positions = positions

    def update_state(self, state: str, reason: str = "",
                     risk_summary: dict | None = None) -> None:
        with self._lock:
            self._view.state = state
            self._view.state_reason = reason
            if risk_summary is not None:
                self._view.risk_summary = risk_summary

    # ── 重置 ─────────────────────────────────

    def reset_for_new_day(self) -> None:
        """新交易日重置"""
        with self._lock:
            self._view = RuntimeView()

    # ── 便捷查询 ─────────────────────────────

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._view.snapshots)

    @property
    def position_count(self) -> int:
        with self._lock:
            return len([p for p in self._view.positions if p.state == "open"])
