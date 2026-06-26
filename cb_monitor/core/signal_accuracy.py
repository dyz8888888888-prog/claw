"""
信号准确率追踪器 — SignalAccuracyTracker

从回测 CSV 计算各类信号近 N 日胜率，供 DecisionPipeline 动态调整信号等级。
支持: 硬抑制 (<25%) / 仅预警 (25-35%) / 动态权重 / 升级奖励
"""
import csv
import glob
import os
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# ── 信号抑制阈值 (可调) ──
SUPPRESS_THRESHOLD = 25.0      # 胜率低于此 → 直接禁用
WARNING_THRESHOLD = 35.0       # 胜率低于此 → 降为 D 级仅预警
UPGRADE_THRESHOLD = 60.0       # 胜率高于此 → 升级
MIN_COUNT_THRESHOLD = 20       # 最少样本数才做决策


class SignalAccuracyTracker:
    """近 N 日信号准确率追踪"""

    def __init__(self, log_dir: str = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
        self._log_dir = log_dir
        self._accuracy: dict[str, dict] = {}      # signal_type → {wr60, wr300, avg300, count}
        self._suppressed: set = set()              # 被硬抑制的信号类型
        self._warning_only: set = set()            # 仅预警(D级)的信号类型

    def load_recent(self, days: int = 5):
        """从最近的 backtest_*.csv 加载准确率"""
        pattern = os.path.join(self._log_dir, 'backtest_*.csv')
        files = sorted(glob.glob(pattern), reverse=True)[:days]

        if not files:
            logger.warning("SignalAccuracy: 无回测数据")
            return

        agg = defaultdict(lambda: {'p60': [], 'p300': [], 'count': 0})
        for fpath in files:
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        st = row.get('signal_type', '')
                        try:
                            p60 = float(row.get('60s_pnl', 0) or 0)
                            p300 = float(row.get('300s_pnl', 0) or 0)
                            agg[st]['p60'].append(p60)
                            agg[st]['p300'].append(p300)
                            agg[st]['count'] += 1
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.warning(f"读取回测文件失败 {fpath}: {e}")

        import statistics
        self._accuracy.clear()
        self._suppressed.clear()
        self._warning_only.clear()

        for st in agg:
            stats = agg[st]
            n = stats['count']
            if n < 5:
                continue
            wr60 = sum(1 for p in stats['p60'] if p > 0) / n * 100
            wr300 = sum(1 for p in stats['p300'] if p > 0) / n * 100
            avg300 = statistics.mean(stats['p300'])
            self._accuracy[st] = {
                'win_rate_60s': round(wr60, 1),
                'win_rate_300s': round(wr300, 1),
                'avg_pnl_300s': round(avg300, 2),
                'count': n,
            }

            # ── 硬抑制判定 ──
            if wr300 < SUPPRESS_THRESHOLD and n >= MIN_COUNT_THRESHOLD:
                self._suppressed.add(st)
                logger.warning(f"信号抑制: {st} (胜率{wr300}%<{SUPPRESS_THRESHOLD}%, {n}次) — 已禁用")
            elif wr300 < WARNING_THRESHOLD and n >= MIN_COUNT_THRESHOLD:
                self._warning_only.add(st)
                logger.info(f"信号降级: {st} → 仅D预警 (胜率{wr300}%, {n}次)")

        logger.info(f"SignalAccuracy: 加载 {len(self._accuracy)} 类信号准确率 "
                    f"(近{len(files)}日, 抑制{len(self._suppressed)}个, 预警{len(self._warning_only)}个)")

    # ── 公共接口 ──────────────────────────────

    def is_suppressed(self, signal_type: str) -> bool:
        """信号是否被硬抑制 (胜率太低, 不应产生)"""
        return signal_type in self._suppressed

    def is_warning_only(self, signal_type: str) -> bool:
        """信号是否应降为 D 级仅预警"""
        return signal_type in self._warning_only

    def get_suppressed_types(self) -> list[str]:
        """返回所有被抑制的信号类型"""
        return sorted(self._suppressed)

    def get_warning_types(self) -> list[str]:
        """返回所有预警级信号类型"""
        return sorted(self._warning_only)

    def get_dynamic_weight(self, signal_type: str) -> float:
        """基于胜率的动态权重 (1.0 = 基准50%胜率)
        
        公式: min(max(win_rate_300s / 50.0, 0.15), 2.0)
        - 50%胜率 → 1.0
        - 25%胜率 → 0.5  
        - 13%胜率 → 0.26 (即使不禁用也会被压到很低)
        - 60%+ → >1.2 (奖励)
        """
        acc = self._accuracy.get(signal_type)
        if not acc:
            return 1.0
        wr300 = acc['win_rate_300s']
        weight = max(0.15, min(2.0, wr300 / 50.0))
        return round(weight, 2)

    def get_level_adjustment(self, signal_type: str, current_level: str) -> str:
        """根据近 N 日胜率返回推荐的信号等级调整

        规则:
        - 胜率 < 25% (300s) 且 count >= 20 → 完全抑制, 返回 'X'
        - 胜率 < 35% (300s) 且 count >= 20 → 降为 D (仅预警)
        - 胜率 > 60% (300s) 且 count >= 20 → 升一级
        - 其他 → 不变
        """
        acc = self._accuracy.get(signal_type)
        if not acc:
            return current_level

        wr300 = acc['win_rate_300s']
        count = acc['count']

        # 硬抑制: 胜率太低, 返回特殊标记让调度器删除
        if wr300 < SUPPRESS_THRESHOLD and count >= MIN_COUNT_THRESHOLD:
            return 'X'

        # 降为仅预警
        if wr300 < WARNING_THRESHOLD and count >= MIN_COUNT_THRESHOLD:
            if current_level not in ('D',):
                logger.info(f"信号降级: {signal_type} {current_level}→D (胜率{wr300}%)")
                return 'D'

        # 升级: 胜率优秀
        if wr300 > UPGRADE_THRESHOLD and count >= MIN_COUNT_THRESHOLD:
            upgrade = {'B': 'A', 'A': 'S', 'C': 'B', 'D': 'C'}
            new_level = upgrade.get(current_level, current_level)
            if new_level != current_level:
                logger.info(f"信号升级: {signal_type} {current_level}→{new_level} (胜率{wr300}%)")
                return new_level

        return current_level

    def get_all(self) -> dict:
        """返回所有准确率数据 + 抑制状态"""
        all_data = dict(self._accuracy)
        for st in all_data:
            all_data[st]['suppressed'] = st in self._suppressed
            all_data[st]['warning_only'] = st in self._warning_only
            all_data[st]['dynamic_weight'] = self.get_dynamic_weight(st)
        return all_data
