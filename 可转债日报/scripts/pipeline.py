"""
行情感知日报生成流水线
将 market_classifier + adaptive scorer 集成到日报生成流程中

每次快照的流水线：
  1. 采集市场数据（涨跌比/涨停数/指数/量能）
  2. 行情分类 → 确定行情类型 A-F
  3. 动态调整评分权重
  4. 生成带有行情类型标注的日报
"""

import json
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional

from market_classifier import (
    MarketType, MarketClassifier, MarketSnapshot,
    ClassificationResult, get_weight_config,
    format_classification_for_report,
)
from scorer import AdaptiveScorer, CandidateScore, build_snapshot_from_market_data


# ============================================================
# 日报输出生成器
# ============================================================

@dataclass
class DailyReport:
    """一份完整的快照日报"""
    date: str
    snapshot_time: str
    market_type: str  # A-F
    market_name: str
    concept_weight: float
    candidate_weight: float
    top10: List[dict]
    recommendations: List[dict]
    concept_diffusion: List[dict]  # 涨停扩散表
    demon_bonds: List[dict]  # 妖债预警
    market_indicators: dict  # 市场指标快照


class ReportGenerator:
    """日报生成器 — 行情类型感知版"""

    def __init__(self, scorer: AdaptiveScorer = None):
        self.scorer = scorer or AdaptiveScorer()

    def generate_header(
        self, date: str, snapshot_time: str, market_result: ClassificationResult
    ) -> str:
        """生成日报头部（含行情类型）"""
        config = market_result.config
        tag = config.get("report_tag", "")
        name = config.get("name", "")
        mt = market_result.market_type.value
        concept_w = config.get("concept_weight", 0.7)
        cand_w = config.get("candidate_weight", 0.3)

        lines = [
            f"# 可转债 x 涨停概念日报",
            f"**日期**: {date}  **快照**: {snapshot_time}  "
            f"**行情**: {tag} {mt}型-{name}  **概念权重**: 概念{concept_w:.0%}/候选{cand_w:.0%}",
            "",
        ]

        # 关键市场指标摘要
        ind = market_result.indicators
        if ind:
            ratio = ind.get("up_down_ratio", 0)
            lu = ind.get("limit_up_count", 0)
            idx = ind.get("index_change", 0)
            vr = ind.get("volume_ratio")
            vol_str = f" 量能{vr:.1%}" if vr else ""
            lines.append(
                f"> 涨跌比 {ratio:.1f}:1 | 涨停 {lu}只 | 指数 {idx:+.2f}%{vol_str} "
                f"| 置信度 {market_result.confidence:.0%}"
            )
            lines.append("")

        lines.append("---")
        return "\n".join(lines)

    def generate_top10_table(self, candidates: List[CandidateScore]) -> str:
        """生成 TOP10 排名表"""
        lines = [
            "## TOP10 排名",
            "",
            "| # | 可转债名称 | 溢价率(%) | 规模(亿) | 价格 | 评分 | 强赎 |",
            "|---|-----------|----------|:--------:|------|------|------|",
        ]
        for i, c in enumerate(candidates[:10], 1):
            name = c.name if c.name else c.code
            forced = "是" if c.has_forced_redemption else "-"
            lines.append(
                f"| {i} | {name} | {c.premium:.1f} | {c.scale:.2f} | {c.price:.0f} | "
                f"{c.final_score:.1f} | {forced} |"
            )
        return "\n".join(lines)

    def generate_recommendations(self, candidates: List[CandidateScore]) -> str:
        """生成自动推荐"""
        lines = [
            "",
            "### 自动推荐",
            "| 转债 | 分 | 溢价 | 规模 | 价格 | 强赎 |",
            "|------|:--:|:----:|:----:|:----:|:----:|",
        ]
        for c in candidates[:5]:
            name = c.name if c.name else c.code
            forced = "是" if c.has_forced_redemption else "-"
            lines.append(
                f"| {name} | {c.final_score:.1f} | {c.premium:.1f}% | "
                f"{c.scale:.2f}亿 | {c.price:.0f} | {forced} |"
            )
        return "\n".join(lines)

    def generate_demon_bonds_section(
        self, demon_bonds: List[dict]
    ) -> str:
        """生成妖债预警板块（溢价>100% 或 价格>800）"""
        if not demon_bonds:
            return ""

        lines = [
            "",
            "### 妖债预警（溢价>100% 或 价格>800，脱离正股、风险极高）",
            "| 转债 | 溢价 | 价格 | 类型 | 强赎 |",
            "|------|:----:|:----:|------|:----:|",
        ]
        for d in demon_bonds:
            dtype = d.get('reason', '高溢价妖债')
            forced = "是" if d.get("has_forced_redemption") else "-"
            lines.append(
                f"| {d['name']} | {d['premium']:.1f}% | "
                f"{d.get('price', 0):.0f} | {dtype} | {forced} |"
            )
        return "\n".join(lines)

    def generate_diffusion_table(
        self, diffusion: List[dict], prev_snapshot: str, curr_snapshot: str
    ) -> str:
        """生成涨停扩散表"""
        if not diffusion:
            return "\n### 涨停扩散\n> 首期快照，无上期数据可对比\n"

        lines = [
            "",
            f"### 涨停扩散",
            f"> {prev_snapshot} -> {curr_snapshot} 变化 TOP10",
            "",
            "| 概念 | 上期 | 本期 | 变化 |",
            "|------|-----|-----|------|",
        ]
        for d in diffusion[:10]:
            lines.append(
                f"| {d['concept']} | {d['prev']:.1f} | {d['curr']:.1f} | "
                f"{d['change']:+.1f} |"
            )
        return "\n".join(lines)

    def generate_footer(
        self, market_result: ClassificationResult
    ) -> str:
        """生成日报尾部（含策略建议）"""
        mt = market_result.market_type

        # 策略建议
        strategies = {
            MarketType.A: "强势普涨日，概念驱动极强。溢价容忍度可放宽，重点关注新增涨停概念。",
            MarketType.B: "温和偏强日，板块轮动中。优先选择正股已涨停标的，溢价控制30%以内。",
            MarketType.C: "微涨分化日，概念信号有噪音。必须确认正股方向，仅推荐溢价<15%标的。",
            MarketType.D: "横盘震荡日，概念基本失效。以基本面为主，优先微盘+低溢价组合。",
            MarketType.E: "微跌分化日，防御为主。仅推荐低溢价+微盘+无强赎标的，附风险提示。",
            MarketType.F: "弱势普跌日，全面防御。不推荐标的，仅做风险预警。",
        }
        advice = strategies.get(mt, "使用默认策略。")

        return (
            f"\n---\n"
            f"> **今日策略**: {advice}\n"
            f"> *行情分类: {mt.value}型-{market_result.config.get('name', '')} | "
            f"置信度 {market_result.confidence:.0%}*\n"
        )


