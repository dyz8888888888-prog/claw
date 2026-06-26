"""
共识阶段追踪器 — ConceptConsensusTracker
+
概念扩散指标 — ConceptDiffusion
+
概念相对强度 — ConceptRRG
+
概念板块指数快照 — ConceptIndexFeed (Fuyao 直出, 替代个股聚合)

每个概念板块追踪:
  - 7 个共识阶段 (ConsensusTracker)
  - 扩散指标: 上涨股票占比, 20日平滑 (Diffusion)
  - RRG 四象限: 领先/改善/转弱/落后 (RRG)
  - 概念指数实时行情: Fuyao a-share-index/prices/snapshot (IndexFeed)

数据源:
  转债池内正股 → TDX 快照 (免费, 每轮)
  全市场成分股  → 同花顺 API (缓存, 活跃概念时1次/轮批量查行情)
  概念板块指数  → Fuyao 同花顺指数行情快照 (每60秒批量刷新)
"""

import json
import os
import glob
import time
import logging
from collections import defaultdict, deque
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "data", "concept_stocks.json")

STAGE_NAMES = {
    0: "沉寂", 1: "酝酿", 2: "冲锋", 3: "封板",
    4: "扩散", 5: "显性化", 6: "过热", 7: "退潮",
}


def _load_full_concept_stocks() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """加载全市场概念成分股缓存"""
    if not os.path.exists(CACHE_PATH):
        return {}, {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    concept_stocks = data.get("concept_stocks", {})  # concept → [thscode, ...]
    # 反向索引: ticker → [concept, ...]
    ticker_to_concepts = defaultdict(list)
    for concept, stocks in concept_stocks.items():
        for thscode in stocks:
            ticker = thscode.split(".")[0] if "." in thscode else thscode
            ticker_to_concepts[ticker].append(concept)
    return concept_stocks, dict(ticker_to_concepts)


def _fetch_batch_snapshot(tickers: list[str]) -> dict[str, float]:
    """批量取全市场正股行情 (通达信本地), 返回 {ticker: change_pct}"""
    if not tickers:
        return {}
    try:
        from core.data_fusion import TdxClient
        client = TdxClient.get()
        df = client.quotes(tickers[:100])  # TDX 单次上限 ~100
        result = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row.get('code', ''))
                price = float(row.get('price', 0) or 0)
                close = float(row.get('last_close', 0) or 0)
                if close > 0 and price > 0:
                    pct = round((price - close) / close * 100, 2)
                    result[code] = pct
        return result
    except Exception as e:
        logger.debug(f"TDX批量快照失败: {e}")
        return {}


