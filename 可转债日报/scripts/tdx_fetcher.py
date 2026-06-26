"""
TDX 数据获取引擎 — 通达信 V7.73+ PYPlugins 接口封装
替代 push2 + mx-finance-data + i问财概念查询
纯本地调用，零网络延迟

使用方法:
    from tdx_fetcher import TDXFetcher
    fetcher = TDXFetcher()
    cbs = fetcher.fetch_all_cb()        # 所有可转债
    snap = fetcher.fetch_market_breadth()  # 涨跌比/涨停数
    concepts = fetcher.fetch_concepts('605006.SH')  # 正股概念
"""

import sys
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime

# 通达信 PYPlugins 路径
TDX_PLUGIN_DIR = "D:/tdx/PYPlugins/user"
sys.path.insert(0, TDX_PLUGIN_DIR)

# PEP 8 naming — module uses tq alias
from tqcenter import tq  # type: ignore


# ============================================================
# 数据结构
# ============================================================

@dataclass
class CBData:
    """单只可转债完整数据"""
    code: str           # CB代码 (6位)
    name: str           # CB名称
    full_code: str      # 完整代码 (如 '111001.SH')
    price: float        # 现价
    premium: float      # 溢价率%
    scale: float        # 剩余规模(亿元)
    zg_price: float     # 转股价
    stock_price: float  # 正股价
    stock_code: str     # 正股代码 (6位)
    stock_full: str     # 正股完整代码 (如 '605006.SH')
    stock_name: str     # 正股名称
    end_date: str       # 到期日
    rating: str         # 评级
    volume: float       # 成交量(手)
    amount: float       # 成交额(万元)
    pct_chg: float      # 涨跌幅%
    open: float         # 开盘价
    yest_pct: float     # 昨日涨跌幅% (ZAFYesterday, 开盘已知)
    yest_amount: float  # 昨日成交额(万元) (CJJEPre1, 开盘已知)
    vol5d: float        # 近5日涨跌幅绝对值% (|ZAFPre5|, 波动代理)
    high: float         # 最高价
    low: float          # 最低价
    amp: float          # 振幅%
    concepts: List[str] = field(default_factory=list)  # 概念标签


@dataclass
class MarketBreadth:
    """市场广度数据"""
    up_count: int       # 上涨家数
    down_count: int     # 下跌家数
    flat_count: int     # 平盘家数
    limit_up_count: int # 涨停家数 (含ST)
    limit_down_count: int # 跌停家数 (含ST)
    sh_index: float     # 上证指数
    sh_pct: float       # 上证涨跌幅%
    sz_index: float     # 深证成指
    sz_pct: float       # 深证涨跌幅%
    total_stocks: int   # 总股票数
    snapshot_time: str  # 快照时间


# ============================================================
# 公共函数
# ============================================================

def fix_stock_code(hscode: str) -> str:
    """修正 TQ 返回的短代码为完整格式"""
    code = str(hscode).strip().zfill(6)
    if code.startswith(('0', '3')):
        return f'{code}.SZ'
    elif code.startswith('6'):
        return f'{code}.SH'
    elif code.startswith(('4', '8')):
        return f'{code}.BJ'
    return f'{code}.SZ'


def filter_concepts(blocks: List[Dict]) -> List[str]:
    """过滤掉非主题概念（地区/市场标签/交易属性）"""
    skip_keywords = {
        '板块', '含可转债', '含H股', '含B股', 'ST板块',
        '中证100', '中证200', '中证500', '中证1000', '中证2000',
        '沪深300', '创业300', '深证成指', '国证2000',
        '上证180', '上证50', '科创50', '科创100', '创业200',
        '连续亏损', '微利股', '券商重仓', '基金重仓', '定增股',
        '近期新高', '近期强势', '近期超跌', '最近多板', '最近异动',
        '昨日较强', '昨日涨停', '昨日触板', '昨日首板',
        '最近情绪指数', '活跃ETF', '高贝塔值', '低市盈率', '高市盈率',
    }
    result = []
    for b in blocks:
        name = b.get('BlockName', '')
        if name not in skip_keywords and not name.endswith('板块'):
            result.append(name)
    return result


# ============================================================
# TDXFetcher 核心类
# ============================================================

