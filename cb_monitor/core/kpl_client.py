"""
KPLClient — 开盘啦(kpl)数据源封装

封装 kaipanla-crawler, 提供:
- 日级 JSON 缓存 (data/kpl_cache.json)
- 自动回退到上一交易日
- 统一错误处理

用法:
    client = KPLClient()
    summary = client.get_daily_summary()       # 今日市场概况
    sectors = client.get_limit_up_sectors()    # 涨停原因板块
    sentiment = client.get_sentiment_6d()      # 六维情绪原始数据
"""

import json
import os
import logging
import sys
from datetime import datetime, timedelta
from collections import OrderedDict

logger = logging.getLogger(__name__)

# kaipanla-crawler 路径
_KPL_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    '..', 'kaipanla-crawler'
))


class KPLClient:
    """开盘啦数据源封装 (MIT协议免费)"""

    def __init__(self, cache_dir: str = None):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
        self._cache_dir = cache_dir
        self._cache_file = os.path.join(cache_dir, 'kpl_cache.json')
        self._cache: dict = {}
        self._crawler = None
        self._init_crawler()

    def _init_crawler(self):
        """延迟加载 kaipanla-crawler"""
        if _KPL_PATH not in sys.path:
            sys.path.insert(0, _KPL_PATH)
        try:
            from kaipanla_crawler import KaipanlaCrawler
            self._crawler = KaipanlaCrawler()
            logger.info("KPLClient: kaipanla-crawler 已就绪")
        except ImportError as e:
            logger.error(f"KPLClient: kaipanla-crawler 加载失败: {e}")
            raise

    def _load_cache(self) -> dict:
        if self._cache:
            return self._cache
        os.makedirs(self._cache_dir, exist_ok=True)
        try:
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
                logger.debug(f"KPL缓存加载: {len(self._cache)} 天")
        except (FileNotFoundError, json.JSONDecodeError):
            self._cache = {}
        return self._cache

    def _save_cache(self):
        os.makedirs(self._cache_dir, exist_ok=True)
        with open(self._cache_file, 'w', encoding='utf-8') as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def _get_date_key(self, date_str: str) -> str:
        """统一日期格式 YYYY-MM-DD"""
        return date_str

    def _get_recent_trade_date(self) -> str:
        """获取最近的交易日 (非周末)"""
        today = datetime.now()
        # 15:30 后用今天, 否则用昨天
        if today.hour < 15 or (today.hour == 15 and today.minute < 30):
            today -= timedelta(days=1)

        for i in range(5):
            d = today - timedelta(days=i)
            if d.weekday() < 5:  # 周一到周五
                return d.strftime('%Y-%m-%d')
        return today.strftime('%Y-%m-%d')

    def _cached_or_fetch(self, key: str, date: str, fetcher, force: bool = False):
        """缓存优先获取"""
        cache = self._load_cache()
        date_key = self._get_date_key(date)

        if not force and date_key in cache and key in cache[date_key]:
            logger.debug(f"KPL缓存命中: {date_key}/{key}")
            return cache[date_key][key]

        try:
            data = fetcher()
            if date_key not in cache:
                cache[date_key] = {}
            cache[date_key][key] = data
            self._cache = cache
            self._save_cache()
            logger.info(f"KPL缓存写入: {date_key}/{key}")
            return data
        except Exception as e:
            logger.warning(f"KPL获取失败 {date_key}/{key}: {e}")
            # 尝试回退缓存
            if date_key in cache and key in cache[date_key]:
                return cache[date_key][key]
            raise

    # ========== 公开接口 ==========

    def get_daily_summary(self, date: str = None) -> dict:
        """获取单日市场概况: 涨跌停/涨跌家数/上证指数/连板分布/大幅回撤
        返回 dict:
          { 日期, 涨停数, 实际涨停, 跌停数, 上涨家数, 下跌家数, 平盘家数,
            上证指数, 涨跌幅, 成交额, 首板数量, 2连板数量, 3连板数量,
            4连板以上数量, 连板率, 大幅回撤家数 }
        """
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            series = self._crawler.get_daily_data(date)
            if series.empty:
                return {}
            d = series.to_dict()
            # 统一字段名
            return {
                '日期': str(d.get('日期', date)),
                '涨停数': int(d.get('涨停数', 0)),
                '实际涨停': int(d.get('实际涨停', 0)),
                '跌停数': int(d.get('跌停数', 0)),
                '实际跌停': int(d.get('实际跌停', 0)),
                '上涨家数': int(d.get('上涨家数', 0)),
                '下跌家数': int(d.get('下跌家数', 0)),
                '平盘家数': int(d.get('平盘家数', 0)),
                '上证指数': float(d.get('上证指数', 0)),
                '涨跌幅': str(d.get('涨跌幅', '')),
                '成交额': int(d.get('成交额', 0)),
                '首板数量': int(d.get('首板数量', 0)),
                '2连板数量': int(d.get('2连板数量', 0)),
                '3连板数量': int(d.get('3连板数量', 0)),
                '4连板以上数量': int(d.get('4连板以上数量', 0)),
                '连板率': round(float(d.get('连板率', 0)), 2),
                '大幅回撤家数': int(d.get('大幅回撤家数', 0)),
            }
        return self._cached_or_fetch('daily_summary', date, fetcher)

    def get_limit_up_sectors(self, date: str = None) -> dict:
        """获取涨停原因板块 (核心接口!)
        返回 dict:
          { summary: {上涨家数, 下跌家数, 涨停数, 跌停数, 涨跌比},
            sectors: [{sector_code, sector_name, stock_count,
                       stocks: [{股票代码, 股票名称, 涨停价, 流通市值, 连板天数,
                                 概念标签, 封单额, 主力资金, 涨停时间, 涨停原因, 主题}]}] }
        """
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            return self._crawler.get_sector_ranking(date, timeout=30)

        return self._cached_or_fetch('limit_up_sectors', date, fetcher)

    def get_consecutive_limit_up(self, date: str = None) -> dict:
        """获取连板梯队详情
        返回: {date, max_consecutive, max_consecutive_stocks, max_consecutive_concepts, ladder: {板数: [stocks]}}
        """
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            return self._crawler.get_consecutive_limit_up(date, timeout=30)

        return self._cached_or_fetch('consecutive', date, fetcher)

    def get_new_high_count(self, date: str = None) -> int:
        """获取百日新高新增数 (单日)"""
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            try:
                nh = self._crawler.get_new_high_data(date, timeout=30)
                if nh is None:
                    return 0
                if hasattr(nh, 'empty') and nh.empty:
                    return 0
                if hasattr(nh, 'item'):
                    val = nh.item()
                    return int(val) if val is not None and val == val else 0  # NaN check
                if hasattr(nh, 'iloc') and len(nh) > 0:
                    return int(nh.iloc[0]) if nh.iloc[0] is not None else 0
                return int(nh) if nh else 0
            except (ValueError, TypeError, AttributeError):
                return 0

        return self._cached_or_fetch('new_high', date, fetcher)

    def get_limit_up_ladder_stats(self, date: str = None) -> dict:
        """获取连板梯队统计 (含炸板率/晋级率/昨日涨停表现)
        返回: {一板, 二板, 三板, 高度板, 连板率, 昨日首板今涨, 昨日首板今跌,
                今日炸板率, 昨日涨停今表现, 昨日连板今表现, 昨日破板今表现, 市场评价}
        """
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            df = self._crawler.get_limit_up_ladder(date)
            if df.empty:
                return {}
            d = df.iloc[0].to_dict()
            return {
                '一板': int(d.get('一板', 0)),
                '二板': int(d.get('二板', 0)),
                '三板': int(d.get('三板', 0)),
                '高度板': int(d.get('高度板', 0)),
                '连板率': round(float(d.get('连板率(%)', 0)), 2),
                '昨日首板今日上涨数': int(d.get('昨日首板今日上涨数', 0)),
                '昨日首板今日下跌数': int(d.get('昨日首板今日下跌数', 0)),
                '今日涨停破板率': round(float(d.get('今日涨停破板率(%)', 0)), 2),
                '昨日涨停今表现': round(float(d.get('昨日涨停今表现(%)', 0)), 2),
                '昨日连板今表现': round(float(d.get('昨日连板今表现(%)', 0)), 2),
                '昨日破板今表现': round(float(d.get('昨日破板今表现(%)', 0)), 2),
                '市场评价': str(d.get('市场评价', '')),
            }
        return self._cached_or_fetch('ladder_stats', date, fetcher)

    def get_broken_limit_up(self, date: str = None) -> list:
        """获取历史炸板股列表"""
        if date is None:
            date = self._get_recent_trade_date()

        def fetcher():
            return self._crawler.get_historical_broken_limit_up(date, timeout=30)

        return self._cached_or_fetch('broken_limit_up', date, fetcher)

    def get_abnormal_stocks(self):
        """获取实时异动个股 (不缓存)"""
        try:
            df = self._crawler.get_abnormal_stocks(timeout=15)
            if df is not None and not df.empty:
                return df.to_dict('records')
            return []
        except Exception as e:
            logger.warning(f"异动个股获取失败: {e}")
            return []

    def get_sentiment_6d_raw(self, date: str = None) -> dict:
        """获取六维情绪计算所需的原始数据包 (含容错降级)

        容错策略: 当天数据不完整时 (炸板率为0或连板为空), 自动回退到最近有效交易日,
        并在返回数据中标注 _fallback_date.
        """
        if date is None:
            date = self._get_recent_trade_date()

        cache = self._load_cache()
        date_key = self._get_date_key(date)
        if date_key in cache and 'raw_6d' in cache[date_key]:
            return cache[date_key]['raw_6d']

        raw = {
            'summary': self.get_daily_summary(date),
            'ladder': self.get_limit_up_ladder_stats(date),
            'new_high': self.get_new_high_count(date),
            'broken_limit': self.get_broken_limit_up(date),
            'consecutive': self.get_consecutive_limit_up(date),
        }

        # 完整性校验 + 自动降级
        is_complete = self._is_data_complete(raw)
        if not is_complete:
            fallback_date = self._find_last_valid_date(date_key)
            if fallback_date:
                logger.warning(f"KPL {date} 数据不完整, 回退到 {fallback_date}")
                fb_cache = cache.get(fallback_date, {})
                raw['_fallback_date'] = fallback_date
                raw['_data_complete'] = False
                # 逐字段降级: 只替换缺失的字段
                if not raw['ladder'] and fb_cache.get('ladder_stats'):
                    raw['ladder'] = fb_cache['ladder_stats']
                if not raw['consecutive'] and fb_cache.get('consecutive'):
                    raw['consecutive'] = fb_cache['consecutive']
                # broken_limit 为空列表也算缺失 (炸板数应为0但实际非空日期应该有数据)
                if (not raw['broken_limit'] or len(raw['broken_limit']) == 0) and fb_cache.get('broken_limit_up'):
                    raw['broken_limit'] = fb_cache['broken_limit_up']
            else:
                raw['_data_complete'] = False
        else:
            raw['_data_complete'] = True

        # 缓存
        if date_key not in cache:
            cache[date_key] = {}
        cache[date_key]['raw_6d'] = raw
        self._cache = cache
        self._save_cache()

        return raw

    def _is_data_complete(self, raw: dict) -> bool:
        """检查六维数据完整性: ladder有连板率, consecutive有最高板, broken_limit非空"""
        ladder = raw.get('ladder', {})
        if not ladder:
            return False
        # 关键字段: 连板率必须>0 (炸板率也应有值)
        if ladder.get('连板率', 0) == 0 and ladder.get('今日涨停破板率', 0) == 0:
            # 所有板数为0 → 数据未沉淀
            if ladder.get('一板', 0) == 0 and ladder.get('二板', 0) == 0:
                return False
        consecutive = raw.get('consecutive', {})
        if not consecutive:
            return False
        # 最高板为0且连板梯为空 → 数据不完整
        if consecutive.get('max_consecutive', 0) == 0 and not consecutive.get('ladder'):
            return False
        return True

    def _find_last_valid_date(self, date_key: str) -> str:
        """查找缓存中最近一个有效交易日"""
        cache = self._load_cache()
        dates = sorted([k for k in cache.keys() if k != date_key and len(k) == 10], reverse=True)
        for d in dates:
            entry = cache[d]
            if 'ladder_stats' in entry or 'consecutive' in entry:
                return d
        return ""

    def flush(self):
        """清空内存缓存 (强制下次重新读取文件)"""
        self._cache = {}