class ConceptConsensusTracker:
    """概念共识阶段追踪器 — 池内+全市场双数据源"""

    def __init__(self, concept_map: dict[str, list[str]], max_concepts: int = 50):
        self._concept_map = concept_map
        self.max_concepts = max_concepts
        self._states: dict[str, dict] = {}
        self._code_to_concepts: dict[str, list[str]] = defaultdict(list)
        for code, concepts in concept_map.items():
            for c in concepts:
                self._code_to_concepts[code].append(c)

        # 全市场概念成分股缓存
        self._full_stocks: dict[str, list[str]] = {}  # concept → [ticker, ...]
        self._full_loaded = False
        self._api_calls = 0

        # 增量缓存: 全市场正股行情仅刷新变化的概念
        self._full_price_cache: dict[str, dict] = {}
        self._full_cache_ttl: float = 60.0

        # 问财概念龙头数据 (开盘前预加载)
        self._leader_tickers: dict[str, set[str]] = {}  # concept → {ticker, ...}
        self._ticker_leaders: dict[str, set[str]] = {}  # ticker → {concept, ...}
        self._leaders_loaded = False

    def load_full_market(self):
        """加载全市场概念成分股 (启动时调用)"""
        if self._full_loaded:
            return
        stocks, _ = _load_full_concept_stocks()
        if stocks:
            self._full_stocks = stocks
            self._full_loaded = True
            logger.info(f"全市场概念加载: {len(stocks)} 个, "
                        f"均值 {sum(len(v) for v in stocks.values())//max(len(stocks),1)} 只/概念")
        # 同时尝试加载问财龙头
        self.load_leaders()

    def load_leaders(self):
        """加载问财概念龙头数据 (开盘前由 query_concept_leaders.py 生成)"""
        if self._leaders_loaded:
            return
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        files = sorted(glob.glob(os.path.join(data_dir, "concept_leaders_*.json")), reverse=True)
        if not files:
            logger.info("无问财龙头数据, 跳过")
            self._leaders_loaded = True
            return

        try:
            with open(files[0], "r", encoding="utf-8") as f:
                data = json.load(f)

            leaders_map = data.get("leaders", {})
            loaded = 0
            for concept, leaders in leaders_map.items():
                tickers = {ldr["code"] for ldr in leaders if ldr.get("code")}
                if tickers:
                    self._leader_tickers[concept] = tickers
                    for t in tickers:
                        if t not in self._ticker_leaders:
                            self._ticker_leaders[t] = set()
                        self._ticker_leaders[t].add(concept)
                    loaded += 1

            self._leaders_loaded = True
            logger.info(f"问财龙头加载: {loaded}/{len(leaders_map)} 个概念, "
                        f"{len(self._ticker_leaders)} 只龙头正股")
        except Exception as e:
            logger.warning(f"问财龙头加载失败: {e}")
            self._leaders_loaded = True

    def update(self, snapshots: dict, stock_of: dict[str, str]):
        """每轮 tick 更新所有概念的共识阶段"""
        now_ts = time.time()

        # 1. 构建概念 → 债列表
        concept_bonds: dict[str, list[dict]] = defaultdict(list)
        for code, snap in snapshots.items():
            concepts = self._code_to_concepts.get(code, [])
            if not concepts:
                continue
            sc_pct = getattr(snap, 'stock_change_pct', None) or 0
            cb_pct = getattr(snap, 'change_pct', 0) or 0
            info = {
                'code': code,
                'name': getattr(snap, 'name', '') or code,
                'stock_code': stock_of.get(code, ''),  # 正股代码 (问财龙头匹配用)
                'stock_pct': sc_pct,
                'cb_pct': cb_pct,
                'volume': getattr(snap, 'volume', 0),
                'amount': getattr(snap, 'amount', 0),
                'premium': getattr(snap, 'premium_ratio', None),
            }
            for c in concepts:
                concept_bonds[c].append(info)

        # 2. 全市场补全 (增量: 仅刷新阶段变化或缓存过期的概念)
        self._api_calls = 0
        concept_full_prices: dict[str, dict[str, float]] = {}  # concept → {ticker: pct}
        if self._full_loaded:
            # 收集池内债的正股 ticker (用于去重)
            cb_stock_tickers: set[str] = set()
            for bonds in concept_bonds.values():
                for b in bonds:
                    cb_stock_tickers.add(b.get('stock_code', ''))

            # 粗判活跃概念 + 判断是否需要刷新全市场数据
            refresh_concepts = []
            now = time.time()
            for concept, bonds in concept_bonds.items():
                if not any(b['stock_pct'] >= 1.0 for b in bonds):
                    continue
                cache = self._full_price_cache.get(concept)
                prev_stage = self._states.get(concept, {}).get('stage', 0)
                need_refresh = (
                    cache is None or                          # 首次
                    (now - cache.get('ts', 0)) > self._full_cache_ttl or  # 过期
                    prev_stage != self._compute_stage_fast(concept, bonds)  # 阶段变化
                )
                if need_refresh:
                    refresh_concepts.append(concept)

            # 按概念分组 → 批量查 TDX → 拆回各概念
            concept_tickers: dict[str, list[str]] = {}
            all_query_tickers: list[str] = []
            for concept in refresh_concepts[:8]:
                full_stocks = self._full_stocks.get(concept, [])
                tickers_only = [s.split('.')[0] for s in full_stocks]
                no_cb = [t for t in tickers_only if t not in cb_stock_tickers]
                picked = no_cb[:12]
                if picked:
                    concept_tickers[concept] = picked
                    all_query_tickers.extend(picked)

            if all_query_tickers:
                all_prices = _fetch_batch_snapshot(all_query_tickers)
                for concept, tickers in concept_tickers.items():
                    cp = {t: pct for t, pct in all_prices.items() if t in tickers}
                    if cp:
                        self._full_price_cache[concept] = {
                            'prices': cp,
                            'ts': now,
                            'tickers': tickers,
                        }

            # 合并缓存 + 本轮查询 (概念级: 缓存优先)
            for concept, bonds in concept_bonds.items():
                if not any(b['stock_pct'] >= 1.0 for b in bonds):
                    continue
                if concept in refresh_concepts and concept in concept_tickers:
                    concept_full_prices[concept] = self._full_price_cache.get(
                        concept, {}).get('prices', {})
                else:
                    cached = self._full_price_cache.get(concept, {}).get('prices')
                    if cached:
                        concept_full_prices[concept] = cached

        # 3. 逐概念判定阶段 (全市场数据参与龙头判定)
        for concept, bonds in concept_bonds.items():
            n_cb = len(bonds)
            full_stocks = self._full_stocks.get(concept, [])
            full_count = len(full_stocks)

            # 全市场补全: 池内少但全市场大的概念, 注入全市场正股行情
            blind_spot = max(0, full_count - n_cb)
            my_full_prices = concept_full_prices.get(concept, {})

            # 构建全市场补全债列表 (只用于阶段判定, 不参与显示统计)
            full_bonds = []
            if blind_spot > 0 and my_full_prices:
                for ticker, pct in my_full_prices.items():
                    full_bonds.append({
                        'code': f'full_{ticker}',
                        'name': ticker,
                        'stock_pct': pct,
                        'cb_pct': 0,
                        'volume': 0,
                        'amount': 0,
                        'premium': None,
                        '_is_full': True,  # 标记为全市场
                    })

            stage = self._compute_stage(concept, bonds, full_bonds, now_ts)
            stage['full_market_stocks'] = full_count
            stage['cb_pool_stocks'] = n_cb
            stage['blind_spot'] = blind_spot

            if concept not in self._states:
                self._states[concept] = {}
            old_stage = self._states[concept].get('stage', 0)
            self._states[concept].update(stage)
            self._states[concept]['last_update'] = now_ts
            if old_stage != stage['stage']:
                logger.debug(f"[共识] {concept}: {STAGE_NAMES[old_stage]}"
                             f"→{STAGE_NAMES[stage['stage']]} "
                             f"dragon={stage.get('dragon_name','')} "
                             f"+{stage.get('dragon_sc_pct',0):.1f}% "
                             f"limit_up={stage.get('limit_up_count',0)}")

        # 限制数量
        if len(self._states) > self.max_concepts:
            oldest = sorted(self._states.items(),
                            key=lambda x: x[1].get('last_update', 0))[0][0]
            del self._states[oldest]

    def _compute_stage_fast(self, concept: str, bonds: list[dict], now_ts: float = 0) -> int:
        """轻量版: 仅返回阶段号, 不构造完整结果 (用于判断是否需要刷新全市场)"""
        if not bonds:
            return 0
        sorted_bonds = sorted(bonds, key=lambda b: -b['stock_pct'])
        dragon = sorted_bonds[0]
        d_sc = dragon['stock_pct']

        limit_up_count = sum(1 for b in bonds if b['stock_pct'] >= 9.5)
        followers = [b for b in sorted_bonds[1:]
                     if b['stock_pct'] >= 1.0 and b['cb_pct'] < 2.0]

        if d_sc < 2.0:
            return 0
        elif d_sc < 7.0:
            return 1
        elif d_sc < 9.5:
            return 2
        elif limit_up_count == 1 and followers:
            return 3
        elif limit_up_count >= 1 and len(followers) >= 2:
            return 4
        else:
            return 3

    def _compute_stage(self, concept: str, cb_bonds: list[dict],
                       full_bonds: list[dict], now_ts: float) -> dict:
        """计算单个概念的共识阶段 (全市场参与龙头判定)"""
        if not cb_bonds:
            return {'stage': 0, 'dragon_code': '', 'dragon_name': '',
                    'dragon_sc_pct': 0, 'limit_up_count': 0, 'follower_count': 0,
                    'avg_premium': 0, 'dragons': []}

        # --- 龙头判定: 全市场 + CB池 混合排序 ---
        all_bonds = cb_bonds + full_bonds
        all_sorted = sorted(all_bonds, key=lambda b: -b['stock_pct'])
        true_dragon = all_sorted[0]  # 真正的龙一 (可能是全市场)
        true_d_sc = true_dragon['stock_pct']

        # CB池内的龙 (用于显示, 优先取池内)
        cb_pool_dragon = cb_bonds[0] if cb_bonds else None
        cb_sorted = sorted(cb_bonds, key=lambda b: -b['stock_pct'])
        if cb_pool_dragon is None:
            cb_pool_dragon = cb_sorted[0] if cb_sorted else true_dragon

        # 全市场龙比池内龙强多少
        cb_best_sc = max((b['stock_pct'] for b in cb_bonds), default=0)
        full_better = true_d_sc - cb_best_sc if true_d_sc > cb_best_sc else 0

        # --- 龙一龙二龙三 (仅池内, 避免全市场假名) ---
        dragons = []
        for rank, b in enumerate(cb_sorted[:3], 1):
            label = "滞后" if b['cb_pct'] < 2.0 and b['stock_pct'] > 1.0 else (
                "高溢价" if (b.get('premium') or 0) > 40 else "同步")
            dragons.append({
                'rank': rank,
                'code': b['code'],
                'name': b['name'],
                'stock_pct': round(b['stock_pct'], 2),
                'cb_pct': round(b['cb_pct'], 2),
                'premium': round(b.get('premium', 0) or 0, 1),
                'label': label,
            })

        # 如果全市场有更强的龙, 追加标注
        if full_better > 2.0:  # 全市场龙比池内龙强 2%+
            dragons.append({
                'rank': 0,  # 0 = 全市场龙
                'code': true_dragon['code'],
                'name': f"全市场{true_dragon['name']}",
                'stock_pct': round(true_d_sc, 2),
                'cb_pct': 0,
                'premium': 0,
                'label': f"龙头+{full_better:.1f}%",
            })

        # --- 龙二龙三 (= 跟班): 仅池内, 需正股涨且转债滞后 ---
        followers = [b for b in cb_sorted[1:]
                     if b['stock_pct'] >= 1.0 and b['cb_pct'] < 2.0]

        # --- 涨停计数: 仅池内 (全市场 cb_pct 恒为 0, 不能拿来数) ---
        limit_up_count = sum(1 for b in cb_bonds if b['stock_pct'] >= 9.5)

        # 均价: 仅池内 (全市场premium=None不参与)
        premiums = [b['premium'] for b in cb_bonds if b['premium'] is not None]
        avg_premium = round(sum(premiums) / len(premiums), 1) if premiums else 0

        d_cb = cb_pool_dragon['cb_pct']

        # --- 问财龙头匹配: 池内龙的正股是否是官方认定的龙头 ---
        dragon_stock = cb_pool_dragon.get('stock_code', '')
        known_leaders = self._leader_tickers.get(concept, set())
        leader_match = dragon_stock in known_leaders if dragon_stock and known_leaders else None
        # 池内有多少只债的正股是官方龙头
        pool_leader_count = sum(1 for b in cb_bonds
                                if b.get('stock_code', '') in known_leaders)

        # --- 阶段判定: 用全市场真龙头做阈值 ---
        if true_d_sc < 2.0:
            stage = 0  # 沉寂
        elif true_d_sc < 7.0:
            stage = 1  # 酝酿
        elif true_d_sc < 9.5:
            stage = 2  # 冲锋
        elif limit_up_count == 1 and followers:
            stage = 3  # 封板 (龙一封板, 龙二在跟)
        elif limit_up_count >= 1 and len(followers) >= 2:
            stage = 4  # 扩散
        elif avg_premium > 30 or (true_d_sc >= 9.5 and d_cb > 8):
            stage = 5  # 显性化
        elif true_d_sc >= 9.5 and d_cb < 0:
            stage = 6  # 过热
        else:
            stage = 3  # 默认封板

        return {
            'stage': stage,
            'stage_name': STAGE_NAMES[stage],
            'dragon_code': cb_pool_dragon['code'],
            'dragon_name': cb_pool_dragon['name'],
            'dragon_sc_pct': round(true_d_sc, 2),  # 真龙头涨幅
            'dragon_cb_pct': round(d_cb, 2),
            'leader_match': leader_match,           # 池内龙是否匹配问财官方龙头
            'pool_leader_count': pool_leader_count,  # 池内官方龙头数量
            'limit_up_count': limit_up_count,
            'follower_count': len(followers),
            'total_bonds': len(cb_bonds) + len(full_bonds),
            'avg_premium': avg_premium,
            'dragons': dragons,  # 龙一龙二龙三 + 可选全市场标注
            'full_leader_better': round(full_better, 2),  # 全市场龙比池内强多少
        }

    def get_stage(self, concept: str) -> int:
        return self._states.get(concept, {}).get('stage', 0)

    def get_concepts_by_stage(self, min_stage: int = 0, max_stage: int = 7) -> list[dict]:
        """按阶段筛选概念"""
        result = []
        for concept, state in self._states.items():
            s = state.get('stage', 0)
            if min_stage <= s <= max_stage:
                result.append({'concept': concept, **state})
        return sorted(result, key=lambda x: -x['stage'])

    def get_all(self) -> dict:
        return dict(self._states)

    def get_top_consensus(self, n: int = 12) -> list[dict]:
        """Top N 共识阶段最高的概念 (排除沉寂)"""
        active = [{'concept': c, **s}
                  for c, s in self._states.items()
                  if s.get('stage', 0) >= 1]
        active.sort(key=lambda x: -x['stage'])
        return active[:n]