class TDXFetcher:
    """通达信数据获取器"""

    def __init__(self, script_path: str = None):
        """
        初始化并连接 TDX 客户端。
        必须在 TDX 客户端运行且已登录时调用。
        """
        if script_path is None:
            script_path = os.path.join(TDX_PLUGIN_DIR, 'tdx_fetcher.py')
        tq.initialize(script_path)

    def close(self):
        """断开连接"""
        try:
            tq.close()
        except Exception:
            pass

    # ---- CB 数据 ----

    def fetch_all_cb(self, quiet: bool = True) -> Tuple[List[CBData], int]:
        """获取全部可转债基础数据（价格/溢价/规模/正股）"""
        import io as _io
        cb_list = tq.get_stock_list('32')
        results = []
        fail_count = 0

        for cb_full in cb_list:
            try:
                if quiet:
                    _stdout = sys.stdout
                    _stderr = sys.stderr
                    sys.stdout = _io.StringIO()
                    sys.stderr = _io.StringIO()
                kzz = tq.get_kzz_info(stock_code=cb_full)
                if quiet:
                    sys.stdout = _stdout
                    sys.stderr = _stderr
            except Exception:
                if quiet:
                    sys.stdout = _stdout
                    sys.stderr = _stderr
                fail_count += 1
                continue

            if not kzz:
                continue

            code = cb_full[:6]  # '111001'
            price = float(kzz.get('KZZNow', 0))
            if price <= 0:
                continue

            premium = float(kzz.get('KZZYj', 999))
            scale_raw = float(kzz.get('RestScope', 0))
            scale = scale_raw / 10000  # 万元→亿元

            if scale <= 0:
                scale = 999

            zg_price = float(kzz.get('ZGPrice', 0))
            stock_price = float(kzz.get('AGNow', 0))
            hs_code = str(kzz.get('HSCode', '')).strip()

            # 行情快照（成交量/成交额）
            try:
                snap = tq.get_market_snapshot(stock_code=cb_full)
                volume = float(snap.get('Volume', 0))
                amount = float(snap.get('Amount', 0))
                open_p = float(snap.get('Open', 0))
                high = float(snap.get('Max', 0))
                low = float(snap.get('Min', 0))
                last_close = float(snap.get('LastClose', 0))
                pct = (price / last_close - 1) * 100 if last_close > 0 else 0
                amp = (high / low - 1) * 100 if low > 0 else 0
            except Exception:
                volume = amount = open_p = high = low = pct = amp = 0

            stock_code = hs_code.zfill(6)
            stock_full = fix_stock_code(hs_code)

            results.append(CBData(
                code=code,
                name=kzz.get('KZZCode', ''),  # CB名称从 kzz_info 拿不到，后续补充
                full_code=cb_full,
                price=round(price, 2),
                premium=round(premium, 2),
                scale=round(scale, 4),
                zg_price=round(zg_price, 2),
                stock_price=round(stock_price, 2),
                stock_code=stock_code,
                stock_full=stock_full,
                stock_name='',
                end_date=str(kzz.get('EndDate', '')),
                rating=str(kzz.get('KZZScore', '')),
                volume=volume,
                amount=amount,
                pct_chg=round(pct, 2),
                open=open_p,
                high=high,
                low=low,
                amp=round(amp, 2),
                yest_pct=0.0,
                yest_amount=0.0,
                vol5d=0.0,
            ))

        return results, fail_count

    def load_yesterday_data(self, cb_list: List[CBData]) -> int:
        """
        加载昨日盘后数据（开盘前已知因子）。
        返回成功加载数量。
        """
        from tqcenter import tq as _tq
        count = 0
        for cb in cb_list:
            try:
                import io as _io
                _stderr = sys.stderr
                sys.stderr = _io.StringIO()
                mi = _tq.get_more_info(stock_code=cb.full_code)
                sys.stderr = _stderr
                if mi and isinstance(mi, dict):
                    cb.yest_pct = float(mi.get('ZAFYesterday', 0))
                    cb.yest_amount = float(mi.get('CJJEPre1', 0))
                    cb.vol5d = abs(float(mi.get('ZAFPre5', 0)))
                    count += 1
            except:
                sys.stderr = _stderr
                continue
        return count

    def resolve_cb_names(self, cb_list: List[CBData]) -> None:
        """
        从交易数据反查 CB 名称。
        kzz_info 无名称字段，通过 get_match_stkinfo 逐个查。
        批量查会耗时较多（~10s），建议只在必要的时候调用。
        """
        for cb in cb_list:
            try:
                match = tq.get_match_stkinfo(key_word=cb.code)
                if match and len(match) > 0:
                    # match 返回 [(code, name, market, type), ...]
                    for m in match:
                        if cb.code in str(m[0]):
                            cb.name = str(m[1])
                            break
            except Exception:
                pass

    # ---- 市场广度 ----

    def fetch_market_breadth(self) -> MarketBreadth:
        """获取市场广度：涨跌比、涨停数、指数涨跌幅"""
        now = datetime.now().strftime('%H:%M')

        # 全A股涨跌统计
        all_stocks = tq.get_stock_list('5')
        try:
            pv = tq.get_pricevol(stock_list=all_stocks)
        except Exception:
            return MarketBreadth(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, now)

        up = down = flat = lu = ld = 0
        for code, data in pv.items():
            try:
                now_p = float(data.get('Now', 0))
                close = float(data.get('LastClose', 0))
                vol = float(data.get('Volume', 0))
                if close <= 0:
                    continue
                pct = (now_p / close - 1) * 100

                if pct > 0.01:
                    up += 1
                elif pct < -0.01:
                    down += 1
                else:
                    flat += 1

                # 涨停（涨幅>=9.8% 且非零量）
                if pct >= 9.8 and vol > 0:
                    lu += 1
                if pct <= -9.8 and vol > 0:
                    ld += 1
            except Exception:
                continue

        # 指数
        try:
            sh = tq.get_market_snapshot(stock_code='999999.SH')
            sh_idx = float(sh.get('Now', 0))
            sh_close = float(sh.get('LastClose', 0))
            sh_pct = (sh_idx / sh_close - 1) * 100 if sh_close > 0 else 0
        except Exception:
            sh_idx = sh_pct = 0

        try:
            sz = tq.get_market_snapshot(stock_code='399001.SZ')
            sz_idx = float(sz.get('Now', 0))
            sz_close = float(sz.get('LastClose', 0))
            sz_pct = (sz_idx / sz_close - 1) * 100 if sz_close > 0 else 0
        except Exception:
            sz_idx = sz_pct = 0

        return MarketBreadth(
            up_count=up, down_count=down, flat_count=flat,
            limit_up_count=lu, limit_down_count=ld,
            sh_index=round(sh_idx, 2), sh_pct=round(sh_pct, 2),
            sz_index=round(sz_idx, 2), sz_pct=round(sz_pct, 2),
            total_stocks=up + down + flat,
            snapshot_time=now,
        )

    # ---- 概念 ----

    def fetch_concepts(self, stock_full: str,
                       filter_noise: bool = True) -> List[str]:
        """获取单只正股的概念标签"""
        try:
            rel = tq.get_relation(stock_code=stock_full)
            blocks = [{'BlockName': b.get('BlockName', ''),
                       'BlockType': b.get('BlockType', '')}
                      for b in rel]
            if filter_noise:
                return filter_concepts(blocks)
            return [b['BlockName'] for b in blocks]
        except Exception:
            return []

    def fetch_concepts_batch(self,
                              stock_map: Dict[str, str]) -> Dict[str, List[str]]:
        """批量获取概念: {cb_code: [概念]}"""
        result = {}
        for cb_code, stock_full in stock_map.items():
            try:
                result[cb_code] = self.fetch_concepts(stock_full)
            except Exception:
                result[cb_code] = []
        return result

    def fetch_concept_blocks(self) -> List[Dict]:
        """获取全部概念板块列表"""
        try:
            return tq.get_sector_list()
        except Exception:
            return []

    def fetch_block_stocks(self, block_name: str) -> List[str]:
        """获取板块成分股"""
        try:
            return tq.get_stock_list_in_sector(block_name)
        except Exception:
            return []

    # ---- 板块行情（热度） ----

    def fetch_block_pct(self, block_code: str) -> float:
        """获取板块指数涨跌幅%"""
        try:
            snap = tq.get_market_snapshot(stock_code=block_code)
            now_p = float(snap.get('Now', 0))
            close = float(snap.get('LastClose', 0))
            if close > 0:
                return (now_p / close - 1) * 100
        except Exception:
            pass
        return 0.0


