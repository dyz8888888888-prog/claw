"""
行情快照获取器
从 i问财 (pywencai) 获取实时市场指标：指数涨跌、涨跌家数、涨停数
"""

import pywencai
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

COOKIE_FILE = Path(__file__).parent.parent / ".wencai_cookie"


def _load_cookie() -> str:
    with open(COOKIE_FILE) as f:
        return f.read().strip()


@dataclass
class MarketSnapshot:
    """市场快照"""
    up_count: int          # 上涨家数
    down_count: int        # 下跌家数
    limit_up_count: int    # 涨停数
    limit_down_count: int  # 跌停数
    sh_index_pct: float    # 上证涨跌幅%
    sz_index_pct: float    # 深证涨跌幅%
    volume_ratio: Optional[float] = None  # 量能比 (暂未实现)


def fetch_market_snapshot() -> MarketSnapshot:
    """从 i问财获取完整市场快照"""
    cookie = _load_cookie()

    # 1. 指数涨跌
    result = pywencai.get(
        query='上证指数 深证成指涨跌幅',
        query_type='zhishu', cookie=cookie, log=False
    )
    index_df = list(result.values())[0] if result else pd.DataFrame()
    sh_pct = 0.0
    sz_pct = 0.0
    if not index_df.empty:
        sh_row = index_df[index_df['代码'] == '000001.SH']
        sz_row = index_df[index_df['代码'] == '399001.SZ']
        if not sh_row.empty:
            sh_pct = float(sh_row['涨跌幅'].iloc[0])
        if not sz_row.empty:
            sz_pct = float(sz_row['涨跌幅'].iloc[0])

    # 2. 涨跌家数 (用涨跌幅查询前1000只来抽样估算)
    #    i问财不直接支持汇总统计，我们通过两次查询做近似
    try:
        up_df = pywencai.get(
            query='今日上涨 非ST', cookie=cookie,
            perpage=100, loop=3, sleep=1, log=False
        )
        up_count = len(up_df)
    except:
        up_count = 0

    try:
        down_df = pywencai.get(
            query='今日下跌 非ST', cookie=cookie,
            perpage=100, loop=3, sleep=1, log=False
        )
        down_count = len(down_df)
    except:
        down_count = 0

    # 3. 涨停跌停数
    try:
        lu_df = pywencai.get(
            query='今日涨停 非ST', cookie=cookie,
            perpage=100, loop=3, sleep=1, log=False
        )
        lu_count = len(lu_df)
    except:
        lu_count = 0

    try:
        ld_df = pywencai.get(
            query='今日跌停 非ST', cookie=cookie,
            perpage=100, loop=3, sleep=1, log=False
        )
        ld_count = len(ld_df)
    except:
        ld_count = 0

    return MarketSnapshot(
        up_count=up_count, down_count=down_count,
        limit_up_count=lu_count, limit_down_count=ld_count,
        sh_index_pct=sh_pct, sz_index_pct=sz_pct,
    )


def fetch_index_only() -> dict:
    """仅获取指数涨跌（快速版）"""
    cookie = _load_cookie()
    result = pywencai.get(
        query='上证指数 深证成指涨跌幅',
        query_type='zhishu', cookie=cookie, log=False
    )
    index_df = list(result.values())[0] if result else pd.DataFrame()
    out = {'sh': 0.0, 'sz': 0.0}
    if not index_df.empty:
        for _, row in index_df.iterrows():
            if row['代码'] == '000001.SH':
                out['sh'] = float(row['涨跌幅'])
            elif row['代码'] == '399001.SZ':
                out['sz'] = float(row['涨跌幅'])
    return out


if __name__ == '__main__':
    snap = fetch_market_snapshot()
    print(f'上证: {snap.sh_index_pct:+.2f}%  深证: {snap.sz_index_pct:+.2f}%')
    print(f'涨/跌: {snap.up_count}/{snap.down_count}')
    print(f'涨停: {snap.limit_up_count}  跌停: {snap.limit_down_count}')
