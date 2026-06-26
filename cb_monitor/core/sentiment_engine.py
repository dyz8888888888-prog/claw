"""
SentimentEngine — 六维市场情绪引擎

参考《超短复盘必备情绪指标》, 整合 kaipanla-crawler + TDX 快照数据:
  1. 涨停强度   — 涨停数 / 跌停数 / 首板/连板分布
  2. 打板质量   — 炸板率 / 封板成功率
  3. 接力情绪   — 连板率 / 连板晋级趋势
  4. 亏钱效应   — 大幅回撤 / 天地板 / 昨日破板表现
  5. 赚钱效应   — 昨日涨停/连板今日表现 / 百日新高
  6. 市场广度   — 涨跌比 / 成交额

情绪阶段判定: 过热(overheat) / 活跃(active) / 温和(mild) / 退潮(ebb) / 冰点(freeze)

盘中/盘后混合策略:
  - 盘中 (09:30-15:00): TDX快照涨跌比 + 转债池内涨停数
  - 盘后 (15:30后): kaipanla-crawler 全量 (炸板/晋级/封单完整)
  - 下次开盘前自动加载盘后结果作为基准
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 情绪阶段 → 中文名 + CSS类名
PHASE_INFO = {
    'overheat': {'cn': '过热', 'color': '#ef4444', 'advice': '减仓防分歧', 'priority': 5},
    'active':   {'cn': '活跃', 'color': '#22c55e', 'advice': '积极操作',   'priority': 4},
    'mild':     {'cn': '温和', 'color': '#f59e0b', 'advice': '谨慎参与',   'priority': 3},
    'ebb':      {'cn': '退潮', 'color': '#3b82f6', 'advice': '控制仓位',   'priority': 2},
    'freeze':   {'cn': '冰点', 'color': '#64748b', 'advice': '空仓等待',   'priority': 1},
}

# 情绪阶段 → DecisionPipeline 5阶段 (唯一市场状态源)
# overheat→climax, active→ferment, mild→startup, ebb→retreat, freeze→freeze
SENTIMENT_TO_MARKET = {
    'overheat': 'climax',
    'active': 'ferment',
    'mild': 'startup',
    'ebb': 'retreat',
    'freeze': 'freeze',
}


class SentimentEngine:
    """六维市场情绪引擎"""

    def __init__(self, kpl_client=None):
        """kpl_client: KPLClient 实例 (可选, 盘后模式下必需)"""
        self._kpl = kpl_client

    def evaluate_from_kpl(self, date: str = None) -> dict:
        """盘后全量评估: 用 kaipanla-crawler 的完整数据

        Returns:
            {
                phase: 'active' | 'mild' | ...,
                phase_cn: '活跃' | ...,
                advice: '积极操作' | ...,
                indicators: { ... 六维指标 },
                signals: ['涨停家数增多', ...],
                timestamp: '2026-06-23 15:30'
            }
        """
        if self._kpl is None:
            return self._empty_result('需初始化 kpl_client')

        try:
            raw = self._kpl.get_sentiment_6d_raw(date)
        except Exception as e:
            logger.warning(f"KPL六维数据获取失败: {e}")
            return self._empty_result(f'数据获取失败: {e}')

        summary = raw.get('summary', {})
        ladder = raw.get('ladder', {})
        new_high = raw.get('new_high', 0)
        broken = raw.get('broken_limit', [])

        # === 六维指标计算 ===

        # 1. 涨停强度
        limit_up = summary.get('涨停数', 0)
        actual_limit_up = summary.get('实际涨停', 0)
        limit_dn = summary.get('跌停数', 0)
        first_board = ladder.get('一板', 0)
        second_board = ladder.get('二板', 0)
        third_board = ladder.get('三板', 0)
        height_board = ladder.get('高度板', 0)

        # 2. 打板质量
        broken_count = len(broken)
        blast_ratio = ladder.get('今日涨停破板率', 0) / 100  # 已经是百分比
        seal_success_rate = 1 - blast_ratio
        total_attempts = (limit_up + broken_count) or 1

        # 3. 接力情绪
        promotion_rate = ladder.get('连板率', 0)  # 已经是百分比值

        # 4. 亏钱效应
        sharp_withdrawal = summary.get('大幅回撤家数', 0)

        # 5. 赚钱效应
        yesterday_zt_perf = ladder.get('昨日涨停今表现', 0)
        yesterday_lb_perf = ladder.get('昨日连板今表现', 0)
        yesterday_break_perf = ladder.get('昨日破板今表现', 0)

        # 6. 市场广度
        up_count = summary.get('上涨家数', 0)
        down_count = summary.get('下跌家数', 0)
        up_down_ratio = up_count / max(down_count, 1)
        turnover = summary.get('成交额', 0) / 1e8  # 亿

        indicators = {
            # 1. 涨停强度
            'limit_up': limit_up,
            'actual_limit_up': actual_limit_up,
            'limit_down': limit_dn,
            'first_board': first_board,
            'second_board': second_board,
            'third_board': third_board,
            'height_board': height_board,

            # 2. 打板质量
            'broken_count': broken_count,
            'blast_ratio': round(blast_ratio * 100, 1),  # 转为百分比数值
            'seal_success_rate': round(seal_success_rate * 100, 1),
            'total_attempts': total_attempts,

            # 3. 接力情绪
            'promotion_rate': round(promotion_rate, 1),

            # 4. 亏钱效应
            'sharp_withdrawal': sharp_withdrawal,

            # 5. 赚钱效应
            'yesterday_zt_perf': round(yesterday_zt_perf, 2),
            'yesterday_lb_perf': round(yesterday_lb_perf, 2),
            'yesterday_break_perf': round(yesterday_break_perf, 2),
            'new_high': new_high,

            # 6. 市场广度
            'up_count': up_count,
            'down_count': down_count,
            'up_down_ratio': round(up_down_ratio, 2),
            'turnover_yi': round(turnover, 1),
        }

        # === 情绪阶段判定 ===
        phase, signals = self._classify(indicators)
        info = PHASE_INFO.get(phase, PHASE_INFO['freeze'])

        return {
            'phase': phase,
            'phase_cn': info['cn'],
            'advice': info['advice'],
            'indicators': indicators,
            'signals': signals,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }

    def evaluate_from_snapshot(self, snapshots: dict, market_state: dict = None) -> dict:
        """盘中轻量评估: 用 TDX 快照 + 转债池数据

        盘中缺少炸板/晋级数据, 仅用:
          - 转债池涨跌比
          - 转债池内正股涨停近似计数
          - TDX 市场状态 (如有)
        """
        if not snapshots:
            return self._empty_result('无快照数据')

        up = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) > 0)
        down = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) < 0)
        ratio = up / max(down, 1)

        # 正股涨停近似 (阈值按主板/创业板分开)
        stock_zt = 0
        for s in snapshots.values():
            pct = getattr(s, 'stock_change_pct', 0) or 0
            code = getattr(s, 'code', '')
            if code.startswith(('300', '688')):
                if pct >= 19.0:
                    stock_zt += 1
            else:
                if pct >= 9.5:
                    stock_zt += 1

        # 提取 TDX 市场状态额外信息
        limit_up_from_ms = 0
        broke_limit_from_ms = 0
        state_cn = ''
        if market_state:
            limit_up_from_ms = market_state.get('limit_up', 0)
            broke_limit_from_ms = market_state.get('broke_limit', 0)
            state_cn = market_state.get('state_cn', '')

        indicators = {
            'pool_up': up,
            'pool_down': down,
            'pool_ratio': round(ratio, 2),
            'pool_limit_up': stock_zt,  # 转债池正股涨停近似数
            'market_limit_up': limit_up_from_ms,
            'market_broke_limit': broke_limit_from_ms,
        }

        # 盘中简化判定: 只用池内涨跌比 + 正股涨停数
        if ratio > 2.0 and stock_zt >= 5:
            phase = 'active'
        elif ratio > 1.2 and stock_zt >= 3:
            phase = 'mild'
        elif ratio < 0.5:
            phase = 'freeze'
        elif ratio < 0.8:
            phase = 'ebb'
        else:
            phase = 'mild'

        # 如果 TDX 市场状态可用, 优先使用 (更准确)
        if state_cn:
            if state_cn == '高潮':
                phase = 'overheat'
            elif state_cn == '冰点':
                phase = 'freeze'
            elif state_cn == '退潮':
                phase = 'ebb'
            elif state_cn == '发酵':
                phase = 'active'

        info = PHASE_INFO.get(phase, PHASE_INFO['freeze'])
        return {
            'phase': phase,
            'phase_cn': info['cn'],
            'advice': info['advice'],
            'indicators': indicators,
            'signals': [],
            'timestamp': datetime.now().strftime('%H:%M'),
            'source': 'snapshot',  # 标记为盘中轻量源
        }

    def evaluate_intraday_full(self, snapshots: dict, market_state: dict = None,
                                fuyao_pool_items: list = None,
                                fuyao_ladder_data: dict = None) -> dict:
        """盘中全量评估: TDX快照 + Fuyao实时 + 市场状态

        用 Fuyao 盘中涨停/晋级数据替代 KPL 昨日数据,
        使情绪指标反映今日实时状况。

        Args:
            snapshots: TDX 转债快照
            market_state: 市场状态分类结果
            fuyao_pool_items: Fuyao 涨停池缓存 (由调度器维护, 避免API端点429)
            fuyao_ladder_data: Fuyao 连板天梯缓存
        """
        base = self.evaluate_from_snapshot(snapshots, market_state)

        # 从快照计算今日涨跌比 (转债池级别, 盘中实时)
        indicators = dict(base['indicators'])
        if snapshots:
            up = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) > 0)
            down = sum(1 for s in snapshots.values() if getattr(s, 'change_pct', 0) < 0)
            indicators['up_down_ratio'] = round(up / max(down, 1), 2)

        # 尝试获取 Fuyao 盘中数据 (优先缓存, 回退直调)
        try:
            items = fuyao_pool_items
            if items is None:
                from core.fuyao_client import get_fuyao_client
                fc = get_fuyao_client()
                items = fc.get_limit_up_pool_all()
            intraday_limit_up = len(items) if items else 0

            # 晋级率: 优先用缓存天梯计算, 回退直调
            promotion_rate = 0
            ladder_data = fuyao_ladder_data
            if ladder_data:
                # 从缓存的连板天梯计算晋级率
                try:
                    ladder_items = ladder_data.get('data', {}).get('item', [])
                    if len(ladder_items) >= 2:
                        yesterday = ladder_items[1]
                        boards = yesterday.get('boards', {})
                        total = sum(len(stocks) for board_name in
                                    ["two_board", "three_board", "four_board",
                                     "five_board", "six_board", "seven_over"]
                                    for stocks in [boards.get(board_name, [])])
                        sealed = sum(1 for board_name in
                                     ["two_board", "three_board", "four_board",
                                      "five_board", "six_board", "seven_over"]
                                     for stock in boards.get(board_name, [])
                                     if stock.get("seal_nextday") is True)
                        promotion_rate = round(sealed / max(total, 1) * 100, 1)
                except Exception:
                    pass
            if promotion_rate == 0:
                from core.fuyao_client import get_fuyao_client
                fc = get_fuyao_client()
                promotion_rate = fc.get_promotion_rate()

            indicators = dict(base['indicators'])
            if intraday_limit_up > 0:
                indicators['limit_up'] = intraday_limit_up
                indicators['intraday_limit_up'] = intraday_limit_up

                # 从 Fuyao 涨停池计算盘中连板分布 (替代 KPL 昨日数据)
                board_dist = {1: 0, 2: 0, 3: 0, 4: 0}
                for s in (items or []):
                    bd = s.get('continue_day_cnt', 0) or 1
                    if bd <= 4:
                        board_dist[bd] = board_dist.get(bd, 0) + 1
                    else:
                        board_dist[4] = board_dist.get(4, 0) + 1
                indicators['first_board'] = board_dist.get(1, 0)
                indicators['second_board'] = board_dist.get(2, 0)
                indicators['third_board'] = board_dist.get(3, 0)
                indicators['height_board'] = board_dist.get(4, 0)
            if promotion_rate > 0:
                indicators['promotion_rate'] = round(promotion_rate, 1)

            base['indicators'] = indicators
            base['source'] = 'fuyao+snapshot'

            # 用市场状态修正阶段
            state_cn = (market_state or {}).get('state_cn', '')
            if state_cn == '高潮':
                base['phase'] = 'overheat'
            elif state_cn == '冰点':
                base['phase'] = 'freeze'
            elif state_cn == '退潮':
                base['phase'] = 'ebb'
            elif state_cn == '发酵':
                base['phase'] = 'active'
            elif state_cn == '启动':
                base['phase'] = 'mild'
            info = PHASE_INFO.get(base['phase'], PHASE_INFO['mild'])
            base['phase_cn'] = info['cn']
            base['advice'] = info['advice']

        except Exception as e:
            logger.debug(f"Fuyao盘中数据获取失败, 回退快照: {e}")
            base['source'] = 'snapshot'

        return base

    def merge(self, intraday: dict, posthoc: dict) -> dict:
        """合并盘中+盘后结果: Fuyao今日实时优先, KPL昨日收盘补充

        - 盘中 (intraday): evaluate_intraday_full 结果 (Fuyao实时+快照)
        - 盘后 (posthoc): evaluate_from_kpl 结果 (KPL昨日收盘)
        - 策略: Fuyao可用时以盘中为基调, KPL仅补充昨日收盘维度
        """
        # 盘后数据不可用, 用盘中结果
        if not posthoc or (posthoc.get('phase') == 'freeze' and not posthoc.get('indicators', {}).get('limit_up')):
            if intraday:
                return intraday
            return self._empty_result()

        # 盘中数据有 Fuyao 实时来源 → 以盘中为基调
        is_live = intraday and intraday.get('source', '').startswith('fuyao')
        intra_indicators = intraday.get('indicators', {}) if intraday else {}
        post_indicators = dict(posthoc.get('indicators', {}))

        if is_live and intraday:
            # ── 以 Fuyao 盘中数据为基调 ──
            result = intraday.copy()
            merged_indicators = dict(intra_indicators)

            # KPL 补充昨日收盘维度 (仅在昨日收盘后可用)
            for key in ['blast_ratio', 'seal_success_rate', 'yesterday_zt_perf',
                         'yesterday_lb_perf', 'yesterday_break_perf',
                         'new_high', 'sharp_withdrawal', 'up_down_ratio',
                         'up_count', 'down_count', 'actual_limit_up',
                         'turnover_yi', 'limit_down', 'total_attempts',
                         'broken_count']:
                if key in post_indicators:
                    merged_indicators[key] = post_indicators[key]

            # 今日日期覆盖 (避免显示昨日 date)
            from datetime import date
            result['cached_date'] = date.today().isoformat()

            # 重新判定情绪阶段 (基于合并后的指标, 今日为主)
            phase_key, signals = self._classify(merged_indicators)
            pinfo = PHASE_INFO.get(phase_key, PHASE_INFO['freeze'])
            result['phase'] = phase_key
            result['phase_cn'] = pinfo['cn']
            result['advice'] = pinfo['advice']
            result['signals'] = signals
            result['source'] = 'fuyao+kpl'
            result['indicators'] = merged_indicators
            return result

        # 无 Fuyao 实时: KPL 昨天收盘数据为主, 盘中转债池补充
        result = posthoc.copy()
        for key in ['pool_up', 'pool_down', 'pool_ratio', 'pool_limit_up']:
            if key in intra_indicators:
                post_indicators[key] = intra_indicators[key]
        if intraday:
            result['pool_sentiment'] = intraday.get('phase_cn', '')
        result['indicators'] = post_indicators
        return result

    # ========== 私有方法 ==========

    def _classify(self, ind: dict) -> tuple[str, list[str]]:
        """六维交叉判定情绪阶段 + 生成信号文本

        判断规则 (优先级从高到低):
        """
        limit_up = ind.get('limit_up', 0) or 0
        blast_ratio = ind.get('blast_ratio', 100) or 100
        promotion_rate = ind.get('promotion_rate', 0) or 0
        up_down_ratio = ind.get('up_down_ratio', 0) or 0
        new_high = ind.get('new_high', 0) or 0
        sharp = ind.get('sharp_withdrawal', 0) or 0
        zt_perf = ind.get('yesterday_zt_perf', 0) or 0
        lb_perf = ind.get('yesterday_lb_perf', 0) or 0

        signals = []

        # ── 过热 ──
        if limit_up >= 100 and blast_ratio < 20 and promotion_rate > 60:
            signals.append('涨停家数突破100')
            if blast_ratio < 10:
                signals.append('炸板率极低, 情绪极度亢奋')
            return 'overheat', signals

        if limit_up >= 120 and up_down_ratio > 3.0:
            return 'overheat', ['涨停家数≥120且涨跌比>3', '注意过热风险']

        # ── 活跃 ──
        if 50 <= limit_up < 100:
            if blast_ratio < 30 and promotion_rate > 40:
                signals.append('涨停家数充裕, 炸板率健康')
                return 'active', signals
            if zt_perf > 0 and lb_perf > 0:
                signals.append('昨日涨停赚钱效应良好')
                return 'active', signals
            # 涨停50+但晋级率低 → 温和
            return 'mild', ['涨停数中等但接力偏弱']

        # ── 冰点 ──
        if limit_up < 15:
            signals.append('涨停家数<15, 市场极度悲观')
            return 'freeze', signals
        if blast_ratio > 50 and promotion_rate < 15:
            return 'freeze', ['炸板率>50%且晋级率<15%', '全面退潮信号']

        # ── 退潮 ──
        if limit_up <= 30 or blast_ratio > 40:
            signals.append(f'涨停{limit_up}只, 炸板率{blast_ratio}%')
            if sharp > 10:
                signals.append(f'大幅回撤{sharp}家, 亏钱效应显著')
            if up_down_ratio < 0.5:
                signals.append('涨跌比<0.5, 空方主导')
            return 'ebb', signals
        if promotion_rate < 20 and blast_ratio > 35:
            return 'ebb', ['晋级率<20%且炸板率>35%']
        if zt_perf < -2 and lb_perf < -2:
            signals.append('昨日连板均亏, 资金明显撤离')
            return 'ebb', signals

        # ── 温和 (默认) ──
        if 30 <= limit_up < 50:
            if blast_ratio < 40:
                signals.append('涨停数低位但炸板率可控')
                return 'mild', signals
            return 'ebb', signals

        if 15 <= limit_up < 30:
            if blast_ratio < 35 and zt_perf > 0:
                return 'mild', ['涨停稀少但有赚钱效应, 可观察']
            return 'ebb', signals

        return 'mild', signals

    @staticmethod
    def _empty_result(msg: str = '暂无数据') -> dict:
        return {
            'phase': '',
            'phase_cn': '休眠',
            'advice': msg,
            'indicators': {},
            'signals': [],
            'timestamp': datetime.now().strftime('%H:%M'),
            'source': 'empty',
        }
