"""
数据融合层 - DataFusion (通达信唯一数据源)

数据源架构：
┌──────────────────────────────────────────────────────┐
│  主数据源: mootdx (通达信行情协议)                     │
│    ├── 转债实时行情 + 5档盘口 (295/320只, ~0.1s)     │
│    └── 正股实时行情 + 5档盘口 (313/317只, ~0.1s)     │
├──────────────────────────────────────────────────────┤
│  补充源: akshare                                     │
│    └── bond_zh_hs_cov_spot(): 通达信缺失的转债 (~25只)│
├──────────────────────────────────────────────────────┤
│  基础池: akshare bond_zh_cov()                       │
│    └── 转债静态信息 (含转股价/溢价率, 30分钟刷新)     │
└──────────────────────────────────────────────────────┘

溢价率计算 (实时):
  convert_value = 100 / convert_price * stock_price  (通达信)
  premium_ratio = (bond_price - convert_value) / convert_value * 100
"""

import time
import logging
from typing import Optional
import pandas as pd
import akshare as ak
from mootdx.quotes import Quotes

from core.snapshot import Snapshot

logger = logging.getLogger(__name__)


# ============================================================
# 市场判断工具
# ============================================================

def get_market(code: str) -> int:
    """判断A股/转债所属市场: 0=深圳, 1=上海"""
    if not code:
        return 0
    first = code[0]
    if code.startswith(('12',)):    # 深圳转债 12xxxx
        return 0
    if code.startswith(('11',)):    # 上海转债 11xxxx
        return 1
    if first in ('0', '3', '2'):    # 深圳A股
        return 0
    if first in ('6', '9'):         # 上海A股
        return 1
    if first in ('4', '8'):         # 北交所/三板
        return 0
    return 0


# ============================================================
# 通达信客户端 (单例)
# ============================================================

class TdxClient:
    """通达信行情客户端 - 单例 (带激进健康检查)

    恢复时间优化:
    - 空闲 >15s → 重建 (原30s, 减半)
    - 连续错误 >=2 → 立即重建 (原3次, 更快感知闪断)
    - 单次错误 <15s 内 → 强制重建 (快速恢复, 不等待累加)
    - 新建后必 ping 000001 验证, 失败再重建 → 双保险
    """
    _instance = None
    _client = None
    _last_use = 0
    _error_count = 0
    _last_error_time = 0

    _IDLE_MAX = 15          # 最大空闲时间(s)
    _ERROR_RECENT_WINDOW = 15  # 近期错误窗口(s)
    _ERROR_MAX = 2          # 连续错误上限

    @classmethod
    def get(cls):
        now = time.time()
        # 判断是否需要重建 (任一条件满足)
        recent_error = (cls._error_count >= 1 and
                        (now - cls._last_error_time) < cls._ERROR_RECENT_WINDOW)
        need_reconnect = (cls._client is None or
                          (now - cls._last_use) > cls._IDLE_MAX or
                          cls._error_count >= cls._ERROR_MAX or
                          recent_error)
        if need_reconnect:
            if cls._client is not None:
                try: cls._client.close()
                except: pass
            cls._client = cls._connect(now)
        else:
            cls._last_use = now
        return cls._client

    @classmethod
    def _connect(cls, now: float):
        """新建通达信连接 + ping 验证 (双保险)"""
        client = Quotes.factory(market='std')
        try:
            client.quotes(['000001'])
        except Exception:
            logger.warning("TDX新建连接验证失败, 二次重建")
            try: client.close()
            except: pass
            client = Quotes.factory(market='std')
            # 二次失败不重试, 让上层感知
        cls._client = client
        cls._last_use = now
        cls._error_count = 0
        cls._last_error_time = 0
        return client

    @classmethod
    def mark_error(cls):
        cls._error_count += 1
        cls._last_error_time = time.time()

    @classmethod
    def close(cls):
        if cls._client:
            try: cls._client.close()
            except: pass
            cls._client = None
            cls._error_count = 0


# ============================================================
# DataFusion
# ============================================================

