"""
行情类型识别模块
基于三级快照（09:40/10:00/10:32），区分6种行情类型（A-F）
每级可靠性逐级提升：50% → 80% → 90%
"""

from dataclasses import dataclass, field
from typing import Tuple, Optional
from enum import Enum


class MarketType(Enum):
    A = "A"  # 强势普涨
    B = "B"  # 温和偏强
    C = "C"  # 微涨分化
    D = "D"  # 横盘震荡
    E = "E"  # 微跌分化
    F = "F"  # 弱势普跌


# ============================================================
# 行情类型定义 — 含候选分策略权重
# ============================================================
MARKET_CONFIG = {
    MarketType.A: {
        "name": "强势普涨",
        "concept_weight": 0.80,
        "candidate_weight": 0.20,
        "strategy": "全力进攻：追最大振幅、最强概念",
        "report_tag": "[强势]",
        "color": "red",
        # 候选分权重: 振幅>方向>流动性>规模>溢价
        "score_weights": {
            "amp": 0.30, "pct": 0.25, "amount": 0.20,
            "scale": 0.15, "premium": 0.10,
        },
        "philosophy": "追涨杀跌，振幅为王，溢价不重要",
    },
    MarketType.B: {
        "name": "温和偏强",
        "concept_weight": 0.70,
        "candidate_weight": 0.30,
        "strategy": "进攻为主：顺势+概念，适度防御",
        "report_tag": "[温和]",
        "color": "pink",
        "score_weights": {
            "amp": 0.25, "pct": 0.20, "amount": 0.20,
            "scale": 0.20, "premium": 0.15,
        },
        "philosophy": "顺势+概念驱动，微盘弹性加分",
    },
    MarketType.C: {
        "name": "微涨分化",
        "concept_weight": 0.50,
        "candidate_weight": 0.50,
        "strategy": "均衡配置：选活跃+低溢价交集",
        "report_tag": "[分化]",
        "color": "amber",
        "score_weights": {
            "amp": 0.20, "scale": 0.25, "amount": 0.20,
            "premium": 0.20, "pct": 0.15,
        },
        "philosophy": "活跃度与安全性各半，精选中军",
    },
    MarketType.D: {
        "name": "横盘震荡",
        "concept_weight": 0.30,
        "candidate_weight": 0.70,
        "strategy": "防守反击：微盘弹性+低溢价安全",
        "report_tag": "[震荡]",
        "color": "gray",
        "score_weights": {
            "scale": 0.25, "amp": 0.20, "premium": 0.20,
            "amount": 0.20, "pct": 0.15,
        },
        "philosophy": "微盘+低溢价=安全弹性，等风来",
    },
    MarketType.E: {
        "name": "微跌分化",
        "concept_weight": 0.00,
        "candidate_weight": 1.00,
        "strategy": "严格防御：只选最安全，等反弹",
        "report_tag": "[防御]",
        "color": "blue",
        "score_weights": {
            "premium": 0.30, "scale": 0.30, "amount": 0.20,
            "amp": 0.15, "pct": 0.05,
        },
        "philosophy": "极低溢价+极微盘=跌时抗跌，涨时弹性",
    },
    MarketType.F: {
        "name": "弱势普跌",
        "concept_weight": 0.00,
        "candidate_weight": 1.00,
        "strategy": "避险：极度保守，只做风险预警",
        "report_tag": "[预警]",
        "color": "green",
        "score_weights": {
            "premium": 0.35, "scale": 0.30, "amount": 0.25,
            "amp": 0.07, "pct": 0.03,
        },
        "philosophy": "活下去最重要，不跌就是赚",
    },
}

# ============================================================
# 各快照的识别能力
# ============================================================
# 09:40 — 只能区分偏强(A/B) vs 偏弱(E/F) vs 中性(C/D)
# 10:00 — 可以区分 A/B/C vs D/E/F
# 10:32 — 可以区分全部6种

