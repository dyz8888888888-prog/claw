"""
可转债选债模块 - BondSelector

功能：从东方财富全量池加载、筛选转债池
- bond_zh_cov(): 东方财富全量池 (1023只, 含正股代码/转股价/转股溢价率)
"""

import json
import os
import time
import logging
from typing import Optional
import pandas as pd
import akshare as ak

logger = logging.getLogger(__name__)

# 全量池本地缓存路径 (akshare bond_zh_cov 失败时兜底)
_COV_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "data", "cov_pool_cache.json")


class BondSelector:
    """选债模块 - 加载、筛选转债池"""

    def __init__(self, config: dict):
        self.config = config['selector']
        self._cov_pool: Optional[pd.DataFrame] = None  # 东方财富全量池
        self._fused_pool: Optional[pd.DataFrame] = None  # 筛选后的监控池
        self._last_cov_time = 0
        self._redeem_df: Optional[pd.DataFrame] = None  # 强赎数据
        self._last_redeem_time = 0
        self._active_count: int = 0  # 活跃转债数量 (实时轮询中)

    def load_cov_pool(self, force: bool = False) -> pd.DataFrame:
        """
        加载东方财富全量可转债池
        - 返回: DataFrame [债券代码, 债券简称, 正股代码, 正股简称,
                正股价, 转股价, 转股价值, 债现价, 转股溢价率]
        """
        now = time.time()
        if not force and self._cov_pool is not None and (now - self._last_cov_time) < 600:
            return self._cov_pool

        try:
            df = ak.bond_zh_cov()
            if df is None or df.empty:
                logger.warning("bond_zh_cov() 返回空, 尝试缓存兜底")
                return self._load_cache_fallback()

            # 重命名列 - 统一为英文键名
            rename_map = {
                '债券代码': 'code', '债券简称': 'name',
                '正股代码': 'stock_code', '正股简称': 'stock_name',
                '正股价': 'stock_price', '转股价': 'convert_price',
                '转股价值': 'convert_value', '债现价': 'bond_price',
                '转股溢价率': 'premium_ratio', '发行规模': 'issue_scale',
            }
            available_cols = {k: v for k, v in rename_map.items() if k in df.columns}
            df = df.rename(columns=available_cols)

            # 只保留有关键字段的行
            required = ['code', 'name', 'stock_code']
            for col in required:
                if col not in df.columns:
                    df[col] = ''

            # 数值转换
            for col in ['convert_price', 'convert_value', 'bond_price', 'premium_ratio', 'stock_price']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            self._cov_pool = df
            self._last_cov_time = now

            # 写入本地缓存 (兜底用)
            self._save_cache(df)
            logger.info(f"Cov池加载: {len(df)} 只")
            return df
        except Exception as e:
            logger.error(f"加载Cov池失败: {e}, 尝试缓存兜底")
            return self._load_cache_fallback()

    def _save_cache(self, df: pd.DataFrame):
        """将全量池存为本地 JSON 缓存 (仅关键字段)"""
        try:
            os.makedirs(os.path.dirname(_COV_CACHE_PATH), exist_ok=True)
            # 只缓存核心字段, 减少文件体积
            keep = ['code', 'name', 'stock_code', 'stock_name',
                    'convert_price', 'convert_value', 'bond_price',
                    'premium_ratio', 'issue_scale', 'stock_price']
            cols = [c for c in keep if c in df.columns]
            records = df[cols].to_dict(orient='records')
            with open(_COV_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump({
                    'cached_at': time.time(),
                    'cached_at_str': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'count': len(records),
                    'items': records,
                }, f, ensure_ascii=False, default=str)
            logger.debug(f"Cov池缓存写入: {len(records)} 只 → {_COV_CACHE_PATH}")
        except Exception as e:
            logger.warning(f"Cov池缓存写入失败: {e}")

    def _load_cache_fallback(self) -> pd.DataFrame:
        """从本地缓存加载全量池 (akshare API 失败时兜底)"""
        if not os.path.exists(_COV_CACHE_PATH):
            logger.warning("Cov池本地缓存不存在, 无法兜底")
            return self._cov_pool or pd.DataFrame()
        try:
            with open(_COV_CACHE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            items = data.get('items', [])
            if not items:
                logger.warning("Cov池缓存为空")
                return self._cov_pool or pd.DataFrame()
            df = pd.DataFrame(items)
            cached_at = data.get('cached_at_str', 'unknown')
            logger.info(f"Cov池缓存兜底: {len(df)} 只 (缓存于 {cached_at})")
            self._cov_pool = df
            return df
        except Exception as e:
            logger.error(f"Cov池缓存加载失败: {e}")
            return self._cov_pool or pd.DataFrame()

    def load_redeem_data(self, force: bool = False) -> pd.DataFrame:
        """
        加载强赎数据 (每日缓存)
        - 来源: 集思录, 仅用于强赎提醒, 不影响主数据链路
        - 返回: DataFrame [代码, 强赎状态, 强赎天计数, 最后交易日, 到期日, 剩余规模]
        """
        now = time.time()
        if not force and self._redeem_df is not None and (now - self._last_redeem_time) < 86400:
            return self._redeem_df

        try:
            import akshare as ak
            df = ak.bond_cb_redeem_jsl()
            if df is None or df.empty:
                logger.warning("bond_cb_redeem_jsl() 返回空")
                return self._redeem_df or pd.DataFrame()

            # 保留关键字段并统一代码格式
            keep = ['代码', '名称', '强赎状态', '强赎天计数', '最后交易日', '到期日', '剩余规模', '强赎触发价']
            available = [c for c in keep if c in df.columns]
            df = df[available].copy()
            df['code_num'] = df['代码'].astype(str).str.extract(r'(\d{6})', expand=False)

            self._redeem_df = df
            self._last_redeem_time = now
            logger.info(f"强赎数据加载: {len(df)} 只 (已公告{len(df[df['强赎状态']=='已公告强赎'])}只, 公告要强赎{len(df[df['强赎状态']=='公告要强赎'])}只)")
            return df
        except Exception as e:
            logger.error(f"加载强赎数据失败: {e}")
            return self._redeem_df or pd.DataFrame()

    def merge_and_filter(self) -> pd.DataFrame:
        """
        从全量池筛选转债
        1. 加载 cov 池
        2. 提取6位代码
        3. 应用筛选条件
        4. 按溢价率排序取 Top N
        """
        cov = self.load_cov_pool().copy()

        if cov.empty:
            return pd.DataFrame()

        # 提取6位数字代码
        cov['code_num'] = cov['code'].astype(str).str.extract(r'(\d{6})', expand=False)

        # 安全填充NaN
        for col in cov.columns:
            if 'float' in str(cov[col].dtype):
                cov[col] = cov[col].fillna(0.0)
            elif 'int' in str(cov[col].dtype):
                cov[col] = cov[col].fillna(0)
            elif 'object' in str(cov[col].dtype):
                cov[col] = cov[col].fillna('')

        # 应用筛选条件
        cfg = self.config
        mask = pd.Series(True, index=cov.index)

        if 'premium_ratio' in cov.columns:
            mask &= (cov['premium_ratio'] >= cfg['min_premium_ratio'])
            mask &= (cov['premium_ratio'] <= cfg['max_premium_ratio'])

        if 'issue_scale' in cov.columns:
            mask &= (cov['issue_scale'] <= cfg['max_issue_scale'])

        # 硬编码: 排除price≈100的占位符 (cov池对退市/未上市债券标记为100)
        if 'bond_price' in cov.columns:
            mask &= (cov['bond_price'] < 99.5) | (cov['bond_price'] > 100.5)

        filtered = cov[mask].copy()

        self._fused_pool = filtered
        total_active = ((cov['bond_price'] < 99.5) | (cov['bond_price'] > 100.5)).sum()
        self._active_count = total_active
        logger.info(f"筛选后: {len(filtered)} 只 (全量池{len(cov)}只, 活跃{total_active}只)")
        return filtered

    @property
    def active_count(self) -> int:
        """活跃转债数量 (排除退市/未上市的 price=100 占位符)"""
        return self._active_count

    def get_total_active(self) -> int:
        """获取活跃转债总数 (优先用实时行情数据, 回退到价格过滤)"""
        try:
            spot = ak.bond_zh_hs_cov_spot()
            if spot is not None and not spot.empty:
                return len(spot)
        except Exception:
            pass
        return self._active_count

    def get_monitor_list(self) -> list[dict]:
        """获取监控列表，返回每只转债的code+name+正股信息"""
        df = self.merge_and_filter()
        if df.empty:
            return []

        # 合并强赎数据
        redeem = self.load_redeem_data()
        has_redeem = not redeem.empty and 'code_num' in redeem.columns
        if has_redeem:
            redeem_map = {}
            for _, r in redeem.iterrows():
                redeem_map[r['code_num']] = {
                    'redeem_status': str(r.get('强赎状态', '')),
                    'redeem_count': str(r.get('强赎天计数', '')),
                    'last_trade_day': str(r.get('最后交易日', '')),
                    'expire_day': str(r.get('到期日', '')),
                }
        else:
            redeem_map = {}

        result = []
        for _, row in df.iterrows():
            prem = row.get('premium_ratio')
            if prem is not None:
                try:
                    prem = float(prem)
                    prem = prem if not pd.isna(prem) else None
                except (ValueError, TypeError):
                    prem = None
            bp = row.get('bond_price')
            if bp is not None:
                try:
                    bp = float(bp)
                    bp = bp if not pd.isna(bp) and bp > 0 else 0.0
                except (ValueError, TypeError):
                    bp = 0.0

            code_num = str(row.get('code_num', ''))
            item = {
                'code': str(row.get('code', '')),
                'code_num': code_num,
                'name': str(row.get('name', '')),
                'stock_code': str(row.get('stock_code', '')),
                'stock_name': str(row.get('stock_name', '')),
                'convert_price': float(row.get('convert_price', 0) or 0),
                'convert_value': float(row.get('convert_value', 0) or 0),
                'premium_ratio': prem,
                'bond_price': bp,
                'issue_scale': float(row.get('issue_scale', 0) or 0),
                # 强赎数据
                'redeem_status': redeem_map.get(code_num, {}).get('redeem_status', ''),
                'redeem_count': redeem_map.get(code_num, {}).get('redeem_count', ''),
                'last_trade_day': redeem_map.get(code_num, {}).get('last_trade_day', ''),
                'expire_day': redeem_map.get(code_num, {}).get('expire_day', ''),
            }
            result.append(item)
        return result
