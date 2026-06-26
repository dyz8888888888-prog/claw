"""
策略公共数据层 — 实时批量获取，最小化延迟
适用: 7个交易窗口，单次扫描 < 3s
"""
import sys, os, json, time
sys.path.insert(0, 'D:/tdx/PYPlugins/user')
from tqcenter import tq
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONCEPT_MAP_PATH = os.path.join(PROJECT_DIR, 'cb_concept_map.json')
CONCEPT_HEAT_PATH = os.path.join(PROJECT_DIR, 'cb_concept_heat.json')


@dataclass
class LiveCB:
    """实时CB快照"""
    code: str           # CB代码 (6位)
    name: str           # 名称
    full_code: str      # '111001.SH'
    price: float        # 现价
    last_close: float   # 昨收
    pct_chg: float      # 涨幅%
    volume: float       # 成交量
    stock_code: str     # 正股代码
    stock_full: str     # 正股完整代码
    stock_price: float  # 正股现价
    stock_close: float  # 正股昨收
    stock_pct: float    # 正股涨幅%
    premium: float      # 溢价% (需 get_kzz_info, 首次加载后缓存)
    scale: float        # 规模亿


class LiveData:
    """实时数据引擎 — 每次扫描 2-5 秒"""

    def __init__(self, script_path: str):
        tq.initialize(script_path)
        self._cb_list = tq.get_stock_list('32')
        self._prem_cache: Dict[str, float] = {}  # code → premium
        self._scale_cache: Dict[str, float] = {}  # code → scale
        self._stock_map: Dict[str, str] = {}       # cb_code → stock_full
        self._name_map: Dict[str, str] = {}        # cb_code → name
        self._warm_up()
        self._resolve_names()

    def _warm_up(self):
        """预热：一次加载所有静态数据（溢价/规模/正股）"""
        import io as _io
        for cb_full in self._cb_list:
            try:
                _stdout = sys.stdout
                sys.stdout = _io.StringIO()
                kzz = tq.get_kzz_info(stock_code=cb_full)
                sys.stdout = _stdout
            except:
                sys.stdout = _stdout if '_stdout' in dir() else sys.__stdout__
                continue
            code = cb_full[:6]
            hs = str(kzz.get('HSCode', '')).zfill(6)
            if hs == '000000':
                continue
            self._prem_cache[code] = float(kzz.get('KZZYj', 999))
            self._scale_cache[code] = float(kzz.get('RestScope', 0)) / 10000
            if hs.startswith(('0', '3')):
                self._stock_map[code] = f'{hs}.SZ'
            elif hs.startswith('6'):
                self._stock_map[code] = f'{hs}.SH'

    def _resolve_names(self):
        """从 push2 缓存 + TDX 补充 CB 名称"""
        push2_path = os.path.join(PROJECT_DIR, 'cb_top50_full.json')
        if os.path.exists(push2_path):
            try:
                with open(push2_path, encoding='utf-8') as f:
                    items = json.load(f)
                for item in items:
                    code = str(item.get('f12', ''))
                    name = str(item.get('f14', ''))
                    if code and name and len(name) > 1:
                        self._name_map[code] = name
            except:
                pass

        # TDX 补充遗漏
        missing = []
        for cb_full in self._cb_list:
            code = cb_full[:6]
            if code not in self._name_map:
                missing.append(cb_full)
        if missing:
            for cb_full in missing:
                try:
                    match = tq.get_match_stkinfo(key_word=cb_full[:6])
                    if match:
                        for m in match:
                            if isinstance(m, dict):
                                name = str(m.get('Name', ''))
                            else:
                                name = str(m[1]) if len(m) > 1 else ''
                            if name and len(name) > 1:
                                self._name_map[cb_full[:6]] = name
                                break
                except:
                    pass
        resolved = sum(1 for c in self._stock_map if c in self._name_map)
        print(f'  名称解析: {resolved}/{len(self._stock_map)}只')

    # ---- 实时行情 ----

    def scan(self) -> List[LiveCB]:
        """快速扫描：所有CB行情 + 对应正股行情"""
        cb_prices_raw = tq.get_pricevol(stock_list=self._cb_list)
        # key可能是 '111001.SH' 或 '111001', 统一为 6位code
        cb_prices = {}
        for k, v in cb_prices_raw.items():
            code = k[:6] if '.' in k else k
            cb_prices[code] = v

        stock_codes = list(set(self._stock_map.values()))
        stock_prices_raw = tq.get_pricevol(stock_list=stock_codes)
        stock_prices = {}
        for k, v in stock_prices_raw.items():
            code = k[:6] if '.' in k else k
            stock_prices[k] = v  # 保持完整格式用于匹配

        results = []
        for cb_full in self._cb_list:
            code = cb_full[:6]
            if code not in cb_prices or code not in self._stock_map:
                continue
            sfull = self._stock_map[code]
            if sfull not in stock_prices:
                continue

            cb_data = cb_prices[code]
            stk_data = stock_prices[sfull]
            cb_now = float(cb_data.get('Now', 0))
            cb_close = float(cb_data.get('LastClose', 1))
            stk_now = float(stk_data.get('Now', 0))
            stk_close = float(stk_data.get('LastClose', 1))
            if cb_now <= 0 or stk_now <= 0:
                continue

            results.append(LiveCB(
                code=code, name=self._name_map.get(code, code), full_code=cb_full,
                price=cb_now, last_close=cb_close,
                pct_chg=(cb_now / cb_close - 1) * 100,
                volume=float(cb_data.get('Volume', 0)),
                stock_code=sfull.split('.')[0], stock_full=sfull,
                stock_price=stk_now, stock_close=stk_close,
                stock_pct=(stk_now / stk_close - 1) * 100,
                premium=self._prem_cache.get(code, 999),
                scale=self._scale_cache.get(code, 999),
            ))
        return results

    def market_breadth(self) -> Dict:
        """全市场涨跌比"""
        all_stocks = tq.get_stock_list('5')
        pv = tq.get_pricevol(stock_list=all_stocks)
        up = down = flat = lu = 0
        for code, data in pv.items():
            try:
                now_p = float(data.get('Now', 0))
                close_p = float(data.get('LastClose', 1))
                if close_p <= 0:
                    continue
                pct = (now_p / close_p - 1) * 100
                if pct > 0.1:
                    up += 1
                    if pct > 9.7:
                        lu += 1
                elif pct < -0.1:
                    down += 1
                else:
                    flat += 1
            except:
                continue
        return {'up': up, 'down': down, 'flat': flat, 'limit_up': lu,
                'total': up + down + flat,
                'ratio': round(up / max(down, 1), 2) if down else 99}

    def sh_index(self) -> float:
        try:
            snap = tq.get_market_snapshot(stock_code='999999.SH')
            c = float(snap.get('LastClose', 1))
            n = float(snap.get('Now', 0))
            return (n / c - 1) * 100 if c > 0 else 0
        except:
            return 0

    def close(self):
        try:
            tq.close()
        except:
            pass
