"""
市场状态识别器 — MarketStateClassifier

每5分钟更新一次, 业界5阶段情绪周期:
  冰点  — 涨停<60, 晋级率<20%, 炸板率>35%
  启动  — 涨停<60, 晋级率>30%, 炸板率<25%
  发酵  — 60≤涨停<120, 晋级率>40%, 炸板率<20%
  高潮  — 涨停≥120, 晋级率>60%, 炸板率<10%
  退潮  — 涨停≤60, 晋级率<25%, 炸板率>40%

输入:
  - 涨停家数: Fuyao limit-up-pool (主) → akshare (备) → pywencai (兜底)
  - 炸板数:   从 Fuyao 涨停池筛选封板失败的 (暂用 akshare 备)
  - 晋级率:   Fuyao limit-up-ladder → seal_nextday 自动算
  - 涨跌方向: TDX 三大指数
  - 成交量:   上证成交额同比
"""

import time
import json
import logging
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

STATE_NAMES = {
    "freeze": "冰点",
    "startup": "启动",
    "ferment": "发酵",
    "climax": "高潮",
    "retreat": "退潮",
}

# 三大指数
INDICES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}

# 昨日涨停缓存 (用于计算晋级率)
_YEST_LIMIT_UP_TICKERS: set = set()


@dataclass
class MarketSnapshot:
    """单次全市场快照"""
    ts: float
    limit_up: int           # 全市场涨停数
    broke_limit: int        # 全市场炸板数
    promotion_rate: float = 0   # 连板晋级率 (%)
    index_up: int = 0
    index_dn: int = 0
    index_avg_pct: float = 0
    index_vol: float = 0
    vol_ratio: float = 1.0


