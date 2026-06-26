"""
评分引擎 — 行情类型感知版
根据 market_classifier 的行情类型动态调整概念分/候选分权重

候选分构成 (5因子 + 加分项):
  振幅 25% + 规模 20% + 流动性 20% + 溢价 15% + 方向 20% + 加分项
  注: 2026-06-16 V7 核心理念升级：从"安全优先"改为"波动优先"
      振幅大=有交易机会，方向因子顺势而为，溢价值降为约束而非主导
  加分项:
    微盘(scale<2亿) +15 / 小盘(scale<5亿) +8
    活跃(amp>10%) +15 / 较活跃(amp>5%) +8
    超低溢价(prem<5%) +10 / 低溢价(prem<10%) +5

概念分构成:
  统一基准 40 分（待接入 i问财概念数据后升级为 Σ概念加权分/√n）

妖债判定 (满足任一即标记):
  溢价率 > 100%（高溢价脱钩正股）
  价格 > 800 元（超高价，交易特征异于普通CB）

最终分 = 候选分 × 候选权重 + 概念分 × 概念权重 (权重由行情类型决定)

注意: 实际评分逻辑在 tdx_pipeline.py → compute_candidate_scores_tdx()
      scorer.py 为旧版兼容保留，勿直接使用其 compute_candidate_scores()
"""

import math
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from market_classifier import (
    MarketType, MarketClassifier, MarketSnapshot,
    ClassificationResult, get_weight_config,
)


# ============================================================
# 默认权重配置（无行情分类时使用）
# ============================================================
DEFAULT_CONCEPT_WEIGHT = 0.70
DEFAULT_CANDIDATE_WEIGHT = 0.30


@dataclass
class CandidateScore:
    """单个候选标的的评分明细"""
    name: str
    code: str
    candidate_score: float  # 候选分（基本面）
    concept_score: float  # 概念分（题材热度）
    final_score: float  # 最终评分（加权后）
    premium: float  # 溢价率%
    scale: float  # 剩余规模 亿 (来自东方财富 mx-finance-data)
    price: float  # 价格 元
    amp: float  # 振幅%
    amount: float  # 成交额 元
    concepts: List[str] = field(default_factory=list)
    has_forced_redemption: bool = False  # 是否强赎


@dataclass
class ScoringResult:
    """完整评分结果"""
    market_result: ClassificationResult
    concept_weight: float
    candidate_weight: float
    candidates: List[CandidateScore]
    top_n: int = 10


class AdaptiveScorer:
    """自适应评分器：根据行情类型动态调整权重"""

    def __init__(self, classifier: MarketClassifier = None):
        self.classifier = classifier or MarketClassifier()

    def compute_weights(
        self, market_result: ClassificationResult
    ) -> tuple:
        """从行情分类结果计算权重"""
        config = market_result.config
        concept_w = config.get("concept_weight", DEFAULT_CONCEPT_WEIGHT)
        candidate_w = config.get("candidate_weight", DEFAULT_CANDIDATE_WEIGHT)
        return concept_w, candidate_w

    def classify_and_score(
        self,
        snap: MarketSnapshot,
        candidates: List[CandidateScore],
    ) -> ScoringResult:
        """一步完成：行情分类 + 动态评分"""
        # 1. 行情分类
        market_result = self.classifier.classify(snap)

        # 2. 获取动态权重
        concept_w, candidate_w = self.compute_weights(market_result)

        # 3. 重新计算最终评分
        for c in candidates:
            c.final_score = (
                c.candidate_score * candidate_w + c.concept_score * concept_w
            )

        # 4. 按最终评分排序
        candidates.sort(key=lambda x: x.final_score, reverse=True)

        return ScoringResult(
            market_result=market_result,
            concept_weight=concept_w,
            candidate_weight=candidate_w,
            candidates=candidates,
        )

    def get_top_n(
        self, result: ScoringResult, n: int = 10
    ) -> List[CandidateScore]:
        """返回TOP N"""
        return result.candidates[:n]

    def get_strategy_advice(self, market_result: ClassificationResult) -> str:
        """根据行情类型给出策略建议"""
        mt = market_result.market_type

        strategies = {
            MarketType.A: (
                "强势普涨日，概念驱动极强。建议：\n"
                "  1. 涨停扩散表权重最高，重点关注新增涨停概念\n"
                "  2. 溢价率容忍度可适当放宽至50%\n"
                "  3. 穿透指标（正股涨停→CB跟涨）有效性最高\n"
                "  4. 下午可能出现冲高回落，注意14:15复核"
            ),
            MarketType.B: (
                "温和偏强日，板块轮动中。建议：\n"
                "  1. 关注涨停扩散表中涨幅 TOP5 的概念\n"
                "  2. 溢价率控制在30%以内\n"
                "  3. 优先选择正股已涨停或即将涨停的标的"
            ),
            MarketType.C: (
                "微涨分化日，概念信号有噪音。建议：\n"
                "  1. 概念分权重降至50%，与候选分并重\n"
                "  2. 必须检查正股方向是否确认（涨停或放量拉升）\n"
                "  3. 仅推荐溢价率<15%的标的"
            ),
            MarketType.D: (
                "横盘震荡日，概念基本失效。建议：\n"
                "  1. 概念分权重降至30%，以基本面为主\n"
                "  2. 优先选择微盘（<2亿）+ 低溢价（<10%）标的\n"
                "  3. TOP10 改为技术面排名（量比、振幅）"
            ),
            MarketType.E: (
                "微跌分化日，防御为主。建议：\n"
                "  1. 概念分不参与评分（权重0%）\n"
                "  2. 仅推荐：低溢价（<5%）+ 微盘（<1.5亿）+ 无强赎\n"
                "  3. TOP10 标题加 [防御观察]，附风险提示"
            ),
            MarketType.F: (
                "弱势普跌日，全面防御。建议：\n"
                "  1. 概念分不参与评分\n"
                "  2. 不推荐任何标的，仅做风险预警\n"
                "  3. 日报改为 市场风险简报 格式"
            ),
        }
        return strategies.get(mt, "未知行情类型，使用默认策略")


