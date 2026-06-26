"""
交易日历 — TradingCalendar

使用 akshare 获取交易所官方交易日历, 缓存到本地文件
解决调休日 (补班不开市) 和长假休眠问题
"""

import os
import json
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'data', 'trading_calendar.json')


class TradingCalendar:
    """交易日历 (单例, 按年缓存)"""

    _instance = None
    _trading_days: set = None
    _year: int = 0

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._trading_days = None
        self._year = 0
        self._load_cache()

    def _load_cache(self):
        """从本地缓存加载交易日历"""
        try:
            if os.path.exists(_CACHE_PATH):
                with open(_CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._trading_days = set(data.get('days', []))
                    self._year = data.get('year', 0)
                    logger.info(f"交易日历缓存: {len(self._trading_days)} 天 ({self._year}年)")
        except Exception as e:
            logger.warning(f"交易日历加载失败: {e}")

    def _fetch_and_cache(self):
        """从 akshare 拉取全年交易日历"""
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            if df is None or df.empty:
                logger.warning("akshare 交易日历返回空")
                return

            # 取当前年的交易日
            current_year = str(datetime.now().year)
            days = set()
            for _, row in df.iterrows():
                d = str(row.iloc[0])[:10].replace('-', '')  # 2026-01-19 → 20260119
                if len(d) == 8 and d.startswith(current_year):
                    days.add(d)

            self._trading_days = days
            self._year = current_year

            # 缓存到本地
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump({'year': current_year, 'days': sorted(days)}, f)
            logger.info(f"交易日历更新: {len(days)} 天 ({current_year}年)")

        except Exception as e:
            logger.error(f"获取交易日历失败: {e}, 回退到周末判断")

    def is_trading_day(self) -> bool:
        """判断今天是否为交易日"""
        today = datetime.now().strftime('%Y%m%d')

        # 年初/缓存过期 → 刷新
        current_year = datetime.now().year
        if self._trading_days is None or current_year != self._year:
            self._fetch_and_cache()

        if self._trading_days is not None:
            return today in self._trading_days

        # 回退: 周末判断
        return datetime.now().weekday() < 5

    def is_trading_hours(self) -> bool:
        """判断当前是否在交易时段 (9:30-11:30, 13:00-15:00)"""
        if not self.is_trading_day():
            return False
        now = datetime.now()
        t = now.hour * 100 + now.minute
        return (930 <= t <= 1130) or (1300 <= t <= 1500)


# 全局单例
trading_calendar = TradingCalendar()