SNAPSHOT_CAPABILITY = {
    "09:40": {
        "reliability": 0.50,
        "description": "方向初判：可区分 A/B vs E/F",
        "distinguishable": {
            "strong": [MarketType.A, MarketType.B],
            "neutral": [MarketType.C, MarketType.D],
            "weak": [MarketType.E, MarketType.F],
        },
    },
    "10:00": {
        "reliability": 0.80,
        "description": "关键确认：可区分 A/B/C vs D/E/F",
        "distinguishable": {
            "strong": [MarketType.A, MarketType.B],
            "neutral_strong": [MarketType.C],
            "neutral_weak": [MarketType.D],
            "weak": [MarketType.E, MarketType.F],
        },
    },
    "10:32": {
        "reliability": 0.90,
        "description": "格局定型：可区分全部6种",
        "distinguishable": None,  # all types
    },
    "11:40": {
        "reliability": 0.92,
        "description": "午盘复核：检查行情是否变脸",
        "distinguishable": None,
    },
    "13:30": {
        "reliability": 0.90,
        "description": "午后复核：下午容易出现趋势转变",
        "distinguishable": None,
    },
    "14:15": {
        "reliability": 0.95,
        "description": "终盘确认：近乎确定",
        "distinguishable": None,
    },
}


@dataclass
class MarketSnapshot:
    """市场快照数据"""

    snapshot_time: str  # "09:40", "10:00", ...
    up_count: int  # 上涨家数
    down_count: int  # 下跌家数
    limit_up_count: int  # 涨停数（含ST）
    limit_down_count: int  # 跌停数（含ST）
    index_change_pct: float  # 上证指数涨跌幅%
    volume_ratio: Optional[float] = None  # 成交额 / 前5日均值
    prev_type: Optional[MarketType] = None  # 上一快照的行情类型

    @property
    def up_down_ratio(self) -> float:
        """涨跌比"""
        if self.down_count == 0:
            return 999.0
        return self.up_count / self.down_count

    @property
    def total_stocks(self) -> int:
        return self.up_count + self.down_count

    @property
    def breadth(self) -> float:
        """市场宽度 = (上涨-下跌) / 总数"""
        if self.total_stocks == 0:
            return 0.0
        return (self.up_count - self.down_count) / self.total_stocks


@dataclass
class ClassificationResult:
    """分类结果"""

    market_type: MarketType
    confidence: float  # 0-1
    reliability: float  # 该快照的可靠性
    description: str
    config: dict = field(default_factory=dict)
    indicators: dict = field(default_factory=dict)