# ============================================================
# 测试入口
# ============================================================

if __name__ == '__main__':
    import time
    fetcher = TDXFetcher()

    t0 = time.time()
    cbs = fetcher.fetch_all_cb()
    print(f'CB数据: {len(cbs)}只 ({time.time()-t0:.1f}s)')

    # 规模分布
    scales = [c.scale for c in cbs if c.scale < 999]
    prices = [c.price for c in cbs]
    prems = [c.premium for c in cbs if abs(c.premium) < 999]

    print(f'规模: {min(scales):.2f}~{max(scales):.2f}亿')
    print(f'价格: {min(prices):.0f}~{max(prices):.0f}元')
    print(f'溢价: {min(prems):.1f}%~{max(prems):.1f}%')
    print(f'  负溢价: {sum(1 for p in prems if p < 0)}只')
    print(f'  >100%: {sum(1 for p in prems if p > 100)}只')

    # 前5
    for c in cbs[:5]:
        print(f'  {c.code}: 价格{c.price} 溢价{c.premium:.1f}% 规模{c.scale:.2f}亿')

    # 市场广度
    mb = fetcher.fetch_market_breadth()
    total = mb.total_stocks
    print(f'\n市场广度: 涨{mb.up_count}({mb.up_count/total*100:.0f}%) '
          f'跌{mb.down_count}({mb.down_count/total*100:.0f}%) '
          f'平{mb.flat_count} 涨停{mb.limit_up_count}')

    fetcher.close()
    print('✓ 测试完成')