# ============================================================
# 模拟流水线演示
# ============================================================

def demo_pipeline():
    """演示完整的行情感知日报流水线"""
    generator = ReportGenerator()
    classifier = MarketClassifier()

    date = "2026-06-16"
    snapshots = []

    # ---- 09:40 快照 ----
    print("=" * 70)
    print(f"  {date} 可转债日报流水线演示")
    print("=" * 70)

    snap_0940 = build_snapshot_from_market_data(
        snapshot_time="09:40",
        up_count=3200, down_count=1200,
        limit_up_count=18, limit_down_count=2,
        index_change_pct=0.45,
    )
    r1 = classifier.classify(snap_0940)
    snapshots.append(("09:40", r1))
    print(f"\n[09:40] {r1.market_type.value}型-{r1.config.get('name','')} "
          f"| 置信度 {r1.confidence:.0%}")

    # ---- 10:00 快照 (确认) ----
    snap_1000 = build_snapshot_from_market_data(
        snapshot_time="10:00",
        up_count=2800, down_count=1600,
        limit_up_count=30, limit_down_count=5,
        index_change_pct=0.35, volume_ratio=0.95,
        prev_type=r1.market_type,
    )
    r2 = classifier.classify(snap_1000)
    snapshots.append(("10:00", r2))
    print(f"[10:00] {r2.market_type.value}型-{r2.config.get('name','')} "
          f"| 置信度 {r2.confidence:.0%} | "
          f"权重: 概念{r2.config.get('concept_weight',0):.0%}/"
          f"候选{r2.config.get('candidate_weight',0):.0%}")

    # ---- 10:32 快照 (定型) ----
    snap_1032 = build_snapshot_from_market_data(
        snapshot_time="10:32",
        up_count=2500, down_count=1900,
        limit_up_count=35, limit_down_count=4,
        index_change_pct=0.15, volume_ratio=0.88,
    )
    r3 = classifier.classify(snap_1032)
    snapshots.append(("10:32", r3))
    print(f"[10:32] {r3.market_type.value}型-{r3.config.get('name','')} "
          f"| 置信度 {r3.confidence:.0%} | "
          f"权重: 概念{r3.config.get('concept_weight',0):.0%}/"
          f"候选{r3.config.get('candidate_weight',0):.0%}")

    # ---- 最终日报示例 (10:32 定型) ----
    print(f"\n{'='*70}")
    print(f"  最终日报示例（10:32 定型版）")
    print(f"{'='*70}\n")

    # 生成完整日报
    header = generator.generate_header(date, "10:32", r3)
    print(header)

    # 模拟 TOP10
    mock_top10 = [
        {"name": "宏微转债", "premium": 16.8, "price": 233, "final_score": 85.2, "has_forced_redemption": False},
        {"name": "欧通转债", "premium": 5.3, "price": 1248, "final_score": 82.1, "has_forced_redemption": False},
        {"name": "集智转债", "premium": 7.2, "price": 152, "final_score": 78.6, "has_forced_redemption": False},
    ]
    print("## TOP10 排名（行情感知权重）")
    print("")
    print("| # | 可转债名称 | 溢价率(%) | 价格 | 评分 | 强赎 |")
    print("|---|-----------|----------|------|------|------|")
    for i, t in enumerate(mock_top10, 1):
        forced = "是" if t["has_forced_redemption"] else "-"
        print(f"| {i} | {t['name']} | {t['premium']:.1f} | {t['price']:.0f} | {t['final_score']:.1f} | {forced} |")

    footer = generator.generate_footer(r3)
    print(footer)

    # ---- 行情类型变迁图 ----
    print(f"\n{'='*70}")
    print(f"  行情类型变迁")
    print(f"{'='*70}")
    print(f"  {'时间':<8} {'类型':<6} {'名称':<10} {'概念权重':<10} {'置信度'}")
    print(f"  {'-'*46}")
    for time, r in snapshots:
        cfg = r.config
        print(f"  {time:<8} {r.market_type.value:<6} "
              f"{cfg.get('name',''):<10} "
              f"{cfg.get('concept_weight',0):.0%}       "
              f"{r.confidence:.0%}")

    print(f"\n{'='*70}")
    print(f"  各行情类型完整权重表")
    print(f"{'='*70}")
    print(f"  {'类型':<4} {'名称':<10} {'概念权重':<8} {'候选权重':<8} {'策略'}  ")
    print(f"  {'-'*60}")
    for mt in MarketType:
        cfg = get_weight_config(mt)
        print(f"  {mt.value:<4} {cfg['market_name']:<10} "
              f"{cfg['concept_weight']:<8.0%} {cfg['candidate_weight']:<8.0%} "
              f"{cfg['strategy'][:20]}...")
    print()


if __name__ == "__main__":
    demo_pipeline()
