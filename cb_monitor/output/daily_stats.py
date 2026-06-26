"""
盘中统计 - DailyStats

记录每轮信号统计:
- 按等级、类型、代码的分布
- 当日信号总数
- 最活跃标的
"""

import time
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class DailyStats:
    """盘中统计追踪"""

    def __init__(self, config: dict):
        self.cfg = config.get('stats', {})
        self.enabled = self.cfg.get('enabled', True)
        self.max_history = self.cfg.get('max_history', 500)
        self._today = None
        self._signals = []
        self._count_by_type = defaultdict(int)
        self._count_by_level = defaultdict(int)
        self._count_by_code = defaultdict(int)

    def reset_if_new_day(self):
        """跨日自动重置"""
        today = time.strftime('%Y%m%d')
        if today != self._today:
            self._today = today
            self._signals.clear()
            self._count_by_type.clear()
            self._count_by_level.clear()
            self._count_by_code.clear()
            logger.info("统计已重置 (新交易日)")

    def record(self, signal):
        """记录一条信号"""
        if not self.enabled:
            return
        self.reset_if_new_day()
        self._signals.append(signal)
        self._count_by_type[signal.signal_type] += 1
        self._count_by_level[signal.level] += 1
        self._count_by_code[signal.code] += 1
        # 限制历史
        if len(self._signals) > self.max_history:
            old = self._signals.pop(0)
            # 递减计数 (简化处理, 不精确反减)
            self._count_by_type[old.signal_type] = max(0, self._count_by_type[old.signal_type] - 1)
            self._count_by_level[old.level] = max(0, self._count_by_level[old.level] - 1)
            self._count_by_code[old.code] = max(0, self._count_by_code[old.code] - 1)

    def record_batch(self, signals):
        """批量记录"""
        for sig in signals:
            self.record(sig)

    @property
    def total_signals(self) -> int:
        return len(self._signals)

    @property
    def summary(self) -> str:
        """统计摘要文本"""
        if not self._signals:
            return ''

        parts = []
        for level in ['S', 'A', 'B', 'C', 'D']:
            cnt = self._count_by_level.get(level, 0)
            if cnt:
                parts.append(f'{level}:{cnt}')

        # 最活跃标的 (Top 3)
        top = sorted(self._count_by_code.items(), key=lambda x: -x[1])[:3]
        code_str = ' '.join([f'{c}({n})' for c, n in top]) if top else ''

        result = f'信号分布: {" | ".join(parts)}'
        if code_str:
            result += f' | 最活跃: {code_str}'
        return result
