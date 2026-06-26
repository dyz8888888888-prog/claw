"""
滚动窗口 - RollingWindow

内存环形缓冲区，每只转债保留最近N轮快照
用于计算均价、均量、涨跌幅加速度等时序指标
"""

import logging
from collections import defaultdict, deque
from typing import Optional

from core.snapshot import Snapshot

logger = logging.getLogger(__name__)


class RollingWindow:
    """滚动窗口 - 内存状态管理"""

    def __init__(self, max_window: int = 100):
        self.max_window = max_window
        # code -> deque of Snapshot
        self._windows: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_window)
        )
        # 当日最高价 (盘中累积)
        self._day_highs: dict[str, float] = {}
        # 当日最低价
        self._day_lows: dict[str, float] = {}

    def push(self, code: str, snapshot: 'Snapshot'):
        """存入新快照"""
        self._windows[code].append(snapshot)
        # 更新当日最高/最低
        if code not in self._day_highs or snapshot.trade > self._day_highs[code]:
            self._day_highs[code] = snapshot.trade
        if code not in self._day_lows or snapshot.trade < self._day_lows[code]:
            self._day_lows[code] = snapshot.trade

    def get_window(self, code: str, n: int = 10) -> list:
        """获取最近N轮快照 (最新的在末尾)"""
        q = self._windows.get(code)
        if not q:
            return []
        return list(q)[-n:]

    def get_current(self, code: str) -> Optional['Snapshot']:
        """获取最新的快照"""
        q = self._windows.get(code)
        if not q:
            return None
        return q[-1]

    def get_prev(self, code: str) -> Optional['Snapshot']:
        """获取上一轮快照"""
        q = self._windows.get(code)
        if not q or len(q) < 2:
            return None
        return q[-2]

    def get_avg_volume(self, code: str, n: int = 5) -> float:
        """近N轮平均成交量"""
        snaps = self.get_window(code, n)
        if len(snaps) < 2:
            return 0.0
        # volume是累计值，需要算增量
        vols = []
        for i in range(1, len(snaps)):
            delta = max(0, snaps[i].volume - snaps[i-1].volume)
            vols.append(delta)
        if not vols:
            return 0.0
        return sum(vols) / len(vols)

    def get_current_volume_delta(self, code: str) -> float:
        """本轮成交量增量（最新-上一轮）"""
        q = self._windows.get(code)
        if not q or len(q) < 2:
            return 0.0
        return max(0, q[-1].volume - q[-2].volume)

    def get_volume_delta_long(self, code: str, n: int = 20) -> float:
        """成交量增量（最新 - N轮前），减少短时波动假信号"""
        q = self._windows.get(code)
        if not q or len(q) <= n:
            return 0.0
        return max(0, q[-1].volume - q[-n].volume)

    def get_peak_volume_delta(self, code: str, n: int = 20) -> float:
        """窗口内单轮成交量峰值（用于放量尖峰检测）"""
        snaps = self.get_window(code, n + 1)
        if len(snaps) < 2:
            return 0.0
        peaks = []
        for i in range(1, len(snaps)):
            delta = max(0, snaps[i].volume - snaps[i-1].volume)
            peaks.append(delta)
        return max(peaks) if peaks else 0.0

    def get_avg_volume_long(self, code: str, n: int = 20) -> float:
        """近N轮平均每轮成交量（基于长窗口）"""
        snaps = self.get_window(code, n + 1)
        if len(snaps) < 2:
            return 0.0
        vols = []
        for i in range(1, len(snaps)):
            delta = max(0, snaps[i].volume - snaps[i-1].volume)
            vols.append(delta)
        if not vols:
            return 0.0
        return sum(vols) / len(vols)

    def get_avg_price(self, code: str, n: int = 5) -> float:
        """近N轮均价"""
        snaps = self.get_window(code, n)
        if not snaps:
            return 0.0
        prices = [s.trade for s in snaps if s.trade > 0]
        if not prices:
            return 0.0
        return sum(prices) / len(prices)

    def get_day_high(self, code: str) -> Optional[float]:
        """当日最高价"""
        return self._day_highs.get(code)

    def get_rolling_high(self, code: str, n: int = 20, exclude_latest: bool = True) -> Optional[float]:
        """最近N轮滚动窗口内的最高价 (排除最新一轮, 用于突破检测)"""
        snaps = self.get_window(code, n + 1)
        if len(snaps) < 3:
            return None
        # 取前N个 (排除最新), 找最高价
        window_snaps = snaps[-n-1:-1] if exclude_latest and len(snaps) > n else snaps[:n]
        prices = [s.trade for s in window_snaps if s.trade > 0]
        return max(prices) if prices else None

    def get_day_low(self, code: str) -> Optional[float]:
        """当日最低价"""
        return self._day_lows.get(code)

    def get_recent_prices(self, code: str, n: int = 5) -> list[float]:
        """最近N轮价格序列"""
        snaps = self.get_window(code, n)
        return [s.trade for s in snaps if s.trade > 0]

    def get_prev_premium(self, code: str) -> Optional[float]:
        """上一轮转股溢价率 (实时计算自通达信数据)"""
        prev = self.get_prev(code)
        if prev and prev.premium_ratio is not None:
            return prev.premium_ratio
        return None

    def get_prev_stock_change(self, code: str) -> Optional[float]:
        """上一轮正股涨跌幅"""
        prev = self.get_prev(code)
        if prev and prev.stock_change_pct is not None:
            return prev.stock_change_pct
        return None

    def get_current_stock_change(self, code: str) -> Optional[float]:
        """当前正股涨跌幅"""
        cur = self.get_current(code)
        if cur and cur.stock_change_pct is not None:
            return cur.stock_change_pct
        return None

    def clear(self):
        """清空所有状态"""
        self._windows.clear()
        self._day_highs.clear()
        self._day_lows.clear()