# ============================================================
# 概念扩散指标 — ConceptDiffusion
# ============================================================

class ConceptDiffusion:
    """概念扩散指标 — DI = (上涨成分股数 / 总成分股数) × 100
    
    基于全市场THS成分股抽样, 20期SMA平滑。
    确定性分层抽样, 同一概念每次样本一致。
    """

    def __init__(self, window: int = 20, sample_size: int = 80):
        self._window = window
        self._sample_size = sample_size
        self._history: dict[str, list[float]] = defaultdict(list)
        self._current: dict[str, float] = {}
        self._full_stocks: dict[str, list[str]] = {}
        self._refresh_queue: list[str] = []
        self._samples: dict[str, list[str]] = {}  # 预计算抽样, 确定性

    def set_full_stocks(self, full_stocks: dict[str, list[str]]):
        """注入全市场概念成分股, 预计算每个概念的固定抽样"""
        self._full_stocks = full_stocks
        self._refresh_queue = list(full_stocks.keys())

        # 确定性分层抽样: 用概念名 hash 做偏移
        for concept, stocks in full_stocks.items():
            tickers = [s.split('.')[0] for s in stocks]
            n = len(tickers)
            if n <= self._sample_size:
                self._samples[concept] = tickers
            else:
                step = max(1, n // self._sample_size)
                offset = hash(concept) % step
                self._samples[concept] = tickers[offset::step][:self._sample_size]

    def update(self, snapshots: dict, concept_map: dict[str, list[str]],
               cb_stock_tickers: set = None):
        """每轮更新扩散率 (轮转抽样, 每轮一个概念)"""
        # 1. 每轮刷新一个概念的全市场数据 (轮转)
        if self._refresh_queue and self._samples:
            concept = self._refresh_queue.pop(0)
            self._refresh_queue.append(concept)
            self._sample_concept(concept)

        # 2. CB池补充未被全市场覆盖的概念
        for code, snap in snapshots.items():
            for c in (concept_map.get(code, []) or []):
                if c in self._current:
                    continue
                sc_pct = getattr(snap, 'stock_change_pct', 0) or 0
                self._history[c].append(1.0 if sc_pct > 0 else 0.0)
                if len(self._history[c]) > self._window:
                    self._history[c].pop(0)

    def _sample_concept(self, concept: str):
        """从预计算的固定样本中查询TDX, 计算扩散率"""
        sample = self._samples.get(concept, [])
        if not sample:
            return

        prices = _fetch_batch_snapshot(sample)
        if not prices:
            return

        # DI = (上涨数 / 抽样数) × 100
        up = sum(1 for pct in prices.values() if pct > 0)
        raw = up / len(prices) * 100

        self._history[concept].append(raw)
        if len(self._history[concept]) > self._window:
            self._history[concept].pop(0)

        hist = self._history[concept]
        self._current[concept] = sum(hist) / len(hist)

    def get(self, concept: str) -> float:
        return self._current.get(concept, 0)

    def get_top(self, n: int = 10, min_samples: int = 3) -> list[tuple[str, float]]:
        items = [(c, v) for c, v in self._current.items()
                 if len(self._history.get(c, [])) >= min_samples]
        items.sort(key=lambda x: -x[1])
        return items[:n]

    def get_all(self) -> dict[str, float]:
        return dict(self._current)


# ============================================================
# 概念相对旋转图 RRG — ConceptRRG (JdK标准公式)
# ============================================================

class ConceptRRG:
    """相对旋转图 — JdK RS-Ratio + RS-Momentum, StockCharts标准公式
    
    公式:
      RS = (概念收益率 / 基准收益率) × 100
      raw_RS-Ratio = 10日 SMA(RS)
      RS-Ratio = 100 + (raw - 截面均值) / 截面标准差   [z-score归一化, 原点100]
      RS-Momentum = RS-Ratio(t) - RS-Ratio(t-1)       [同法归一化]
    
    四象限 (原点 100,100):
      领先: RS-Ratio > 100, Mom > 100  → 趋势跟随
      转弱: RS-Ratio > 100, Mom < 100  → 减仓止盈 (动量先转向!)
      落后: RS-Ratio < 100, Mom < 100  → 不参与
      改善: RS-Ratio < 100, Mom > 100  → 小仓试错
    """

    QUADRANTS = {
        (True, True):   "领先",
        (True, False):  "转弱",
        (False, False): "落后",
        (False, True):  "改善",
    }

    def __init__(self, rs_window: int = 10):
        self._rs_window = rs_window
        self._rs_raw: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=rs_window + 5))
        self._ratio_history: dict[str, list[float]] = defaultdict(list)
        self._current: dict[str, dict] = {}

    def update(self, snapshots: dict, concept_map: dict[str, list[str]]):
        """每轮更新 RS → RS-Ratio → RS-Momentum → 四象限"""
        now = time.time()

        # 1. 基准 = CB池正股均涨幅 (代理全A)
        all_pcts = [getattr(s, 'stock_change_pct', 0) or 0
                    for s in snapshots.values()]
        benchmark = sum(all_pcts) / len(all_pcts) if all_pcts else 0

        # 2. 每个概念的 RS = concept_return / benchmark_return × 100
        concept_rs: dict[str, float] = {}
        concept_pcts = defaultdict(list)
        for code, snap in snapshots.items():
            for c in (concept_map.get(code, []) or []):
                concept_pcts[c].append(getattr(snap, 'stock_change_pct', 0) or 0)

        for c, pcts in concept_pcts.items():
            avg = sum(pcts) / len(pcts)
            if benchmark != 0:
                concept_rs[c] = (avg / benchmark) * 100 if benchmark != 0 else 100
            else:
                concept_rs[c] = 100
            self._rs_raw[c].append(concept_rs[c])

        # 3. raw RS-Ratio = 10日 SMA(RS)
        raw_ratios = {}
        for c, deq in self._rs_raw.items():
            if len(deq) >= self._rs_window:
                raw_ratios[c] = sum(deq) / len(deq)

        if not raw_ratios:
            return

        # 4. 横截面 z-score 归一化 → RS-Ratio (原点 100)
        values = list(raw_ratios.values())
        mean_v = sum(values) / len(values)
        std_v = (sum((v - mean_v) ** 2 for v in values) / len(values)) ** 0.5
        if std_v < 0.001:
            std_v = 1.0

        for c, raw in raw_ratios.items():
            rs_ratio = 100 + (raw - mean_v) / std_v
            self._ratio_history[c].append(rs_ratio)

        # 5. RS-Momentum = RS-Ratio(t) - RS-Ratio(t-1), 同样横截面归一化
        raw_momenta = {}
        for c, hist in self._ratio_history.items():
            if len(hist) >= 2:
                raw_momenta[c] = hist[-1] - hist[-2]
            if len(hist) > 20:
                self._ratio_history[c] = hist[-20:]

        if raw_momenta:
            m_vals = list(raw_momenta.values())
            m_mean = sum(m_vals) / len(m_vals)
            m_std = (sum((v - m_mean) ** 2 for v in m_vals) / len(m_vals)) ** 0.5
            if m_std < 0.001:
                m_std = 1.0

            for c, raw_mom in raw_momenta.items():
                rs_momentum = 100 + (raw_mom - m_mean) / m_std
                rs_ratio = self._ratio_history[c][-1] if self._ratio_history[c] else 100
                quadrant = self.QUADRANTS.get(
                    (rs_ratio >= 100, rs_momentum >= 100), "落后")
                self._current[c] = {
                    'rs_ratio': round(rs_ratio, 2),
                    'rs_momentum': round(rs_momentum, 2),
                    'quadrant': quadrant,
                }

    def get(self, concept: str) -> dict:
        return self._current.get(concept, {})

    def get_quadrant(self, concept: str) -> str:
        return self._current.get(concept, {}).get('quadrant', '落后')

    def get_by_quadrant(self) -> dict[str, list[str]]:
        groups = {'领先': [], '改善': [], '转弱': [], '落后': []}
        for c, v in self._current.items():
            groups[v.get('quadrant', '落后')].append(c)
        return groups

    def get_top_leading(self, n: int = 10) -> list[tuple[str, dict]]:
        items = [(c, v) for c, v in self._current.items()
                 if v.get('quadrant') == '领先']
        items.sort(key=lambda x: -(x[1].get('rs_ratio', 0)))
        return items[:n]