class MarketStateClassifier:
    """市场状态分类器 (业界5阶段情绪周期)"""

    def __init__(self, history_size: int = 20):
        self._history: deque[MarketSnapshot] = deque(maxlen=history_size)
        self._current_state: str = "ferment"  # 默认发酵(中性)
        self._state_since: float = time.time()
        self._last_update: float = 0
        self._update_interval: float = 300

        self._state_votes: dict[str, int] = {}
        self._vote_threshold: int = 2

        self._yest_vol: float = 0
        self._first_snapshot: bool = True
        # 昨日涨停股ticker列表 (用于晋级率)
        self._yest_limit_tickers: set = set()

    @property
    def state(self) -> str:
        return self._current_state

    @property
    def state_cn(self) -> str:
        return STATE_NAMES.get(self._current_state, "震荡")

    def should_update(self) -> bool:
        return time.time() - self._last_update >= self._update_interval

    def classify(self) -> MarketSnapshot:
        """从全市场数据分类 (每5分钟, 问财涨停池 + TDX指数 + 晋级率)"""
        snapshot = self._build_full_market_snapshot()
        self._history.append(snapshot)
        self._last_update = time.time()

        if self._first_snapshot and snapshot.index_vol > 0:
            self._yest_vol = snapshot.index_vol
            self._first_snapshot = False
            logger.info(f"全市场基准: 上证成交{snapshot.index_vol:.0f}亿")

        state = self._determine_state(snapshot)
        self._apply_state(state)
        return snapshot

    def _build_full_market_snapshot(self) -> MarketSnapshot:
        """全市场快照: 问财涨停池 + TDX指数 + 晋级率"""
        # 1. 涨停/炸板
        limit_up, broke_limit = self._fetch_limit_up_pools()

        # 2. 三大指数
        index_avg_pct, index_up, index_dn, index_vol = self._fetch_indices()

        # 3. 成交量同比
        vol_ratio = 1.0
        if self._yest_vol > 0 and index_vol > 0:
            vol_ratio = round(index_vol / self._yest_vol, 2)

        # 4. 连板晋级率
        promotion_rate = self._fetch_promotion_rate()

        return MarketSnapshot(
            ts=time.time(),
            limit_up=limit_up,
            broke_limit=broke_limit,
            promotion_rate=round(promotion_rate, 1),
            index_up=index_up,
            index_dn=index_dn,
            index_avg_pct=round(index_avg_pct, 2),
            index_vol=round(index_vol, 1),
            vol_ratio=vol_ratio,
        )

    def _fetch_limit_up_pools(self) -> tuple[int, int]:
        """获取全市场涨停/炸板数: Fuyao(主) → KPL(备参) → akshare(备) → pywencai(兜底)
        
        Fuyao 盘中数据可能偏低, 当 < 60 时自动用 KPL 昨日全量数据补充,
        避免误判为冰点/退潮。
        """
        limit_up, broke_limit = self._try_fuyao_pool()
        use_intraday = limit_up >= 60  # 盘中涨停≥60才用实时数据

        # KPL 备参: 拿昨日全量数据做下限
        kpl_limit, kpl_broke = 0, 0
        try:
            from core.kpl_client import KPLClient
            kpl = KPLClient()
            summary = kpl.get_daily_summary()
            if summary:
                kpl_limit = summary.get('涨停数', 0)
                kpl_broke = summary.get('炸板数', 0)
        except Exception:
            pass

        if use_intraday:
            # 用盘中数据, KPL做补充
            if broke_limit == 0 and kpl_broke > 0:
                broke_limit = kpl_broke
            return limit_up, broke_limit

        # 盘中涨停<60 → 用KPL数据兜底
        if kpl_limit > 0:
            # 计算炸板数: 实际涨停 vs 总涨停
            actual = summary.get('实际涨停', 0)
            kpl_broke = max(0, kpl_limit - actual) if actual > 0 else kpl_broke
            logger.info(f"盘中涨停仅{limit_up}, 用KPL昨日数据兜底: 涨停{kpl_limit} 炸板{kpl_broke}")
            return kpl_limit, kpl_broke

        # KPL也没有 → 依次降级
        limit_up, broke_limit = self._try_akshare()
        if limit_up > 0:
            return limit_up, broke_limit
        return self._try_wencai()

    def _try_fuyao_pool(self) -> tuple[int, int]:
        """Fuyao limit-up-pool: 全量涨停股 (含封板信息)"""
        try:
            from core.fuyao_client import get_fuyao_client
            client = get_fuyao_client()
            items = client.get_limit_up_pool_all()
            if not items:
                return 0, 0
            limit_up = len(items)
            # Fuyao 不直接给炸板数, 先用0, 由 akshare 备源补
            logger.debug(f"Fuyao: 涨停{limit_up}")
            return limit_up, 0
        except Exception as e:
            logger.warning(f"Fuyao涨停池获取失败: {e}")
            return 0, 0

    def _try_akshare(self) -> tuple[int, int]:
        """akshare: 涨停池 + 炸板池 (备源)"""
        limit_up = 0
        broke_limit = 0
        try:
            import akshare as ak
            df = ak.stock_zt_pool_dtgc_em(date=time.strftime("%Y%m%d"))
            if df is not None and not df.empty:
                broke_limit = len(df)
            # 如果 fuyao 失败, 才用 akshare 取涨停数
            df2 = ak.stock_zt_pool_em(date=time.strftime("%Y%m%d"))
            if df2 is not None and not df2.empty:
                limit_up = max(limit_up, len(df2))
            logger.debug(f"akshare: 涨停{limit_up} 炸板{broke_limit}")
        except Exception as e:
            logger.warning(f"akshare涨停池获取失败: {e}")
        return limit_up, broke_limit

    def _try_wencai(self) -> tuple[int, int]:
        """问财: 今日涨停股 + 今日炸板股 (兜底)"""
        limit_up = 0
        broke_limit = 0
        try:
            import pywencai
            df = pywencai.get(query='今日涨停股', loop=True)
            if df is not None and not df.empty:
                limit_up = len(df)
            df2 = pywencai.get(query='今日炸板股', loop=True)
            if df2 is not None and not df2.empty:
                broke_limit = len(df2)
            logger.debug(f"问财: 涨停{limit_up} 炸板{broke_limit}")
        except Exception as e:
            logger.warning(f"问财涨停查询失败: {e}")
        return limit_up, broke_limit

    def _fetch_indices(self) -> tuple[float, int, int, float]:
        """TDX: 三大指数涨跌"""
        from core.data_fusion import TdxClient
        avg_pct = 0.0
        up = 0
        dn = 0
        vol = 0.0

        try:
            client = TdxClient.get()
            codes = list(INDICES.keys())
            df = client.quotes(codes)
            if df is not None and not df.empty:
                pcts = []
                for _, row in df.iterrows():
                    code = str(row.get('code', ''))
                    if code not in codes:
                        continue
                    price = float(row.get('price', 0) or 0)
                    close = float(row.get('last_close', 0) or 0)
                    if close > 0:
                        pct = (price - close) / close * 100
                        pcts.append(pct)
                        if pct > 0:
                            up += 1
                        elif pct < 0:
                            dn += 1
                    # 上证成交额
                    if code == '000001':
                        vol = float(row.get('amount', 0) or 0) / 100000000  # 元→亿

                if pcts:
                    avg_pct = sum(pcts) / len(pcts)

            logger.debug(f"三大指数: 均涨{avg_pct:.2f}% {up}涨{dn}跌 上证{vol:.0f}亿")
        except Exception as e:
            logger.warning(f"TDX指数查询失败: {e}")

        return avg_pct, up, dn, vol

    def _determine_state(self, snap: MarketSnapshot) -> str:
        """业界5阶段情绪周期判定 — 5维交叉验证

        维度:
          A. 涨停数 limit_up         — 市场热度基准
          B. 晋级率 promotion_rate   — 赚钱效应持续性
          C. 炸板率 break_rate       — 资金分歧度
          D. 指数方向 index_up/dn    — 大盘配合度
          E. 量比 vol_ratio          — 资金参与度

        原则: 维度A/B主导, C/D/E做质量校验。
              涨停再多, 指数全跌 → 降级; 涨停不多, 指数全涨 → 升级。
        """
        total_limit = snap.limit_up
        break_rate = snap.broke_limit / max(total_limit, 1)
        promo = snap.promotion_rate

        idx_up = snap.index_up
        idx_dn = snap.index_dn
        idx_pct = snap.index_avg_pct
        vol_ratio = snap.vol_ratio

        all_up = idx_up >= 3 and idx_dn == 0
        all_dn = idx_dn >= 3 and idx_up == 0
        mostly_up = idx_up >= 2
        mostly_dn = idx_dn >= 2
        shrinking = vol_ratio > 0 and vol_ratio < 0.70     # 缩量<70%
        expanding = vol_ratio > 1.10                        # 放量>110%

        # ── 冰点: 市场极度悲观 ──
        if all_dn and total_limit < 60 and promo < 25:
            return "freeze"
        if idx_pct < -1.5 and total_limit < 60:
            return "freeze"
        if total_limit < 60 and (promo < 20 or break_rate > 0.35):
            return "freeze"
        if total_limit < 30:
            return "freeze"

        # ── 退潮: 亏钱效应扩散 ──
        if mostly_dn and shrinking:
            return "retreat"
        if mostly_dn and promo < 25 and break_rate > 0.30:
            return "retreat"
        if promo < 20 and total_limit < 80:
            return "retreat"

        # ── 高潮: 全面亢奋 ──
        if all_up and total_limit >= 100 and promo > 50:
            return "climax"
        if total_limit >= 150 and all_up and expanding:
            return "climax"
        if total_limit >= 200 and mostly_up:
            return "climax"  # 极端涨停数, 指数配合即可

        # 涨停≥120 但指数不配合 → 降级
        if total_limit >= 120 and mostly_dn:
            return "retreat"  # 指数全跌+高炸板 = 借涨停出货

        # ── 发酵: 赚钱效应扩散 ──
        if 60 <= total_limit < 120:
            if promo > 40 or break_rate < 0.20:
                return "ferment"
        if total_limit >= 120:
            # 涨停够多但指数不配合 → 发酵(非高潮)
            return "ferment"

        # ── 启动: 情绪触底回升 ──
        if total_limit < 60 and promo > 30 and break_rate < 0.25:
            return "startup"

        # ── 兜底: 按涨停数 + 指数方向模糊归类 ──
        if total_limit >= 120:
            return "climax" if all_up else "ferment"
        if total_limit >= 60:
            return "ferment" if mostly_up else "ferment"
        if total_limit < 30:
            return "freeze"
        if all_dn and total_limit < 50:
            return "freeze"
        return "ferment"

    def _fetch_promotion_rate(self) -> float:
        """计算连板晋级率 (Fuyao → KPL备源)

        逻辑: 取连板天梯中昨日的所有板位数据,
        统计 seal_nextday==True 的比例即为晋级率。
        """
        try:
            from core.fuyao_client import get_fuyao_client
            rate = get_fuyao_client().get_promotion_rate()
            if rate > 0:
                return rate
        except Exception as e:
            logger.warning(f"Fuyao晋级率获取失败: {e}")

        # KPL 备源: 从 ladder_stats 取连板率
        try:
            from core.kpl_client import KPLClient
            kpl = KPLClient()
            ladder = kpl.get_limit_up_ladder_stats()
            if ladder and ladder.get('连板率', 0) > 0:
                return float(ladder['连板率'])
        except Exception as e:
            logger.warning(f"KPL晋级率备源失败: {e}")

        return 0

    def _apply_state(self, new_state: str):
        """防抖切换"""
        if new_state == self._current_state:
            self._state_votes.clear()
            return

        self._state_votes[new_state] = self._state_votes.get(new_state, 0) + 1
        if self._state_votes[new_state] >= self._vote_threshold:
            old = self._current_state
            self._current_state = new_state
            self._state_since = time.time()
            self._state_votes.clear()
            latest = self._history[-1]
            logger.info(
                f"市场状态切换: {STATE_NAMES.get(old,'')} → {STATE_NAMES[new_state]} "
                f"(涨停{latest.limit_up} 炸板{latest.broke_limit} "
                f"指数{latest.index_up}涨{latest.index_dn}跌 +{latest.index_avg_pct:.1f}% "
                f"量比{latest.vol_ratio})"
            )

    def get_state_info(self) -> dict:
        latest = self._history[-1] if self._history else None
        return {
            "state": self._current_state,
            "state_cn": self.state_cn,
            "since": self._state_since,
            "limit_up": latest.limit_up if latest else 0,
            "broke_limit": latest.broke_limit if latest else 0,
            "promotion_rate": latest.promotion_rate if latest else 0,
            "index_up": latest.index_up if latest else 0,
            "index_dn": latest.index_dn if latest else 0,
            "index_pct": latest.index_avg_pct if latest else 0,
            "vol_ratio": latest.vol_ratio if latest else 1.0,
        }

    def get_signal_weights(self) -> dict:
        s = self._current_state
        return {
            "diffusion_weight": {"climax": 1.0, "ferment": 0.8, "startup": 0.5, "retreat": 0.3, "freeze": 0.1}[s],
            "dip_weight": {"climax": 0.3, "ferment": 0.6, "startup": 0.8, "retreat": 1.2, "freeze": 1.5}[s],
            "chase_weight": {"climax": 1.0, "ferment": 0.8, "startup": 0.5, "retreat": 0.2, "freeze": 0.0}[s],
        }