class MarketClassifier:
    """行情类型识别器"""

    # ============================================================
    # 一级分类：09:40 粗判
    # ============================================================
    def _classify_phase1(self, snap: MarketSnapshot) -> ClassificationResult:
        """09:40 方向初判，只能分三类"""
        ratio = snap.up_down_ratio
        lu = snap.limit_up_count
        idx = snap.index_change_pct

        cap = SNAPSHOT_CAPABILITY["09:40"]

        # 强势信号
        strong_signal = int(ratio > 2.5) + int(lu > 15) + int(idx > 0.3)
        # 弱势信号
        weak_signal = int(ratio < 0.4) + int(lu < 5) + int(idx < -0.3)

        if strong_signal >= 2 and weak_signal == 0:
            # 偏强：A 或 B，默认 B（温和）
            config = MARKET_CONFIG[MarketType.B]
            return ClassificationResult(
                market_type=MarketType.B,
                confidence=min(0.5, strong_signal / 3),
                reliability=cap["reliability"],
                description=f"偏强方向（涨跌比{ratio:.1f}:1，涨停{lu}只，指数{idx:+.2f}%），"
                f"10:00确认后可定A/B",
                config=config,
                indicators={
                    "up_down_ratio": ratio,
                    "limit_up_count": lu,
                    "index_change": idx,
                    "strong_signal": strong_signal,
                    "weak_signal": weak_signal,
                },
            )
        elif weak_signal >= 2 and strong_signal == 0:
            # 偏弱：E 或 F，默认 E（微跌）
            config = MARKET_CONFIG[MarketType.E]
            return ClassificationResult(
                market_type=MarketType.E,
                confidence=min(0.5, weak_signal / 3),
                reliability=cap["reliability"],
                description=f"偏弱方向（涨跌比{ratio:.1f}:1，跌停{snap.limit_down_count}只，"
                f"指数{idx:+.2f}%），10:00确认后可定E/F",
                config=config,
                indicators={
                    "up_down_ratio": ratio,
                    "limit_up_count": lu,
                    "index_change": idx,
                    "strong_signal": strong_signal,
                    "weak_signal": weak_signal,
                },
            )
        else:
            # 中性：C 或 D（微涨分化/横盘震荡）
            config = MARKET_CONFIG[MarketType.D]
            return ClassificationResult(
                market_type=MarketType.D,
                confidence=0.3,
                reliability=cap["reliability"],
                description=f"中性方向（涨跌比{ratio:.1f}:1，涨停{lu}只，"
                f"指数{idx:+.2f}%），待10:00确认",
                config=config,
                indicators={
                    "up_down_ratio": ratio,
                    "limit_up_count": lu,
                    "index_change": idx,
                    "strong_signal": strong_signal,
                    "weak_signal": weak_signal,
                },
            )

    # ============================================================
    # 二级分类：10:00 确认
    # ============================================================
    def _classify_phase2(self, snap: MarketSnapshot) -> ClassificationResult:
        """10:00 关键确认，可区分 A/B/C vs D/E/F"""
        ratio = snap.up_down_ratio
        lu = snap.limit_up_count
        idx = snap.index_change_pct
        vol = snap.volume_ratio or 1.0

        cap = SNAPSHOT_CAPABILITY["10:00"]

        # 方向确认：检查是否与09:40方向一致
        direction_confirmed = True
        if snap.prev_type is not None:
            prev_is_strong = snap.prev_type in (MarketType.A, MarketType.B)
            prev_is_weak = snap.prev_type in (MarketType.E, MarketType.F)
            cur_is_strong = ratio > 2.0 and lu > 10 and idx > 0.2
            cur_is_weak = ratio < 0.5 and idx < -0.2
            if prev_is_strong and not cur_is_strong:
                direction_confirmed = False
            if prev_is_weak and not cur_is_weak:
                direction_confirmed = False
            if snap.prev_type in (MarketType.C, MarketType.D):
                direction_confirmed = True  # 中性方向，不需要确认

        # 量能判断
        volume_normal = vol > 0.8
        volume_surge = vol > 1.2

        # 综合信号
        strong_signal = (
            int(ratio > 3.0) + int(lu > 30) + int(idx > 0.5) + int(volume_surge)
        )
        weak_signal = (
            int(ratio < 0.33) + int(snap.limit_down_count > 10) + int(idx < -0.5)
        )
        mid_strong_signal = int(ratio > 1.5) + int(lu > 20) + int(idx > 0.2)
        mid_weak_signal = int(ratio < 0.67) + int(idx < -0.2)

        # 分类逻辑
        if strong_signal >= 3:
            config = MARKET_CONFIG[MarketType.A]
            mt = MarketType.A
        elif strong_signal >= 1 and mid_strong_signal >= 2:
            config = MARKET_CONFIG[MarketType.B]
            mt = MarketType.B
        elif mid_strong_signal >= 1 and direction_confirmed:
            config = MARKET_CONFIG[MarketType.C]
            mt = MarketType.C
        elif weak_signal >= 2:
            config = MARKET_CONFIG[MarketType.F]
            mt = MarketType.F
        elif weak_signal >= 1 or mid_weak_signal >= 2:
            config = MARKET_CONFIG[MarketType.E]
            mt = MarketType.E
        else:
            config = MARKET_CONFIG[MarketType.D]
            mt = MarketType.D

        return ClassificationResult(
            market_type=mt,
            confidence=min(0.8, max(strong_signal, mid_strong_signal, weak_signal) / 4),
            reliability=cap["reliability"],
            description=(
                f"{'方向确认' if direction_confirmed else '方向待确认'}，"
                f"涨跌比{ratio:.1f}:1，涨停{lu}只，"
                f"指数{idx:+.2f}%，量能{'放量' if volume_surge else '正常' if volume_normal else '缩量'}"
            ),
            config=config,
            indicators={
                "up_down_ratio": ratio,
                "limit_up_count": lu,
                "index_change": idx,
                "volume_ratio": vol,
                "direction_confirmed": direction_confirmed,
                "strong_signal": strong_signal,
                "mid_strong_signal": mid_strong_signal,
                "weak_signal": weak_signal,
            },
        )

    # ============================================================
    # 三级分类：10:32 定型
    # ============================================================
    def _classify_phase3(self, snap: MarketSnapshot) -> ClassificationResult:
        """10:32 格局定型，可区分全部6种"""
        ratio = snap.up_down_ratio
        lu = snap.limit_up_count
        ld = snap.limit_down_count
        idx = snap.index_change_pct
        vol = snap.volume_ratio or 1.0

        cap = SNAPSHOT_CAPABILITY["10:32"]

        # A 强势普涨：涨跌比 >4:1，涨停 >80，指数 >1%
        if ratio > 4.0 and lu > 80 and idx > 1.0:
            config = MARKET_CONFIG[MarketType.A]
            return ClassificationResult(
                market_type=MarketType.A,
                confidence=0.9,
                reliability=cap["reliability"],
                description=f"强势普涨确认：涨跌比{ratio:.1f}:1，涨停{lu}只，"
                f"指数{idx:+.2f}%，概念驱动极强",
                config=config,
                indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
            )
        # B 温和偏强：涨跌比 >2:1，涨停 >50，指数 >0.5%
        if ratio > 2.0 and lu > 50 and idx > 0.5:
            config = MARKET_CONFIG[MarketType.B]
            return ClassificationResult(
                market_type=MarketType.B,
                confidence=0.85,
                reliability=cap["reliability"],
                description=f"温和偏强确认：涨跌比{ratio:.1f}:1，涨停{lu}只，"
                f"指数{idx:+.2f}%，板块轮动中",
                config=config,
                indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
            )
        # F 弱势普跌：涨跌比 <1:3，跌停 >10，指数 <-1%
        if ratio < 0.33 and ld > 10 and idx < -1.0:
            config = MARKET_CONFIG[MarketType.F]
            return ClassificationResult(
                market_type=MarketType.F,
                confidence=0.9,
                reliability=cap["reliability"],
                description=f"弱势普跌确认：涨跌比{ratio:.1f}:1，跌停{ld}只，"
                f"指数{idx:+.2f}%，全面防御",
                config=config,
                indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
            )
        # E 微跌分化：涨跌比 <1:2，指数 <-0.3%
        if ratio < 0.5 and idx < -0.3:
            config = MARKET_CONFIG[MarketType.E]
            return ClassificationResult(
                market_type=MarketType.E,
                confidence=0.8,
                reliability=cap["reliability"],
                description=f"微跌分化确认：涨跌比{ratio:.1f}:1，跌停{ld}只，"
                f"指数{idx:+.2f}%，防御为主",
                config=config,
                indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
            )
        # C 微涨分化：涨跌比 1:1~2:1，指数 0~0.5%
        if ratio >= 1.0 and ratio <= 2.0 and 0 <= idx <= 0.5:
            config = MARKET_CONFIG[MarketType.C]
            return ClassificationResult(
                market_type=MarketType.C,
                confidence=0.75,
                reliability=cap["reliability"],
                description=f"微涨分化确认：涨跌比{ratio:.1f}:1，涨停{lu}只，"
                f"指数{idx:+.2f}%，概念信号需谨慎",
                config=config,
                indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
            )
        # D 横盘震荡：默认
        config = MARKET_CONFIG[MarketType.D]
        return ClassificationResult(
            market_type=MarketType.D,
            confidence=0.7,
            reliability=cap["reliability"],
            description=f"横盘震荡确认：涨跌比{ratio:.1f}:1，涨停{lu}只，"
            f"指数{idx:+.2f}%，概念基本失效",
            config=config,
            indicators={"up_down_ratio": ratio, "limit_up_count": lu, "index_change": idx},
        )

    # ============================================================
    # 后续快照：复核
    # ============================================================
    def _classify_checkpoint(self, snap: MarketSnapshot) -> ClassificationResult:
        """11:40 / 13:30 / 14:15 复核，使用三级分类逻辑但标记为复核"""
        result = self._classify_phase3(snap)
        # 如果类型变了，降低置信度
        if snap.prev_type is not None and result.market_type != snap.prev_type:
            result.confidence *= 0.7
            result.description += f"（注意：行情类型从{snap.prev_type.value}变为{result.market_type.value}，可能变盘）"
        return result

    # ============================================================
    # 涨停数升档逻辑
    # ============================================================
    def _apply_limit_up_upgrade(
        self,
        result: ClassificationResult,
        snap: MarketSnapshot,
    ) -> ClassificationResult:
        """
        涨停数升档：涨停多时提升策略攻击性。
        逻辑: 结构性牛市（涨停多但指数平）→ 应该进攻而非防守
        """
        lu = snap.limit_up_count
        mt = result.market_type

        upgrade_map = {
            # (原类型, 最小涨停数): (新类型, 升档原因)
            (MarketType.F, 80):   (MarketType.E, f"涨停{lu}只→升E防御"),
            (MarketType.F, 150):  (MarketType.D, f"涨停{lu}只→升D防守反击"),
            (MarketType.F, 250):  (MarketType.C, f"涨停{lu}只→升C均衡"),

            (MarketType.E, 100):  (MarketType.D, f"涨停{lu}只→升D防守反击"),
            (MarketType.E, 180):  (MarketType.C, f"涨停{lu}只→升C均衡"),
            (MarketType.E, 280):  (MarketType.B, f"涨停{lu}只→升B进攻"),

            (MarketType.D, 120):  (MarketType.C, f"涨停{lu}只→升C均衡"),
            (MarketType.D, 200):  (MarketType.B, f"涨停{lu}只→升B进攻"),
            (MarketType.D, 300):  (MarketType.A, f"涨停{lu}只→升A全力进攻"),

            (MarketType.C, 180):  (MarketType.B, f"涨停{lu}只→升B进攻"),
            (MarketType.C, 280):  (MarketType.A, f"涨停{lu}只→升A全力进攻"),

            (MarketType.B, 300):  (MarketType.A, f"涨停{lu}只→升A全力进攻"),
        }

        # 按涨停数从高到低检查（取最高档匹配）
        candidates = [(k, v) for k, v in upgrade_map.items()
                      if k[0] == mt and lu >= k[1]]
        if not candidates:
            return result

        # 取最高升级
        best = max(candidates, key=lambda x: x[0][1])
        (orig_type, threshold), (new_type, reason) = best

        if new_type != mt:
            result.market_type = new_type
            result.config = MARKET_CONFIG[new_type]
            old_desc = result.description
            result.description = f"{old_desc} | 涨停升档: {mt.value}→{new_type.value} ({reason})"
            result.indicators["upgrade_from"] = mt.value
            result.indicators["upgrade_reason"] = reason

        return result

    # ============================================================
    # 主入口
    # ============================================================
    def classify(self, snap: MarketSnapshot) -> ClassificationResult:
        """根据快照时间自动选择分类策略"""
        time = snap.snapshot_time

        if time == "09:40":
            result = self._classify_phase1(snap)
        elif time == "10:00":
            result = self._classify_phase2(snap)
        elif time == "10:32":
            result = self._classify_phase3(snap)
        else:
            result = self._classify_checkpoint(snap)

        # 涨停数升档: 结构性牛市自动提升策略攻击性
        result = self._apply_limit_up_upgrade(result, snap)
        return result