# ============================================================
# 概念板块指数快照 — ConceptIndexFeed (Fuyao 直出)
# ============================================================

class ConceptIndexFeed:
    """概念板块指数快照 — 从 Fuyao 同花顺指数行情直接获取概念级实时涨跌

    替代原先"个股快照 → 按概念聚合算均涨"的方案。
    优势: 一次 API 调用获取所有概念的真实点位涨跌, 含成交量/额。

    刷新策略 (增量优化):
    - 首次: 全量 388 概念 (建立市场广度基线)
    - 后续: 仅刷新活跃概念 (stage ≥ 1, 通常 <50 个, 减少 80%+ API 调用)
    - 每 5 分钟: 全量回归一次 (保持涨跌比统计准确)
    """

    _FULL_REFRESH_INTERVAL = 300  # 全量刷新间隔 (秒, 5分钟)

    def __init__(self, refresh_interval: float = 120.0):
        self._refresh_interval = refresh_interval
        self._last_refresh: float = 0
        self._last_full_refresh: float = 0   # 上次全量刷新时间
        self._first_full_done: bool = False
        self._catalog: dict[str, str] = {}     # thscode → name
        self._snapshots: dict[str, dict] = {}  # thscode → snapshot
        self._name_to_code: dict[str, str] = {}  # name → thscode (模糊匹配用)
        self._active_thscodes: set = set()     # 活跃概念 thscode (外部注入)
        self._loaded = False

    def load_catalog(self):
        """加载概念板块目录 (启动时调用一次)"""
        if self._loaded:
            return
        try:
            from core.fuyao_client import get_fuyao_client
            client = get_fuyao_client()
            items = client.get_concept_catalog()
            if items:
                self._catalog = {it["thscode"]: it["name"] for it in items}
                self._name_to_code = {it["name"]: it["thscode"] for it in items}
                self._loaded = True
                logger.info(f"ConceptIndexFeed: 加载 {len(self._catalog)} 个概念板块目录")
        except Exception as e:
            logger.warning(f"ConceptIndexFeed 目录加载失败: {e}")

    def set_active_codes(self, thscodes: set):
        """设置当前活跃概念 (由 Scheduler 根据 ConsensusTracker stages 注入)"""
        self._active_thscodes = thscodes

    def refresh(self, force: bool = False) -> bool:
        """刷新概念指数行情 (增量优先, 每 5 分钟全量回归)

        策略:
        - 首次: 全量 388 (建立基线)
        - 后续: 仅活跃概念, 每 5 分钟全量一次
        - 活跃概念为空时: 退化为全量

        Returns: True 表示成功刷新
        """
        now = time.time()
        if not force and (now - self._last_refresh) < self._refresh_interval:
            return False

        if not self._catalog:
            self.load_catalog()
        if not self._catalog:
            return False

        # 确定本轮刷新范围
        need_full = (force or not self._first_full_done or
                     (now - self._last_full_refresh) >= self._FULL_REFRESH_INTERVAL)

        if need_full:
            thscodes = list(self._catalog.keys())
            mode = "全量"
        elif self._active_thscodes:
            thscodes = [c for c in self._active_thscodes if c in self._catalog]
            mode = f"增量({len(thscodes)}活跃)"
        else:
            thscodes = list(self._catalog.keys())
            mode = "全量(无活跃标记)"

        # 节流优化: 增量模式下如果无变化, 跳过
        if not force and not need_full and not thscodes:
            return False

        try:
            from core.fuyao_client import get_fuyao_client
            client = get_fuyao_client()
            snaps = client.get_concept_snapshots(thscodes)
            if snaps:
                # 增量模式: 合并而非替换 (保留未刷新的旧快照用于 get_stats)
                if not need_full and self._snapshots:
                    self._snapshots.update(snaps)
                else:
                    self._snapshots = snaps
                self._last_refresh = now
                if need_full:
                    self._last_full_refresh = now
                    self._first_full_done = True

                up_count = sum(1 for s in snaps.values()
                               if s.get("change_pct", 0) > 0)
                logger.debug(f"ConceptIndexFeed: {mode}刷新 "
                             f"{len(snaps)} 个概念行情, 上涨 {up_count}")
                return True
        except Exception as e:
            logger.warning(f"ConceptIndexFeed 刷新失败({mode}): {e}")
        return False

    # ── 查询接口 ──────────────────────────────────────────

    def get_concept_change(self, concept_name: str) -> float:
        """获取某个概念板块的实时涨跌幅 (%)
        
        先用精确匹配, 失败则用 name → thscode 反向查。
        """
        # 直接匹配 thscode (如果传的是 thscode)
        if concept_name in self._snapshots:
            return self._snapshots[concept_name].get("change_pct", 0)

        # 名称 → thscode
        code = self._name_to_code.get(concept_name)
        if code and code in self._snapshots:
            return self._snapshots[code].get("change_pct", 0)

        # 模糊匹配 (概念名可能在中文中有细微差异)
        for thscode, snap in self._snapshots.items():
            if snap.get("name", "") == concept_name:
                return snap.get("change_pct", 0)

        return 0

    def get_top_concepts(self, n: int = 15,
                         min_change: float = 0.5) -> list[dict]:
        """获取涨幅 Top N 概念板块"""
        ranked = []
        for thscode, snap in self._snapshots.items():
            pct = snap.get("change_pct", 0) or 0
            if pct >= min_change:
                ranked.append({
                    "thscode": thscode,
                    "name": snap.get("name", ""),
                    "change_pct": round(pct, 2),
                    "turnover": snap.get("turnover", 0),
                })
        ranked.sort(key=lambda x: -x["change_pct"])
        return ranked[:n]

    def get_top_volume(self, n: int = 10) -> list[dict]:
        """获取成交额 Top N 概念板块"""
        ranked = []
        for thscode, snap in self._snapshots.items():
            ranked.append({
                "thscode": thscode,
                "name": snap.get("name", ""),
                "change_pct": round(snap.get("change_pct", 0) or 0, 2),
                "turnover": snap.get("turnover", 0),
            })
        ranked.sort(key=lambda x: -(x["turnover"] or 0))
        return ranked[:n]

    def get_all_sorted(self) -> list[dict]:
        """获取全部概念板块 (按涨幅降序)"""
        ranked = []
        for thscode, snap in self._snapshots.items():
            ranked.append({
                "thscode": thscode,
                "name": snap.get("name", ""),
                "change_pct": round(snap.get("change_pct", 0) or 0, 2),
                "volume": snap.get("volume", 0),
                "turnover": snap.get("turnover", 0),
                "high": snap.get("high", 0),
                "low": snap.get("low", 0),
            })
        ranked.sort(key=lambda x: -x["change_pct"])
        return ranked

    def get_stats(self) -> dict:
        """概念市场统计: 涨跌比、均涨、领涨/领跌"""
        if not self._snapshots:
            return {"up": 0, "down": 0, "flat": 0, "avg_pct": 0,
                    "top_gainer": None, "top_loser": None}

        up = sum(1 for s in self._snapshots.values()
                 if (s.get("change_pct", 0) or 0) > 0.1)
        down = sum(1 for s in self._snapshots.values()
                   if (s.get("change_pct", 0) or 0) < -0.1)
        flat = len(self._snapshots) - up - down
        pcts = [s.get("change_pct", 0) or 0 for s in self._snapshots.values()]
        avg = sum(pcts) / len(pcts) if pcts else 0

        sorted_by_pct = sorted(self._snapshots.values(),
                               key=lambda x: -(x.get("change_pct", 0) or 0))
        top_gainer = {
            "name": sorted_by_pct[0].get("name", ""),
            "pct": round(sorted_by_pct[0].get("change_pct", 0) or 0, 2),
        } if sorted_by_pct else None
        top_loser = {
            "name": sorted_by_pct[-1].get("name", ""),
            "pct": round(sorted_by_pct[-1].get("change_pct", 0) or 0, 2),
        } if sorted_by_pct else None

        return {
            "total": len(self._snapshots),
            "up": up, "down": down, "flat": flat,
            "avg_pct": round(avg, 2),
            "top_gainer": top_gainer,
            "top_loser": top_loser,
        }

    @property
    def is_ready(self) -> bool:
        return self._loaded and bool(self._snapshots)

    @property
    def last_refresh(self) -> float:
        return self._last_refresh


# ============================================================
# 全局单例
# ============================================================

consensus_tracker: Optional[ConceptConsensusTracker] = None
diffusion: Optional[ConceptDiffusion] = None
rrg: Optional[ConceptRRG] = None
concept_index: Optional[ConceptIndexFeed] = None


def init_consensus_tracker(concept_map: dict[str, list[str]]):
    global consensus_tracker, diffusion, rrg, concept_index
    consensus_tracker = ConceptConsensusTracker(concept_map)
    consensus_tracker.load_full_market()
    diffusion = ConceptDiffusion(window=20)
    diffusion.set_full_stocks(consensus_tracker._full_stocks)
    rrg = ConceptRRG(rs_window=10)
    concept_index = ConceptIndexFeed()
    concept_index.load_catalog()
    logger.info(f"共识追踪器初始化: {len(concept_map)} 只债 → "
                f"{len(set(c for v in concept_map.values() for c in v))} 个概念 "
                f"(全市场: {len(consensus_tracker._full_stocks)})")
