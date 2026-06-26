"""
市场环境分类器 — 回答"今天什么钱好赚"。

从六维情绪数据产出 MarketContext。
不挑个股，不做交易决策。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from domain.enums import Regime, TradeMode
from domain.models import MarketContext


@dataclass(slots=True)
class RegimeInput:
    """市场情绪输入 (来自六维情绪引擎 / KPL)"""
    ts: float = 0.0
    limit_up: int = 0              # 涨停数
    broken_limit: int = 0          # 炸板数
    up_down_ratio: float = 1.0     # 涨跌比
    promotion_rate: float = 0.0    # 连板晋级率%
    pool_up_ratio: float = 1.0     # 转债池涨跌比
    turnover_yi: float = 0.0       # 成交额(亿)
    notes: tuple[str, ...] = ()


class MarketRegimeClassifier:
    """市场环境分类 — 先做简单规则版，后续可升级 ML"""

    # ── regime 分类规则 ──

    @staticmethod
    def _classify_regime(inp: RegimeInput) -> Regime:
        """
        六维情绪 → 五档市场环境。

        高潮:  涨停>80, 晋级>40%, 涨跌比>1.5
        发酵:  涨停>50, 晋级>30%, 涨跌比>0.8
        温和:  涨停>25, 涨跌比>0.4
        退潮:  涨跌比<0.4
        冰点:  涨跌比<0.2
        """
        ratio = inp.up_down_ratio

        if inp.limit_up > 80 and inp.promotion_rate > 40 and ratio > 1.5:
            return Regime.OVERHEAT
        if inp.limit_up > 50 and inp.promotion_rate > 30 and ratio > 0.8:
            return Regime.ACTIVE
        if inp.limit_up > 25 and ratio > 0.4:
            return Regime.MILD
        if ratio < 0.2:
            return Regime.FREEZE
        return Regime.EBB

    # ── 策略路由简化(仅用于输出冒泡) ──

    REGIME_STRATEGIES = {
        Regime.OVERHEAT: ("board_spillover", "volume_follow"),
        Regime.ACTIVE:   ("volume_follow", "board_spillover"),
        Regime.MILD:     ("volume_follow",),
        Regime.EBB:      ("tailwash_overnight",),
        Regime.FREEZE:   (),
    }

    # ── 主入口 ──

    def classify(
        self,
        regime_input: RegimeInput,
        trade_mode: TradeMode | None = None,
    ) -> MarketContext:
        """
        从情绪输入 → MarketContext

        若未传入 trade_mode，默认用 PROBE。
        """
        now_ts = regime_input.ts or time.time()
        regime = self._classify_regime(regime_input)

        if trade_mode is None:
            trade_mode = TradeMode.PROBE

        enabled = self.REGIME_STRATEGIES.get(regime, ())
        allow_overnight = trade_mode == TradeMode.ATTACK
        risk_budget = {
            TradeMode.ATTACK: 2.5,
            TradeMode.PROBE: 2.0,
            TradeMode.DEFENSE: 1.5,
            TradeMode.DISABLED: 0.0,
        }.get(trade_mode, 2.0)

        notes: list[str] = list(regime_input.notes)
        notes.append(f"涨停{regime_input.limit_up}炸板{regime_input.broken_limit}涨跌比{regime_input.up_down_ratio:.2f}")

        return MarketContext(
            ts=now_ts,
            regime=regime,
            trade_mode=trade_mode,
            enabled_strategies=tuple(enabled),
            market_notes=tuple(notes),
            total_risk_budget=risk_budget,
            allow_overnight=allow_overnight,
        )
