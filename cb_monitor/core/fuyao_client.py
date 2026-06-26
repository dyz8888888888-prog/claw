"""
FuyaoClient — 同花顺金融数据 REST API 封装

封装 https://fuyao.aicubes.cn 的 A 股特色数据 + 概念板块指数接口:
- limit-up-pool:      涨停股票池 (封板时间/封单/连板/原因)
- limit-up-ladder:    30 日连板天梯 (seal_nextday 晋级标记)
- concept-catalog:    概念板块列表 (一次性全量)
- concept-snapshots:  概念指数实时行情快照 (批量)
- concept-historical: 概念指数历史 K 线

用法:
    client = FuyaoClient()
    pool = client.get_limit_up_pool()          # 今日涨停全量
    ladder = client.get_limit_up_ladder()      # 近30日连板矩阵
    concepts = client.get_concept_catalog()    # 全部概念板块列表
    snap = client.get_concept_snapshots(['886042.TI','886050.TI'])  # 概念实时行情
"""

import json
import logging
import time
import urllib.request
from typing import Optional

from config import CONFIG

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"

# 概念板块 thscode 缓存 (静态, 99%不变)
_CONCEPT_CACHE_PATH = None
_CONCEPT_CACHE: dict = {}  # thscode → {thscode, name}


class FuyaoClient:
    """同花顺金融数据 API 客户端"""

    def __init__(self):
        self._api_key = CONFIG.ext_api.fuyao_api_key
        self._base = CONFIG.ext_api.fuyao_base_url.rstrip('/')

    # ── 公共接口 ──────────────────────────────────────────

    def get_limit_up_pool(self, page: int = 1, size: int = 200,
                          sort_field: str = "limit_up_time",
                          sort_dir: str = "asc") -> Optional[dict]:
        """获取今日涨停股票池

        返回字段 (per stock):
            thscode, ticker, name, is_st, is_new,
            last_price, price_change_ratio_pct,
            limit_up_time, limit_up_reason,
            continue_day_text, continue_day_cnt,
            seal_money, max_seal_money
        """
        return self._get("/api/a-share/special-data/limit-up-pool", {
            "page": str(page),
            "size": str(size),
            "sort_field": sort_field,
            "sort_dir": sort_dir,
        })

    def get_limit_up_pool_all(self) -> list[dict]:
        """获取今日涨停全量 (自动分页, 返回所有股票列表)"""
        all_items = []
        page = 1
        while True:
            data = self.get_limit_up_pool(page=page, size=200,
                                          sort_field="limit_up_time",
                                          sort_dir="asc")
            if not data:
                break
            items = data.get("data", {}).get("item", [])
            if not items:
                break
            all_items.extend(items)
            pagination = data.get("data", {}).get("pagination", {})
            if pagination.get("page", 0) >= pagination.get("pages", 1):
                break
            page += 1
        return all_items

    def get_limit_up_ladder(self) -> Optional[dict]:
        """获取近30日连板天梯矩阵

        返回: 每日 → boards → {two_board, three_board, ..., seven_over}
        每个股票含: thscode, ticker, name, board_num, seal_nextday, sign_level
        """
        return self._get("/api/a-share/special-data/limit-up-ladder")

    def get_promotion_rate(self) -> float:
        """从连板天梯直接算晋级率

        逻辑: 取最近第二个完整交易日 (昨天)，统计其各板位中
        seal_nextday==True 的比例，即为连板晋级率。
        """
        data = self.get_limit_up_ladder()
        if not data:
            return 0

        items = data.get("data", {}).get("item", [])
        if len(items) < 2:
            # 不足两日数据，无法算
            return 0

        # items[0] = 今天 (seal_nextday 都是 null)
        # items[1] = 昨天 → 用 seal_nextday 统计
        yesterday = items[1]
        boards = yesterday.get("boards", {})

        total = 0
        sealed = 0
        for board_name in ["two_board", "three_board", "four_board",
                           "five_board", "six_board", "seven_over"]:
            for stock in boards.get(board_name, []):
                total += 1
                if stock.get("seal_nextday") is True:
                    sealed += 1

        if total == 0:
            return 0
        return round(sealed / total * 100, 1)

    # ── 概念板块指数 ──────────────────────────────────────

    def get_concept_catalog(self) -> list[dict]:
        """获取全部同花顺概念板块列表

        返回: [{thscode, name}, ...], 一次性全量 (约 400+ 概念)
        无分页, 无请求参数 (tag 默认 cn_concept)
        """
        data = self._get("/api/a-share-index/catalog/ths-index-list",
                         {"tag": "cn_concept"})
        if not data or data.get("code") != 0:
            logger.warning("概念板块列表获取失败")
            return []
        items = data.get("data", {}).get("item", [])
        logger.info(f"Fuyao 概念板块: {len(items)} 个")
        return items

    def get_concept_catalog_dict(self) -> dict[str, str]:
        """概念板块列表 → {thscode: name} 字典 (便于 O(1) 查找)"""
        items = self.get_concept_catalog()
        return {it["thscode"]: it["name"] for it in items if "thscode" in it}

    def get_concept_snapshots(self, thscodes: list[str]) -> dict[str, dict]:
        """批量获取概念指数实时行情快照

        参数:
            thscodes: 概念指数代码列表, 如 ['886042.TI', '886050.TI', ...]
                      单次最多约 100 个 (API 无明确上限, 实测 100+ 正常)

        返回: {thscode: {thscode, name, last_price, change_pct,
                         open/high/low/prev, volume, turnover}, ...}

        注意: 快照接口不返回 name, 从 catalog 缓存中补充。
        """
        if not thscodes:
            return {}

        # 确保 catalog 已加载 (用来补 name)
        name_map = getattr(self, '_concept_name_map', None)
        if not name_map:
            self._load_concept_name_map()
            name_map = getattr(self, '_concept_name_map', {})

        # 分批: 每批 80 (保守，避免 URL 过长)
        results = {}
        batch_size = 80
        for i in range(0, len(thscodes), batch_size):
            batch = thscodes[i:i + batch_size]
            data = self._get("/api/a-share-index/prices/snapshot",
                             {"thscodes": ",".join(batch)})
            if not data or data.get("code") != 0:
                logger.warning(f"概念行情快照失败: batch[{i}]")
                continue
            items = data.get("data", {}).get("item", [])
            for it in items:
                thscode = it.get("thscode", "")
                results[thscode] = {
                    "thscode": thscode,
                    "name": name_map.get(thscode, f"概念{thscode.split('.')[0]}"),
                    "last_price": it.get("last_price", 0),
                    "change_pct": it.get("price_change_ratio_pct", 0) or 0,
                    "open": it.get("open_price", 0),
                    "high": it.get("high_price", 0),
                    "low": it.get("low_price", 0),
                    "prev": it.get("prev_price", 0),
                    "volume": it.get("volume", 0),
                    "turnover": it.get("turnover", 0),
                }
        return results

    def _load_concept_name_map(self):
        """加载概念 thscode → name 映射 (缓存)"""
        try:
            items = self.get_concept_catalog()
            self._concept_name_map = {it["thscode"]: it["name"] for it in items}
        except Exception:
            self._concept_name_map = {}

    # ── 内部 ──────────────────────────────────────────────

    _MAX_RETRIES = 3
    _BASE_TIMEOUT = 8       # 单次请求超时(秒)
    _RETRY_BACKOFF = [1, 2, 4]  # 指数退避(秒)

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """统一 GET 请求, 带指数退避重试和熔断

        重试策略:
        - 最多 3 次重试 (共 4 次尝试)
        - 退避: 1s → 2s → 4s (URLError/socket.timeout)
        - 退避: 30s → 60s → 120s (HTTP 429 rate limit)
        - HTTP 4xx (非429)/5xx 不重试 (直接返回 None)
        - 总耗时上限约 8+30+8+60+8+120+8 ≈ 242s (429场景)
        """
        import urllib.error
        url = self._build_url(path, params)
        last_error = None

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"X-api-key": self._api_key})
                with urllib.request.urlopen(req, timeout=self._BASE_TIMEOUT) as resp:
                    body = resp.read().decode()
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                # HTTP 429 → 指数退避重试 (限流恢复通常需要 60-120s)
                if e.code == 429 and attempt < self._MAX_RETRIES:
                    wait = 30 * (2 ** attempt)  # 30, 60, 120
                    logger.warning(f"FuyaoClient HTTP 429 (rate limit): {path} — "
                                   f"等待 {wait}s 后重试 ({attempt+1}/{self._MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                # 其他 4xx/5xx 不重试
                logger.warning(f"FuyaoClient HTTP {e.code}: {path}")
                return None
            except json.JSONDecodeError as e:
                # 响应格式错误不重试
                logger.warning(f"FuyaoClient JSON decode error: {path} — {e}")
                return None
            except Exception as e:
                last_error = e
                if attempt < self._MAX_RETRIES:
                    wait = self._RETRY_BACKOFF[attempt]
                    logger.debug(f"FuyaoClient retry {attempt+1}/{self._MAX_RETRIES} "
                                 f"after {wait}s: {path} — {e}")
                    time.sleep(wait)
                    continue

        logger.warning(f"FuyaoClient exhausted retries: {path} — {last_error}")
        return None

    def _build_url(self, path: str, params: dict = None) -> str:
        if not params:
            return f"{self._base}{path}"
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}"
                      for k, v in params.items())
        return f"{self._base}{path}?{qs}"


# 全局单例
_client: Optional[FuyaoClient] = None


def get_fuyao_client() -> FuyaoClient:
    global _client
    if _client is None:
        _client = FuyaoClient()
    return _client