# ============================================================
# 核心评分函数（唯一真相源）
# ============================================================

def _normalize(values: List[float], reverse: bool = True) -> List[float]:
    """Min-max 归一化到 [0, 100]。reverse=True 时值越小得分越高"""
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    result = [(v - mn) / (mx - mn) * 100 for v in values]
    return [100 - r for r in result] if reverse else result


def is_demon_bond(premium: float, price: float) -> bool:
    """妖债判定: 溢价>100% 或 价格>800元"""
    return premium > 100 or price > 800


def compute_candidate_scores(
    raw_items: List[dict],
    demon_threshold_price: float = 800.0,
    demon_threshold_premium: float = 100.0,
) -> tuple:
    """
    从 push2 API 原始数据 + _scale 字段计算候选分。

    返回: (normal_candidates: List[CandidateScore], demon_candidates: List[dict])

    权重:
      - scale_score:    30% (剩余规模越小越好，弹性越大)
      - premium_score:  35% (溢价越低越好)
      - amount_score:   20% (流动性越好越好，log10)
      - amp_score:      15% (振幅越大越好)
      - 加分项: 微盘(scale<2亿) +15, 小盘(scale<5亿) +8
                超低溢价(prem<5%) +25, 低溢价(prem<10%) +15

    妖债（溢价>100% 或 价格>800）不参与正常评分，单独返回。
    概念分暂统一为 40，待接入 i问财后升级。

    需要 raw_items 中每个元素包含 _scale 字段（来自 mx-finance-data 的未转股余额）。
    """
    valid = []
    demon_raw = []
    for item in raw_items:
        try:
            premium = float(item.get('f237', 999))
            price = float(item.get('f2', 999))
            amp = float(item.get('f7', 0) or 0)
            amount = float(item.get('f6', 0) or 0)
            scale = float(item.get('_scale', -1))
            if premium == 999 or price == 999 or price <= 0:
                continue
            if scale < 0:
                continue  # 没有规模数据的跳过
            entry = {
                'code': str(item.get('f12', '')),
                'name': str(item.get('f14', '')),
                'price': price,
                'premium': premium,
                'scale': scale,
                'amp': amp,
                'amount': amount,
                'pct': float(item.get('f3', 0) or 0),
            }
            if is_demon_bond(premium, price):
                demon_raw.append(entry)
            else:
                valid.append(entry)
        except (ValueError, TypeError):
            continue

    # 妖债不参与评分，直接列出基本信息
    demon_candidates = [
        {
            'name': d['name'], 'code': d['code'],
            'price': d['price'], 'premium': d['premium'],
            'reason': '超高溢价' if d['premium'] > demon_threshold_premium else '超高价妖债',
            'has_forced_redemption': False,
        }
        for d in demon_raw
    ]

    if not valid:
        return [], demon_candidates

    # 各维度归一化
    premiums = [c['premium'] for c in valid]
    scales = [c['scale'] for c in valid]
    amps = [c['amp'] for c in valid]
    amounts = [c['amount'] for c in valid]

    prem_scores = _normalize(premiums, reverse=True)    # 低溢价→高分
    scale_scores = _normalize(scales, reverse=True)      # 小规模→高分（弹性大）
    amp_scores = _normalize(amps, reverse=False)         # 高振幅→高分
    amt_scores = _normalize([math.log10(a + 1) for a in amounts], reverse=False)

    candidates = []
    for i, cb in enumerate(valid):
        # 加分项（基于真实规模）
        scale_bonus = 15 if cb['scale'] < 2 else (8 if cb['scale'] < 5 else 0)
        prem_bonus = 25 if cb['premium'] < 5 else (15 if cb['premium'] < 10 else 0)

        cand_score = (
            prem_scores[i] * 0.35          # 溢价率: 最核心因子
            + scale_scores[i] * 0.30       # 剩余规模: 弹性
            + amt_scores[i] * 0.20          # 流动性
            + amp_scores[i] * 0.15          # 振幅
            + scale_bonus                   # 微盘/小盘加分
            + prem_bonus                    # 超低溢价奖励
        )
        cand_score = round(cand_score, 1)

        candidates.append(CandidateScore(
            name=cb['name'],
            code=cb['code'],
            candidate_score=cand_score,
            concept_score=40.0,
            final_score=cand_score,
            premium=cb['premium'],
            scale=cb['scale'],
            price=cb['price'],
            amp=cb['amp'],
            amount=cb['amount'],
        ))

    # 按候选分排序
    candidates.sort(key=lambda x: x.candidate_score, reverse=True)
    return candidates, demon_candidates