# ============================================================
# 便捷函数
# ============================================================
def get_weight_config(market_type: MarketType) -> dict:
    """获取行情类型对应的权重配置"""
    config = MARKET_CONFIG[market_type]
    return {
        "market_type": market_type.value,
        "market_name": config["name"],
        "concept_weight": config["concept_weight"],
        "candidate_weight": config["candidate_weight"],
        "strategy": config["strategy"],
        "report_tag": config["report_tag"],
    }


def format_classification_for_report(result: ClassificationResult) -> str:
    """将分类结果格式化为日报用的 Markdown 片段"""
    config = result.config
    tag = config.get("report_tag", "")
    name = config.get("name", result.market_type.value)
    strategy = config.get("strategy", "")
    concept_w = config.get("concept_weight", 0)
    cand_w = config.get("candidate_weight", 0)

    lines = [
        f"## 今日行情类型：{tag} {result.market_type.value}型 - {name}",
        "",
        f"**识别时间**: {result.description.split('：')[0] if '：' in result.description else '快照识别'}",
        f"**置信度**: {result.confidence:.0%}  |  **快照可靠性**: {result.reliability:.0%}",
        f"**策略**: {strategy}",
        f"**权重**: 概念分{concept_w:.0%} / 候选分{cand_w:.0%}",
        "",
    ]

    # 添加指标详情
    ind = result.indicators
    if ind:
        indicator_lines = ["**关键指标**:", ""]
        if "up_down_ratio" in ind:
            indicator_lines.append(f"- 涨跌比: {ind['up_down_ratio']:.1f}:1")
        if "limit_up_count" in ind:
            indicator_lines.append(f"- 涨停数: {ind['limit_up_count']}")
        if "index_change" in ind:
            indicator_lines.append(f"- 指数涨跌: {ind['index_change']:+.2f}%")
        if "volume_ratio" in ind:
            vr = ind["volume_ratio"]
            tag_v = "放量" if vr > 1.2 else "缩量" if vr < 0.8 else "正常"
            indicator_lines.append(f"- 量能比: {vr:.1%}（{tag_v}）")
        if "direction_confirmed" in ind:
            indicator_lines.append(
                f"- 方向确认: {'是' if ind['direction_confirmed'] else '否'}"
            )
        lines.extend(indicator_lines)
        lines.append("")

    lines.append("---")
    return "\n".join(lines)


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    classifier = MarketClassifier()

    # 场景1：09:40 偏强
    snap1 = MarketSnapshot(
        snapshot_time="09:40",
        up_count=3200,
        down_count=1200,
        limit_up_count=20,
        limit_down_count=2,
        index_change_pct=0.5,
    )
    r1 = classifier.classify(snap1)
    print(f"09:40: {r1.market_type.value}型 {r1.market_type.name} | 置信度:{r1.confidence:.0%}")
    print(format_classification_for_report(r1))
    print()

    # 场景2：10:00 强势确认
    snap2 = MarketSnapshot(
        snapshot_time="10:00",
        up_count=3800,
        down_count=800,
        limit_up_count=45,
        limit_down_count=1,
        index_change_pct=1.2,
        volume_ratio=1.3,
        prev_type=MarketType.B,
    )
    r2 = classifier.classify(snap2)
    print(f"10:00: {r2.market_type.value}型 {r2.market_type.name} | 置信度:{r2.confidence:.0%}")
    print(format_classification_for_report(r2))
    print()

    # 场景3：10:32 震荡
    snap3 = MarketSnapshot(
        snapshot_time="10:32",
        up_count=2300,
        down_count=2100,
        limit_up_count=25,
        limit_down_count=5,
        index_change_pct=-0.1,
        volume_ratio=0.9,
    )
    r3 = classifier.classify(snap3)
    print(f"10:32: {r3.market_type.value}型 {r3.market_type.name} | 置信度:{r3.confidence:.0%}")
    print(format_classification_for_report(r3))

    # 权重输出
    print("\n=== 各行情类型权重 ===")
    for mt in MarketType:
        cfg = get_weight_config(mt)
        print(f"  {mt.value}型 {cfg['market_name']:6s} | "
              f"概念{cfg['concept_weight']:.0%} / 候选{cfg['candidate_weight']:.0%} | "
              f"{cfg['report_tag']}")
