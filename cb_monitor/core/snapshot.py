"""
快照数据结构 — Snapshot

单轮行情快照, 是系统的核心数据单元
包含: 转债实时行情 + 正股数据 + 实时计算的溢价率
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Snapshot:
    """单轮快照数据"""
    code: str              # 转债代码 (6位)
    name: str              # 转债名称
    trade: float           # 最新价
    change_pct: float      # 涨跌幅 %
    volume: int            # 成交量 (手)
    amount: float          # 成交额 (元) — 原始值, 显示用 fmt_amount()
    high: float            # 当日最高
    low: float             # 当日最低
    buy: float             # 买一价
    sell: float            # 卖一价
    ticktime: str          # 更新时间
    stock_name: str = ''   # 正股名称

    # 通达信正股数据
    stock_price: Optional[float] = None
    stock_change_pct: Optional[float] = None

    # 计算得到的溢价率 (TDX正股价 + cov转股价 实时计算)
    premium_ratio: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'name': self.name,
            'trade': self.trade,
            'change_pct': self.change_pct,
            'volume': self.volume,
            'amount': self.amount,
            'high': self.high,
            'low': self.low,
            'buy': self.buy,
            'sell': self.sell,
            'ticktime': self.ticktime,
            'stock_price': self.stock_price,
            'stock_change_pct': self.stock_change_pct,
            'premium_ratio': self.premium_ratio,
        }
