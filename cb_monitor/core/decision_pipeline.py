"""
决策管道 — DecisionPipeline

4 阶段判定, 只服务两个策略:
  1. 正股买不到外溢 (主线 → 正股买不到 → 转债滞后 → 风险可控 → 埋伏)
  2. 错杀修复 (急跌 → 逻辑没坏 → 转债超跌 → 情绪修复 → 低吸)

输出: 埋伏 / 卖出 / 不做
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class PipelineStage:
    """单阶段判定结果"""

    MAINLINE_CONFIRMED = "主线确认"
    MAINLINE_WATCH = "主线观察"
    MAINLINE_NONE = "无主线"

    STOCK_UNBUYABLE = "正股买不到"
    STOCK_SURGING = "正股强势拉升"
    STOCK_STRONG = "正股强但可买"
    STOCK_WEAK = "正股不强"

    CB_LAGGING = "滞后"
    CB_SYNC = "同步"
    CB_OVERSHOT = "已超涨"

    RISK_OK = "可做"
    RISK_CAUTION = "谨慎"
    RISK_FORBID = "禁入"


@dataclass
class PipelineDecision:
    """管道最终决策"""
    code: str
    name: str
    concept: str = ""
    action: str = "不做"          # 埋伏 / 卖出 / 不做

    # 4 阶段判定
    mainline: str = ""            # 主线确认 / 观察 / 无
    stock_status: str = ""        # 正股买不到 / 强 / 不强
    cb_status: str = ""           # 滞后 / 同步 / 超涨
    risk_level: str = ""          # 可做 / 谨慎 / 禁入

    # 行情
    cb_pct: float = 0
    stock_pct: float = 0
    premium: float = 0
    amount: float = 0            # 成交额(亿元)

    # 决策参数
    reason: str = ""
    buyer: str = ""
    hold_time: str = ""
    stop_loss_pct: float = -2.0
    take_profit_pct: float = 3.0
    invalid_if: str = ""
    risk_tags: list = field(default_factory=list)
    value_score: float = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            'action': self.action,
            'code': self.code,
            'name': self.name,
            'concept': self.concept,
            'mainline': self.mainline,
            'stock_status': self.stock_status,
            'cb_status': self.cb_status,
            'risk_level': self.risk_level,
            'reason': self.reason,
            'buyer': self.buyer,
            'hold_time': self.hold_time,
            'cb_pct': self.cb_pct,
            'stock_pct': self.stock_pct,
            'premium': self.premium,
            'amount': self.amount,
            'stop_loss_pct': self.stop_loss_pct,
            'take_profit_pct': self.take_profit_pct,
            'invalid_if': self.invalid_if,
            'risk_tags': self.risk_tags,
            'value_score': self.value_score,
            'timestamp': self.timestamp,
        }


class DecisionPipeline:
    """4 阶段决策管道"""

    def __init__(self):
        self._mainline_concepts: set = set()
        self._mainline_watch: set = set()

    def set_mainlines(self, concepts_by_stage: dict[str, int],
                      concept_heats: dict[str, float]):
        """设置当前主线概念"""
        self._mainline_concepts.clear()
        self._mainline_watch.clear()
        for concept, stage in concepts_by_stage.items():
            heat = concept_heats.get(concept, 0)
            if stage >= 3 or (stage >= 2 and heat > 3):
                self._mainline_concepts.add(concept)
            elif stage >= 1:
                self._mainline_watch.add(concept)

    def evaluate(self, cb_code: str, cb_name: str,
                 cb_snap, stock_code: str = "",
                 concepts: list[str] = None,
                 consensus_stage: int = 0,
                 redeem_status: str = "",
                 market_state: str = "ferment",
                 concept_diffusion: dict = None,
                 concept_rrg: dict = None) -> PipelineDecision:
        """对单只转债执行 4 阶段判定
        market_state: climax(高潮)/ferment(发酵)/startup(启动)/retreat(退潮)/freeze(冰点)
        concept_diffusion: concept_name → diffusion% (0-100)
        concept_rrg: concept_name → {rs_ratio, rs_momentum, quadrant}
        """

        concepts = concepts or []
        cb_pct = getattr(cb_snap, 'change_pct', 0) or 0
        stock_pct = getattr(cb_snap, 'stock_change_pct', 0) or 0
        premium = getattr(cb_snap, 'premium_ratio', 0) or 0
        # amount统一为亿元 (snap.amount 存原始元)
        from core.data_fusion import fmt_amount
        amount = (getattr(cb_snap, 'amount', 0) or 0) / 100000000

        decision = PipelineDecision(
            code=cb_code, name=cb_name,
            cb_pct=round(cb_pct, 2),
            stock_pct=round(stock_pct, 2),
            premium=round(premium, 1),
            amount=round(amount, 1),
        )

        # 找最佳概念
        best_concept = ""
        for c in concepts:
            stage = consensus_stage
            if stage >= 1:
                best_concept = c
                break
        decision.concept = best_concept

        # ─── 阶段 0: 强赎禁入 (最高优先级) ───
        if redeem_status in ('已公告强赎', '公告要强赎'):
            decision.action = "不做"
            decision.reason = f"强赎预告/{redeem_status}"
            decision.risk_level = PipelineStage.RISK_FORBID
            decision.risk_tags = ["强赎禁入"]
            decision.value_score = 0
            return decision

        # ─── 阶段 1: 主线判定 ───
        in_mainline = any(c in self._mainline_concepts for c in concepts)
        in_watch = any(c in self._mainline_watch for c in concepts)
        if in_mainline:
            decision.mainline = PipelineStage.MAINLINE_CONFIRMED
        elif in_watch:
            decision.mainline = PipelineStage.MAINLINE_WATCH
        else:
            decision.mainline = PipelineStage.MAINLINE_NONE

        # ─── 阶段 2: 正股判定 ───
        # 区分 10cm 和 20cm 涨停板: 创业板(300)/科创板(688) 为 20cm
        is_20cm = stock_code.startswith(('300', '688'))
        limit_up_line = 19.0 if is_20cm else 9.5
        surging_line = 14.0 if is_20cm else 7.0

        if stock_pct >= limit_up_line:
            decision.stock_status = PipelineStage.STOCK_UNBUYABLE
        elif stock_pct >= surging_line:
            decision.stock_status = PipelineStage.STOCK_SURGING
        elif stock_pct >= 3.0:
            decision.stock_status = PipelineStage.STOCK_STRONG
        elif stock_pct > 1.0:
            decision.stock_status = PipelineStage.STOCK_STRONG
        else:
            decision.stock_status = PipelineStage.STOCK_WEAK

        # ─── 阶段 3: 转债状态(简化, 仅用于卖出判定) ───
        is_overshot = (cb_pct > stock_pct + 5 and cb_pct > 8.0)

        # ─── 阶段 4: 风险判定 ───
        risks = []
        if premium > 80:
            decision.risk_level = PipelineStage.RISK_FORBID
            risks.append(f"溢价{premium:.0f}%")
        elif premium > 40:
            decision.risk_level = PipelineStage.RISK_CAUTION
            risks.append(f"高溢价{premium:.0f}%")
        elif amount < 0.10:  # 成交额<0.10亿元(1000万)
            decision.risk_level = PipelineStage.RISK_CAUTION
            risks.append("流动性差")
        elif cb_pct > 8.0 and cb_pct > stock_pct + 3:
            decision.risk_level = PipelineStage.RISK_CAUTION
            risks.append("转债已超涨")
        else:
            decision.risk_level = PipelineStage.RISK_OK

        decision.risk_tags = risks

        # ─── 综合判定 → action ───
        # 5阶段情绪周期 → 信号开关
        is_climax = (market_state == 'climax')      # 高潮: 涨停≥120
        is_ferment = (market_state == 'ferment')     # 发酵: 60-120
        is_startup = (market_state == 'startup')     # 启动: <60但晋级率回升
        is_retreat = (market_state == 'retreat')     # 退潮: 炸板率高
        is_freeze = (market_state == 'freeze')       # 冰点
        is_bullish = is_climax or is_ferment         # 偏强

        # 概念质量过滤 (扩散指标 + RRG)
        concept_diff = 0
        concept_quadrant = ''
        if decision.concept:
            concept_diff = (concept_diffusion or {}).get(decision.concept, 0)
            concept_quadrant = (concept_rrg or {}).get(decision.concept, {}).get('quadrant', '')
        concept_weak = (concept_diff < 30 or concept_quadrant == '落后')
        concept_strong = (concept_diff >= 60 and concept_quadrant == '领先')

        # 1. 正股涨停外溢 (非冰点可用)
        if (market_state != 'freeze'
                and decision.mainline in (PipelineStage.MAINLINE_CONFIRMED,
                                           PipelineStage.MAINLINE_WATCH)
                and decision.stock_status == PipelineStage.STOCK_UNBUYABLE
                and decision.risk_level == PipelineStage.RISK_OK):
            decision.action = "埋伏"
            decision.reason = f"{decision.concept}主线/正股买不到外溢"
            decision.buyer = "追板散户买不到正股"
            decision.hold_time = "30-120s"
            decision.stop_loss_pct = -2.0
            decision.take_profit_pct = 3.0
            decision.invalid_if = "正股炸板立即取消"
            decision.value_score = 85

        # 2. 板块扩散 (高潮/发酵全开, 启动仅主线确认, 退潮/冰点关闭)
        #    需要正股强势拉升(>=7%) + 转债滞后, 避免弱动量假信号
        elif (not concept_weak
                and (is_bullish or (is_startup and decision.mainline == PipelineStage.MAINLINE_CONFIRMED))
                and decision.stock_status == PipelineStage.STOCK_SURGING
                and decision.risk_level == PipelineStage.RISK_OK):
            decision.action = "埋伏"
            decision.reason = f"{decision.concept}/正股强转债滞后"
            decision.buyer = "板块扩散追涨资金"
            decision.hold_time = "60-180s"
            decision.stop_loss_pct = -1.5
            decision.take_profit_pct = 2.5
            boost = 10 if concept_strong else 0
            decision.value_score = (75 if is_climax else 60) + boost

        # 3. 错杀修复 (退潮/冰点主策略, 高潮关闭, 发酵/启动宽松)
        elif (cb_pct < -2.0 and stock_pct > -3.0
                and decision.risk_level != PipelineStage.RISK_FORBID
                and amount > 0.20
                and (is_ferment or is_startup or is_retreat or is_freeze)
                and (not is_freeze or (stock_pct - cb_pct) > 5.0)
                and (not is_retreat or (stock_pct - cb_pct) > 2.0)):
            decision.action = "埋伏"
            decision.reason = f"错杀修复/转债超跌{cb_pct:+.1f}%"
            decision.buyer = "恐慌修复资金"
            decision.hold_time = "60-180s"
            decision.stop_loss_pct = -3.0
            decision.take_profit_pct = 2.0
            decision.invalid_if = "正股继续下跌取消"
            decision.value_score = {
                'freeze': 75, 'retreat': 65, 'startup': 55, 'ferment': 50
            }.get(market_state, 50)

        # 卖出条件
        elif is_overshot and amount > 0.5:
            decision.action = "卖出"
            decision.reason = "转债已超涨/追涨资金已入场"
            decision.buyer = "获利了结"
            decision.hold_time = "立即"
            decision.value_score = 40

        elif decision.risk_level == PipelineStage.RISK_CAUTION:
            decision.action = "卖出"
            decision.reason = f"风险偏高/{decision.concept or '概念'}"
            decision.buyer = "减仓"
            decision.hold_time = "考虑减持"
            decision.value_score = 30

        # 不做
        else:
            decision.action = "不做"
            decision.reason = "无明确对手盘或风险大于机会"
            decision.buyer = ""
            decision.hold_time = ""
            decision.value_score = 10

        return decision

    def evaluate_batch(self, snapshots: dict,
                       concept_map: dict[str, list[str]],
                       consensus_stages: dict[str, int],
                       redeem_map: dict[str, str] = None,
                       market_state: str = "ferment") -> list[PipelineDecision]:
        """批量判定, 返回按优先级排序的决策列表
        market_state: 从 MarketStateClassifier 读取 (climax/ferment/startup/retreat/freeze)
        """
        redeem_map = redeem_map or {}

        # 读取扩散指标 + RRG (懒加载, 避免循环引用)
        concept_diffusion = {}
        concept_rrg = {}
        try:
            import core.consensus_tracker as ct
            if ct.diffusion:
                concept_diffusion = ct.diffusion.get_all()
            if ct.rrg:
                concept_rrg = ct.rrg._current
        except Exception:
            pass

        decisions = []
        for code, snap in snapshots.items():
            # 流动性过滤: 成交额 < 1亿 的不参与决策
            if getattr(snap, 'amount', 0) < 100_000_000:
                continue
            name = getattr(snap, 'name', '') or code
            concepts = concept_map.get(code, [])
            stage = max((consensus_stages.get(c, 0) for c in concepts), default=0)
            d = self.evaluate(code, name, snap, concepts=concepts,
                              consensus_stage=stage,
                              redeem_status=redeem_map.get(code, ''),
                              market_state=market_state,
                              concept_diffusion=concept_diffusion,
                              concept_rrg=concept_rrg)
            decisions.append(d)

        action_order = {'埋伏': 0, '卖出': 1, '不做': 2}
        decisions.sort(key=lambda d: (action_order.get(d.action, 99), -d.value_score))
        return decisions
