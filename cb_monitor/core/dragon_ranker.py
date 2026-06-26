"""
DragonRanker — 概念板块龙头排序引擎

三维打分模型 (参考行业共识):
  1. 封板时间分 (35%): 越早涨停分越高
  2. 封单力度分 (35%): 封单/流通市值 比例
  3. 连板高度分 (30%): 连板天数 × 10

用法:
    ranker = DragonRanker()
    sectors = kpl_client.get_limit_up_sectors()
    ranked = ranker.rank_all_sectors(sectors)
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DragonRanker:
    """概念板块龙头排序引擎"""

    TIME_WEIGHT = 0.35
    FENGDAN_WEIGHT = 0.35
    LIANBAN_WEIGHT = 0.30

    # 涨停时间 → 分数映射
    TIME_SCORE_TABLE = [
        (10.00, 100),   # 10:00 前
        (10.50, 90),    # 10:30 前
        (11.00, 80),    # 11:00 前
        (11.50, 70),    # 11:30 前
        (13.50, 60),    # 13:30 前
        (14.00, 50),    # 14:00 前
        (14.50, 40),    # 14:30 前
        (15.00, 30),    # 尾盘
    ]

    # 板块最小涨停数门槛
    MIN_STOCK_COUNT = 2

    # 板块最小涨幅门槛 (仅为龙头上色)
    MIN_SECTOR_PCT = 1.5

    def rank_sector(self, sector: dict) -> list[dict]:
        """对单个板块的涨停股排序

        输入: sector dict (来自 kpl_client.get_limit_up_sectors)
            {sector_name, sector_code, stock_count, stocks: [{股票代码, 股票名称, 涨停时间, 封单额, 流通市值, 连板天数, 概念标签, 涨停原因, 主题}]}
        输出: 排序后的 dragos list
            [{rank, name, code, time_score, fengdan_score, lianban_score, total_score, limit_up_time, concepts, reason, theme}]
        """
        stocks = sector.get('stocks', [])
        if not stocks:
            return []

        scored = []
        for s in stocks:
            time_score = self._calc_time_score(s.get('涨停时间', '') or s.get('首次封板时间', ''))
            fengdan_score = self._calc_fengdan_score(
                float(s.get('封单额', 0) or 0),
                float(s.get('流通市值', 0) or 1)
            )
            # 连板天数可能是: 整数 / '首板' / 'N连板' / None
            lb_raw = s.get('连板天数', 0)
            lianban_days = self._parse_lianban(lb_raw)
            lianban_score = lianban_days * 10
            total = (time_score * self.TIME_WEIGHT +
                     fengdan_score * self.FENGDAN_WEIGHT +
                     lianban_score * self.LIANBAN_WEIGHT)

            scored.append({
                'name': str(s.get('股票名称', '')),
                'code': str(s.get('股票代码', '')),
                'time_score': round(time_score, 1),
                'fengdan_score': round(fengdan_score, 1),
                'lianban_score': round(lianban_score, 1),
                'total_score': round(total, 1),
                'limit_up_time': str(s.get('涨停时间', '') or s.get('首次封板时间', '')),
                'concepts': str(s.get('概念标签', '')),
                'reason': str(s.get('涨停原因', '')),
                'theme': str(s.get('主题', '')),
                'consecutive_days': lianban_days,
                'fengdan_amount': float(s.get('封单额', 0) or 0),
                'limit_up_price': float(s.get('涨停价', 0) or 0),
            })

        scored.sort(key=lambda x: -x['total_score'])
        for i, s in enumerate(scored):
            s['rank'] = i + 1
        return scored

    def rank_all_sectors(self, kpl_sectors: dict) -> list[dict]:
        """对所有板块执行龙头排序

        输入: kpl_client.get_limit_up_sectors() 的完整返回
        输出: [{
            sector_name, sector_code, stock_count,
            top_dragon: {rank=1 的完整信息},
            dragons: [龙1~龙N 详情]
        }]
        """
        sectors = kpl_sectors.get('sectors', [])
        results = []

        for sec in sectors:
            stock_count = sec.get('stock_count', 0)
            if stock_count < self.MIN_STOCK_COUNT:
                continue

            dragons = self.rank_sector(sec)
            if not dragons:
                continue

            top = dragons[0] if dragons else {}

            results.append({
                'sector_name': sec.get('sector_name', ''),
                'sector_code': sec.get('sector_code', ''),
                'stock_count': stock_count,
                'top_dragon': top,
                'dragons': dragons,
            })

        # 按涨停数降序 + 龙一总分降序
        results.sort(key=lambda x: (-x['stock_count'],
                                     -x['dragons'][0]['total_score'] if x['dragons'] else 0))
        return results

    def get_top_dragons(self, ranked_sectors: list[dict], top_n: int = 8) -> list[dict]:
        """从排序后的板块中提取 Top N 龙头 (简化视图)

        返回: [{sector, rank, name, code, total_score, reason, limit_up_time, consecutive}]
        """
        result = []
        for sec in ranked_sectors[:top_n]:
            top = sec.get('top_dragon', {})
            if top:
                result.append({
                    'sector': sec['sector_name'],
                    'rank': 1,
                    'name': top.get('name', ''),
                    'code': top.get('code', ''),
                    'total_score': top.get('total_score', 0),
                    'reason': top.get('reason', '')[:40],
                    'limit_up_time': top.get('limit_up_time', ''),
                    'consecutive': top.get('consecutive_days', 0),
                })
        return result

    def _calc_time_score(self, time_str: str) -> float:
        """涨停越早分越高

        时间 → 分数映射:
          10:00前 = 100,  10:30前 = 90,  11:00前 = 80,
          11:30前 = 70,   13:30前 = 60,  14:00前 = 50,
          14:30前 = 40,   尾盘 = 30,      无数据 = 50
        """
        if not time_str:
            return 50

        # 解析时间: "09:35:00" 或 "09:35" 或 9.5833
        try:
            if ':' in str(time_str):
                parts = str(time_str).split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                t = hour + minute / 60.0
            else:
                t = float(time_str)
        except (ValueError, IndexError):
            return 50

        for threshold, score in self.TIME_SCORE_TABLE:
            if t <= threshold:
                return score
        return 30

    def _calc_fengdan_score(self, fengdan: float, mcap: float) -> float:
        """封单力度: 封单/流通市值 比例越大越好

        封单/流通市值 > 8% = 100分
        > 5% = 80分
        > 2% = 60分
        > 1% = 40分
        其他线性衰减
        """
        if mcap <= 0:
            return 0
        ratio = fengdan / mcap * 100
        return min(ratio * 12.5, 100)

    @staticmethod
    def _parse_lianban(val) -> int:
        """解析连板天数: 处理 '首板' / '2连板' / 数字等"""
        if val is None:
            return 0
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val).strip()
        # '首板' → 1
        if '首板' in s or '一板' in s:
            return 1
        # 'N连板' → N
        m = re.search(r'(\d+)', s)
        if m:
            return int(m.group(1))
        return 0