def load_and_score(json_path: str) -> tuple:
    """便捷函数: 从 JSON 文件加载数据并评分。返回 (normal, demon)"""
    with open(json_path, 'r', encoding='utf-8') as f:
        items = json.load(f)
    return compute_candidate_scores(items)


# ============================================================
# 便捷函数
# ============================================================
def build_snapshot_from_market_data(
    snapshot_time: str,
    up_count: int,
    down_count: int,
    limit_up_count: int,
    limit_down_count: int,
    index_change_pct: float,
    volume_ratio: Optional[float] = None,
    prev_type: Optional[MarketType] = None,
) -> MarketSnapshot:
    """从原始市场数据构建 MarketSnapshot"""
    return MarketSnapshot(
        snapshot_time=snapshot_time,
        up_count=up_count,
        down_count=down_count,
        limit_up_count=limit_up_count,
        limit_down_count=limit_down_count,
        index_change_pct=index_change_pct,
        volume_ratio=volume_ratio,
        prev_type=prev_type,
    )


# ============================================================
# 行情类型决策表（供日报生成时查阅）
# ============================================================
def print_decision_table():
    """打印行情类型决策表"""
    print("=" * 80)
    print("行情类型决策表")
    print("=" * 80)
    print(f"{'类型':<4} {'名称':<10} {'概念权重':<8} {'候选权重':<8} {'策略'}")
    print("-" * 80)
    for mt in MarketType:
        cfg = get_weight_config(mt)
        print(
            f"{mt.value:<4} "
            f"{cfg['market_name']:<10} "
            f"{cfg['concept_weight']:<8.0%} "
            f"{cfg['candidate_weight']:<8.0%} "
            f"{cfg['strategy']}"
        )
    print("=" * 80)


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    print_decision_table()
    print()

    scorer = AdaptiveScorer()

    # 模拟：09:40 偏强方向
    snap_0940 = build_snapshot_from_market_data(
        snapshot_time="09:40",
        up_count=3200, down_count=1200,
        limit_up_count=20, limit_down_count=2,
        index_change_pct=0.5,
    )
    r = scorer.classifier.classify(snap_0940)
    print(f"09:40 行情: {r.market_type.value}型 {r.market_type.name}")
    print(f"  权重: 概念{scorer.compute_weights(r)[0]:.0%} / 候选{scorer.compute_weights(r)[1]:.0%}")
    print()

    # 模拟：10:00 强势确认
    snap_1000 = build_snapshot_from_market_data(
        snapshot_time="10:00",
        up_count=3800, down_count=800,
        limit_up_count=45, limit_down_count=1,
        index_change_pct=1.2, volume_ratio=1.3,
        prev_type=MarketType.B,
    )
    r2 = scorer.classifier.classify(snap_1000)
    print(f"10:00 行情: {r2.market_type.value}型 {r2.market_type.name}")
    print(f"  权重: 概念{scorer.compute_weights(r2)[0]:.0%} / 候选{scorer.compute_weights(r2)[1]:.0%}")
    print()

    # 模拟：10:32 震荡
    snap_1032 = build_snapshot_from_market_data(
        snapshot_time="10:32",
        up_count=2300, down_count=2100,
        limit_up_count=25, limit_down_count=5,
        index_change_pct=-0.1, volume_ratio=0.9,
    )
    r3 = scorer.classifier.classify(snap_1032)
    print(f"10:32 行情: {r3.market_type.value}型 {r3.market_type.name}")
    print(f"  权重: 概念{scorer.compute_weights(r3)[0]:.0%} / 候选{scorer.compute_weights(r3)[1]:.0%}")
    print()
    print(scorer.get_strategy_advice(r3))