class DataFusion:
    """数据融合层 - 通达信为主，akshare补充"""

    def __init__(self, monitor_list: list[dict]):
        self.monitor_list = monitor_list

        # code(6位数字) -> monitor_item
        self._monitor_map: dict[str, dict] = {}
        # stock_code -> bond_code
        self._stock_to_bond: dict[str, str] = {}
        for item in monitor_list:
            code = item.get('code_num', '') or item.get('code', '')
            self._monitor_map[code] = item
            sc = item.get('stock_code', '')
            if sc:
                self._stock_to_bond[sc] = code

        # 统计
        self.last_fetch_cost = 0.0
        self.last_fetch_time = 0

    def set_monitor_list(self, monitor_list: list[dict]):
        self.monitor_list = monitor_list
        self._monitor_map.clear()
        self._stock_to_bond.clear()
        for item in monitor_list:
            code = item.get('code_num', '') or item.get('code', '')
            self._monitor_map[code] = item
            sc = item.get('stock_code', '')
            if sc:
                self._stock_to_bond[sc] = code

    def _fetch_bonds_from_tdx(self, codes: list[str]) -> dict[str, dict]:
        """用通达信批量查询转债行情"""
        if not codes:
            return {}
        result = {}
        try:
            client = TdxClient.get()
            df = client.quotes(codes)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get('code', ''))
                    if code not in codes:
                        continue
                    price = float(row.get('price', 0) or 0)
                    close = float(row.get('last_close', 0) or 0)
                    if close <= 0 or price <= 0:
                        continue
                    pct = ((price - close) / close * 100) if close > 0 else 0.0
                    result[code] = {
                        'trade': price,
                        'change_pct': round(pct, 2),
                        'open': float(row.get('open', 0) or 0),
                        'high': float(row.get('high', 0) or 0),
                        'low': float(row.get('low', 0) or 0),
                        'volume': int(row.get('volume', 0) or 0),
                        'amount': float(row.get('amount', 0) or 0),
                        'bid1': float(row.get('bid1', 0) or 0),
                        'ask1': float(row.get('ask1', 0) or 0),
                        'bid2': float(row.get('bid2', 0) or 0),
                        'ask2': float(row.get('ask2', 0) or 0),
                        'bid3': float(row.get('bid3', 0) or 0),
                        'ask3': float(row.get('ask3', 0) or 0),
                        'bid4': float(row.get('bid4', 0) or 0),
                        'ask4': float(row.get('ask4', 0) or 0),
                        'bid5': float(row.get('bid5', 0) or 0),
                        'ask5': float(row.get('ask5', 0) or 0),
                        'servertime': str(row.get('servertime', '')),
                    }
        except Exception as e:
            logger.error(f"通达信转债查询异常: {e}")
            TdxClient.mark_error()
        return result

    def _fetch_bonds_from_akshare(self, codes: set[str]) -> dict[str, dict]:
        """用akshare spot补充通达信缺失的转债"""
        if not codes:
            return {}
        result = {}
        try:
            df = ak.bond_zh_hs_cov_spot()
            if df is not None and not df.empty:
                for col in ['trade', 'changepercent', 'buy', 'sell',
                            'settlement', 'open', 'high', 'low']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')

                code_col = 'code'
                for _, row in df.iterrows():
                    code = str(row.get(code_col, '')).strip()
                    if code not in codes:
                        continue
                    trade = float(row.get('trade', 0) or 0)
                    if trade <= 0:
                        continue
                    close = float(row.get('settlement', 0) or 0)
                    pct = float(row.get('changepercent', 0) or 0)
                    result[code] = {
                        'trade': trade,
                        'change_pct': pct,
                        'open': float(row.get('open', 0) or 0),
                        'high': float(row.get('high', 0) or 0),
                        'low': float(row.get('low', 0) or 0),
                        'volume': int(row.get('volume', 0) or 0),
                        'amount': float(row.get('amount', 0) or 0),
                        'bid1': float(row.get('buy', 0) or 0),
                        'ask1': float(row.get('sell', 0) or 0),
                        'servertime': str(row.get('ticktime', '')),
                    }
        except Exception as e:
            logger.error(f"akshare spot补充查询异常: {e}")
        return result

    def _fetch_stocks_from_tdx(self, codes: list[str]) -> dict[str, dict]:
        """用通达信批量查询正股行情"""
        if not codes:
            return {}
        result = {}
        try:
            client = TdxClient.get()
            df = client.quotes(codes)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get('code', ''))
                    if code not in codes:
                        continue
                    price = float(row.get('price', 0) or 0)
                    close = float(row.get('last_close', 0) or 0)
                    if close <= 0 or price <= 0:
                        continue
                    pct = ((price - close) / close * 100) if close > 0 else 0.0
                    result[code] = {
                        'price': price,
                        'change_pct': round(pct, 2),
                        'open': float(row.get('open', 0) or 0),
                        'high': float(row.get('high', 0) or 0),
                        'low': float(row.get('low', 0) or 0),
                        'volume': int(row.get('volume', 0) or 0),
                        'amount': float(row.get('amount', 0) or 0),
                        'bid1': float(row.get('bid1', 0) or 0),
                        'ask1': float(row.get('ask1', 0) or 0),
                    }
        except Exception as e:
            logger.error(f"通达信正股查询异常: {e}")
            TdxClient.mark_error()
        return result

    def merge(self) -> dict[str, Snapshot]:
        """
        核心融合方法

        执行顺序:
        1. 通达信批量查转债 (主)
        2. 通达信批量查正股 (主)
        3. akshare补充通达信缺失的转债
        4. 从通达信数据实时计算溢价率
        5. 构造Snapshot
        """
        t0 = time.time()

        # --- 1. 收集需要查询的代码 ---
        monitor_codes = list(self._monitor_map.keys())
        stock_codes = [sc for sc in self._stock_to_bond.keys() if sc]

        if not monitor_codes:
            logger.warning("监控列表为空")
            return {}

        # --- 2. 通达信查转债 ---
        tdx_bonds = self._fetch_bonds_from_tdx(monitor_codes)

        # --- 3. 通达信查正股 ---
        tdx_stocks = {}
        if stock_codes:
            tdx_stocks = self._fetch_stocks_from_tdx(stock_codes)

        # --- 4. akshare补充转债 (仅缺失>5只时调用, 减少外网开销)
        missing_codes = set(monitor_codes) - set(tdx_bonds.keys())
        akshare_bonds = {}
        if len(missing_codes) > 5:
            akshare_bonds = self._fetch_bonds_from_akshare(missing_codes)
            logger.info(f"akshare补充: {len(akshare_bonds)}/{len(missing_codes)} 只")

        # --- 5. 构造Snapshot + 计算溢价率 ---
        snapshots: dict[str, Snapshot] = {}

        for code in monitor_codes:
            mi = self._monitor_map[code]

            # 取转债数据 (优先通达信)
            bond_data = tdx_bonds.get(code) or akshare_bonds.get(code)
            if not bond_data:
                continue

            # 取正股数据
            sc = mi.get('stock_code', '')
            stock = tdx_stocks.get(sc, {})

            snap = Snapshot(
                code=code,
                name=mi.get('name', ''),
                stock_name=mi.get('stock_name', ''),
                trade=bond_data.get('trade', 0),
                change_pct=bond_data.get('change_pct', 0),
                volume=int(bond_data.get('volume', 0) or 0),
                amount=float(bond_data.get('amount', 0) or 0),
                high=bond_data.get('high', 0),
                low=bond_data.get('low', 0),
                buy=bond_data.get('bid1', 0),
                sell=bond_data.get('ask1', 0),
                ticktime=bond_data.get('servertime', ''),
            )

            # 正股数据 (通达信)
            if stock:
                snap.stock_price = stock.get('price', 0) or None
                snap.stock_change_pct = stock.get('change_pct')

            # 溢价率计算: 用通达信正股价 + cov转股价实时计算
            convert_price = mi.get('convert_price', 0)
            if stock and convert_price and convert_price > 0 and snap.trade > 0:
                sp = stock.get('price', 0)
                if sp and sp > 0:
                    convert_value = 100.0 / convert_price * sp
                    snap.premium_ratio = round((snap.trade - convert_value) / convert_value * 100, 2)

            snapshots[code] = snap

        # --- 5.5 数据校验: 检测价格突变 (单轮 >5% 视为脏数据, 丢弃)
        _validate_snapshots(snapshots)

        # --- 统计 ---
        self.last_fetch_cost = time.time() - t0
        self.last_fetch_time = time.time()

        stock_count = sum(1 for s in snapshots.values() if s.stock_change_pct is not None)
        prem_count = sum(1 for s in snapshots.values() if s.premium_ratio is not None)
        tdx_count = sum(1 for c in snapshots if c in tdx_bonds)
        logger.debug(
            f"融合: {len(snapshots)}只(通达信{tdx_count}+补充{len(snapshots)-tdx_count}) "
            f"正股{stock_count}只 溢价率{prem_count}只 耗时{self.last_fetch_cost:.2f}s"
        )

        return snapshots


# ============================================================
# 数据校验
# ============================================================

# 上一轮价格缓存 (code -> price), 用于检测价格突变
_prev_prices: dict[str, float] = {}

def _validate_snapshots(snapshots: dict[str, Snapshot]):
    """检测价格突变: 单轮 >5% → 丢弃并告警"""
    global _prev_prices
    bad_codes = []
    now_prices = {}
    for code, snap in snapshots.items():
        now_prices[code] = snap.trade
        prev = _prev_prices.get(code)
        if prev and prev > 0 and snap.trade > 0:
            jump = abs(snap.trade - prev) / prev
            if jump > 0.05:  # 5%突变
                bad_codes.append(code)
                logger.warning(f"价格突变: {snap.name}({code}) {prev:.2f}→{snap.trade:.2f} 跳变{jump*100:.1f}%")

    for code in bad_codes:
        snapshots.pop(code, None)
    _prev_prices = now_prices


# ============================================================
# 工具函数
# ============================================================

def fmt_amount(amount_yuan: float) -> str:
    """成交额格式化: 元 → 亿元 (统一显示)"""
    if amount_yuan <= 0:
        return "0.00亿"
    return f"{amount_yuan / 100000000:.2f}亿"
