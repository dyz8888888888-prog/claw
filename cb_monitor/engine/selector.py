"""
标的选择器 — 四维打分后输出候选排序。

只排序、不决策。真正的"能不能开仓"由 risk_engine + state_machine 决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.models import CandidateRecord, MarketContext, MarketSnapshot, TradeIntent


@dataclass(slots=True)
class SelectorScore:
    """四维分项评分"""
    cb_code: str
    strategy_id: str
    total_score: float
    opponent_pressure: float      # 对手盘焦虑度
    catalyst_clarity: float       # 催化剂明确性
    execution_quality: float      # 流动性与执行性
    risk_reward: float            # 风险收益比


class Selector:
    """标的选择器 — 只排序，不开仓"""

    # ── 四维评分 ─────────────────────────────

    @staticmethod
    def _score_snapshot(snapshot: MarketSnapshot, intent: TradeIntent) -> SelectorScore:
        """
        对单个候选进行四维打分。

        opponent_pressure: 放量程度 + 波动率 → 对手盘有多焦虑
        catalyst_clarity:  正股方向 + 信号原因数 → 催动力量是否明确
        execution_quality: 点差 + 成交额 → 能不能顺利进出
        risk_reward:       intent的score + 溢价率是否合理
        """
        vol_score = min(snapshot.cb_volume_ratio / 5.0 * 30, 30)
        opp = vol_score + min(abs(snapshot.cb_pct) * 5, 20)

        cat = min(len(intent.reason_codes) * 10, 20) + min(snapshot.stock_pct * 3 if snapshot.stock_pct > 0 else 0, 30)

        spread_penalty = max(0, 30 - snapshot.cb_spread_pct * 100)
        amount_score = min(snapshot.cb_amount / 1e8 * 20, 20)
        exe = spread_penalty + amount_score

        rr = min(intent.score * 0.5, 40) + min(abs(opp), 10)

        total = opp + cat + exe + rr
        return SelectorScore(
            cb_code=snapshot.cb_code,
            strategy_id=intent.strategy_id,
            total_score=round(total, 1),
            opponent_pressure=round(opp, 1),
            catalyst_clarity=round(cat, 1),
            execution_quality=round(exe, 1),
            risk_reward=round(rr, 1),
        )

    # ── 排序 ─────────────────────────────────

    def rank(
        self,
        intents: list[TradeIntent],
        snapshots: dict[str, MarketSnapshot],
        market_ctx: MarketContext,
    ) -> list[CandidateRecord]:
        """
        输入: 一批 TradeIntent + 对应快照
        输出: 按总分排序的 CandidateRecord 列表

        注意: 所有候选的 selected 初始为 False，
        真正选中由外层 risk_engine + state_machine 决定。
        """
        scored: list[tuple[SelectorScore, TradeIntent]] = []

        for intent in intents:
            snap = snapshots.get(intent.cb_code)
            if not snap:
                continue
            ss = self._score_snapshot(snap, intent)
            scored.append((ss, intent))

        # 按总分降序
        scored.sort(key=lambda x: -x[0].total_score)

        records: list[CandidateRecord] = []
        for rank, (ss, intent) in enumerate(scored):
            records.append(CandidateRecord(
                ts=intent.ts,
                cb_code=intent.cb_code,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                score=ss.total_score,
                selected=False,   # 初始未选中
                rejected_by=(),
                market_regime=market_ctx.regime.value,
                trade_mode=market_ctx.trade_mode.value,
            ))

        return records
