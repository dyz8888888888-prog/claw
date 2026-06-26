"""
调度器 - Scheduler

主循环:
- 交易时段 (9:30-11:30, 13:00-15:00): 每3秒轮询
- 非交易时段: 休眠，每15分钟检查一次
"""

import time
import logging
from datetime import datetime
from typing import Optional

from config import CONFIG
from core.bond_selector import BondSelector
from core.data_fusion import DataFusion
from core.signal_engine import SignalEngine, AlertManager, Signal
import core.consensus_tracker as consensus_mod
from core.decision_pipeline import DecisionPipeline
from core.trading_calendar import trading_calendar
from core.market_state import MarketStateClassifier
from core.signal_accuracy import SignalAccuracyTracker
from backtest.tracker import tracker as backtest_tracker
from scheduler.rolling_window import RollingWindow
from output.formatter import OutputFormatter
from output.colors import RESET, CYAN, DIM, GRAY, GREEN
from output.signal_logger import SignalLogger
from output.notifier import Notifier
from output.daily_stats import DailyStats
from dashboard.shared_state import state

logger = logging.getLogger(__name__)


class Scheduler:
    """主调度器"""

    def __init__(self):
        self.config = CONFIG
        self._stop = False
        self._paused = False

        # 初始化模块
        self.selector = BondSelector(self.config)
        self.window = RollingWindow(max_window=100)
        self.signal_engine = SignalEngine(self.config)
        self.alert_manager = AlertManager(self.config)

        # 数据融合 (初始空，选债后创建)
        self.fusion: Optional[DataFusion] = None
        self._last_fuyao_refresh: float = 0

        # 决策管道 (注入到Scheduler, 非全局单例)
        self.pipeline = DecisionPipeline()

        # 市场状态分类器 (SentimentEngine优先, MarketStateClassifier备源)
        self.market_state = MarketStateClassifier()
        self._sentiment_engine = None  # 惰性初始化

        # 统计
        self.total_bonds = 0
        self.monitored_count = 0

        # 日志/推送/统计
        self.signal_logger = SignalLogger(self.config)
        self.notifier = Notifier(self.config)
        self.daily_stats = DailyStats(self.config)

        # 信号准确率追踪 (近5日回测胜率, 用于动态等级调整)
        self.signal_accuracy = SignalAccuracyTracker()
        self.signal_accuracy.load_recent(days=5)

        # 新架构旁路运行器 (与旧系统并行, 不影响旧链路)
        self._sidecar = None  # 惰性初始化
        # 选债池每日刷新标记 (开盘前刷新一次, 交易日有效)
        self._last_cov_date: str = ""

    def _refresh_bond_pool(self):
        """刷新选债池 (初始化/每30分钟)"""
        monitor_list = self.selector.get_monitor_list()
        self.total_bonds = self.selector.get_total_active()
        self.monitored_count = len(monitor_list)

        if self.fusion is None:
            self.fusion = DataFusion(monitor_list)
        else:
            self.fusion.set_monitor_list(monitor_list)

        logger.info(f"选债池刷新: 总池{self.total_bonds}只, 监控{self.monitored_count}只")

        # 更新强赎映射到共享状态 (供决策管道禁入判定)
        redeem_map = {}
        for item in monitor_list:
            status = item.get('redeem_status', '')
            if status in ('已公告强赎', '公告要强赎'):
                redeem_map[item.get('code_num', '')] = status
        state.redeem_map = redeem_map

        # 初始化共识追踪器 (使用 signal_engine 的概念映射)
        if not consensus_mod.consensus_tracker and hasattr(self.signal_engine, '_concept_map'):
            consensus_mod.init_consensus_tracker(self.signal_engine._concept_map)

        return monitor_list

    def _is_trading_hours(self) -> bool:
        """判断是否在交易时段 (交易所日历)"""
        return trading_calendar.is_trading_hours()

    def _seconds_until_next_trading(self) -> float:
        """计算距离下一个交易时段开始的秒数

        当前不在交易时段时调用, 精确计算到下次开盘的等待时间:
        - 盘前 (0:00-9:30):  等到 9:30
        - 午休 (11:30-13:00): 等到 13:00
        - 盘后 (15:00-24:00): 等到次日 9:30
        - 非交易日: 等到下个交易日 9:30
        上限 900s, 避免跨日计算误差导致无限等待
        """
        now = datetime.now()
        hhmm = now.hour * 100 + now.minute

        if not trading_calendar.is_trading_day():
            return 900.0

        if hhmm < 930:
            # 盘前 → 9:30 开盘
            target = now.replace(hour=9, minute=30, second=0, microsecond=0)
            return min(900.0, (target - now).total_seconds())
        elif hhmm < 1130:
            return 3.0  # 交易中, 理论不会到这
        elif hhmm < 1300:
            # 午休 → 13:00 开盘
            target = now.replace(hour=13, minute=0, second=0, microsecond=0)
            return (target - now).total_seconds()
        elif hhmm < 1500:
            return 3.0  # 交易中
        else:
            # 盘后 → 次日 9:30
            return 900.0

    def _apply_accuracy_adjustment(self, signals: list[Signal]) -> list[Signal]:
        """根据近5日回测胜率动态调整信号等级 + 抑制 + 权重
        
        三步:
        1. 硬抑制: 胜率<25% → 直接删除
        2. 降级: 胜率<35% → 降为D级仅预警
        3. 动态权重: 胜率/50% 调整 score
        """
        if not signals:
            return signals

        result = []
        suppressed_count = 0
        for sig in signals:
            # 1. 硬抑制检查
            if self.signal_accuracy.is_suppressed(sig.signal_type):
                suppressed_count += 1
                continue  # 直接丢弃

            # 2. 降为预警
            if self.signal_accuracy.is_warning_only(sig.signal_type):
                if sig.level not in ('D',):
                    sig.level = 'D'
                    sig.description += " [AI预警]"

            # 3. 动态权重
            weight = self.signal_accuracy.get_dynamic_weight(sig.signal_type)
            if abs(weight - 1.0) > 0.01:
                sig.score = round(sig.score * weight, 1)

            result.append(sig)

        if suppressed_count:
            logger.info(f"信号抑制: 过滤 {suppressed_count} 个低胜率信号 "
                        f"({self.signal_accuracy.get_suppressed_types()})")

        # 保留原有的升级/降级逻辑 (升1级 + 分数调整)
        for sig in result:
            adj_level = self.signal_accuracy.get_level_adjustment(sig.signal_type, sig.level)
            if adj_level == 'X':
                continue  # 已被抑制, 不应到这里
            if adj_level != sig.level:
                old_level = sig.level
                sig.level = adj_level
                if adj_level > old_level:  # 升级
                    sig.score = round(sig.score * 1.2, 1)
                    sig.description += f" [AI升{adj_level}]"
                elif adj_level == 'D':  # 降为预警
                    sig.score = round(sig.score * 0.5, 1)
                    sig.description += f" [AI降D]"

        return result

    def _run_cycle(self):
        """单轮监控循环 (含耗时监控 + 决策管道)"""
        t0 = time.time()

        if self.fusion is None:
            return

        monitor_list = self.fusion.monitor_list

        # 1. 获取融合数据
        snapshots = self.fusion.merge()
        if not snapshots:
            logger.warning("本轮无数据")
            return

        # 2. 存入滚动窗口
        for code, snap in snapshots.items():
            self.window.push(code, snap)

        # 2.5 市场状态分类: SentimentEngine优先 → MarketStateClassifier备源
        try:
            # 惰性初始化情绪引擎
            if self._sentiment_engine is None:
                from core.sentiment_engine import SentimentEngine
                self._sentiment_engine = SentimentEngine()
            from core.sentiment_engine import SENTIMENT_TO_MARKET
            with state._lock:
                snaps_for_sentiment = dict(state.snapshots)
            sent = self._sentiment_engine.evaluate_intraday_full(snaps_for_sentiment)
            sent_phase = sent.get('phase', 'active')
            sent_market = SENTIMENT_TO_MARKET.get(sent_phase, 'ferment')
            # 写入共享状态 (Flask API 也读这里)
            state.market_state = {
                'state': sent_market,
                'state_cn': sent.get('phase_cn', '活跃'),
                'source': 'sentiment_engine',
            }
            # 信号权重 (映射到原 MarketState 5阶段)
            weights = {
                "diffusion_weight": {"climax": 1.0, "ferment": 0.8, "startup": 0.5, "retreat": 0.3, "freeze": 0.1}.get(sent_market, 0.5),
                "dip_weight": {"climax": 0.3, "ferment": 0.6, "startup": 0.8, "retreat": 1.2, "freeze": 1.5}.get(sent_market, 0.8),
                "chase_weight": {"climax": 1.0, "ferment": 0.8, "startup": 0.5, "retreat": 0.2, "freeze": 0.0}.get(sent_market, 0.5),
            }
            self.signal_engine.set_market_weights(weights)
            logger.debug(f"情绪引擎: {sent.get('phase_cn')}→{sent_market} "
                         f"涨停{sent.get('indicators',{}).get('limit_up',0)}")
        except Exception as e:
            logger.warning(f"情绪引擎失败, 回退MarketStateClassifier: {e}")
            if self.market_state.should_update():
                try:
                    ms = self.market_state.classify()
                    state.market_state = {
                        'state': self.market_state.state,
                        'state_cn': self.market_state.state_cn,
                        'source': 'market_state_classifier',
                    }
                    logger.info(f"MarketState回退: {self.market_state.state_cn} "
                                f"涨停{ms.limit_up} 指数+{ms.index_avg_pct:.1f}%")
                except Exception as e2:
                    logger.warning(f"市场状态分类失败: {e2}")

        # 3. 计算信号
        # 注入主线概念到 SignalEngine (用于置信度升级)
        if consensus_mod.consensus_tracker:
            all_states = consensus_mod.consensus_tracker.get_all()
            mainline_set = {c for c, s in all_states.items() if s.get('stage', 0) >= 2}
            self.signal_engine.set_mainline_concepts(mainline_set)

        raw_signals = self.signal_engine.analyze(snapshots, self.window, monitor_list)

        # 3.4 动态等级调整: 根据近5日回测胜率升降信号等级
        raw_signals = self._apply_accuracy_adjustment(raw_signals)

        signals = self.alert_manager.process(raw_signals)

        # 3.5 日志记录 + 盘中统计 + 推送通知
        self.signal_logger.write_batch(signals, snapshots)
        self.daily_stats.record_batch(signals)
        self.notifier.send(signals, snapshots)

        # 3.6 决策管道: 对有信号的债做埋/卖/不做判定
        if signals and consensus_mod.consensus_tracker:
            self._run_decision_pipeline(signals, snapshots)

        # 4. 输出
        now_str = time.strftime('%H:%M:%S')
        fetch_cost = self.fusion.last_fetch_cost if hasattr(self.fusion, 'last_fetch_cost') else 0
        cycle_cost = time.time() - t0

        OutputFormatter.render_frame(
            now_str=now_str,
            total_bonds=self.total_bonds,
            monitored=self.monitored_count,
            snapshots=snapshots,
            signals=signals,
            window=self.window,
            fetch_cost=fetch_cost,
            monitor_list=monitor_list,
        )

        # 5. 统计摘要
        stats_summary = self.daily_stats.summary
        if stats_summary:
            print(f"  {DIM}{stats_summary}{RESET}")

        # 耗时告警
        if cycle_cost > 2.5:
            logger.warning(f"主循环慢: {cycle_cost:.2f}s (阈值2.5s)")

        # 6. 更新仪表盘共享状态
        self._update_dashboard_state(now_str, fetch_cost, snapshots, signals, monitor_list, cycle_cost)

    def _run_decision_pipeline(self, signals, snapshots):
        """对有信号的债执行4阶段决策管道"""
        try:
            # 设置主线概念
            all_states = consensus_mod.consensus_tracker.get_all()
            consensus_stages = {c: s.get('stage', 0) for c, s in all_states.items()}
            concept_heat = {c: s.get('dragon_sc_pct', 0) for c, s in all_states.items()}
            self.pipeline.set_mainlines(consensus_stages, concept_heat)

            # 概念映射
            concept_map = getattr(self.signal_engine, '_concept_map', {}) or {}

            # 只对有信号的债做判定 (节省计算)
            sig_codes = {s.code for s in signals}
            sig_snaps = {c: snapshots[c] for c in sig_codes if c in snapshots}
            if not sig_snaps:
                return

            redeem_map = getattr(state, 'redeem_map', {}) or {}
            ms = (state.market_state or {}).get('state', 'ferment')
            decisions = self.pipeline.evaluate_batch(sig_snaps, concept_map, consensus_stages, redeem_map,
                                                     market_state=ms)

            # 统计并输出埋伏信号
            ambush = [d for d in decisions if d.action == '埋伏']
            if ambush:
                for d in ambush[:3]:
                    print(f"  {GREEN}[决策] 埋伏 {d.name}({d.code}) {d.reason} "
                          f"止损{d.stop_loss_pct:.0f}% 止盈{d.take_profit_pct:.0f}%{RESET}")
                logger.info(f"决策管道: {len(ambush)}只埋伏 / {len(decisions)}只判定")
        except Exception as e:
            logger.error(f"决策管道异常: {e}")

    def _update_dashboard_state(self, now_str, fetch_cost, snapshots, signals, monitor_list, cycle_cost=0):
        """更新仪表盘共享状态"""
        # 涨跌统计
        surge_count = sum(1 for s in snapshots.values() if s.change_pct is not None and s.change_pct > 2)
        drop_count = sum(1 for s in snapshots.values() if s.change_pct is not None and s.change_pct < -2)
        limit_up_count = sum(1 for s in snapshots.values()
                             if s.stock_change_pct is not None and s.stock_change_pct >= 9.5)

        # 更新强赎预警
        state.clear_redeem_warnings()
        for item in (monitor_list or []):
            status = item.get('redeem_status', '')
            if status in ('已公告强赎', '公告要强赎'):
                state.add_redeem_warning(
                    item.get('code_num', ''),
                    item.get('name', ''),
                    status
                )

        # 信号历史 + 触发价格跟踪 + 后验统计
        for sig in signals:
            state.add_signal_history(sig)
            snap = snapshots.get(sig.code)
            trigger_price = snap.trade if snap and snap.trade > 0 else 0
            if trigger_price > 0:
                state.track_signal(sig, trigger_price)

            # 后验统计注册: 用 ask1 做实盘买入价 (纸面收益)
            # 流动性过滤: 成交额 < 1亿 的不跟踪
            if snap and getattr(snap, 'amount', 0) < 100_000_000:
                continue
            strategy = Signal._infer_strategy_type(sig.signal_type)
            ask_price = snap.ask1 if snap and getattr(snap, 'ask1', 0) > 0 else 0
            backtest_tracker.on_signal(sig, trigger_price, strategy=strategy,
                                       ask_price=ask_price)

        # 今日统计
        stats = self.daily_stats
        count_by_level = {}
        for lv in ['S', 'A', 'B', 'C', 'D']:
            cnt = stats._count_by_level.get(lv, 0)
            if cnt:
                count_by_level[lv] = cnt

        state.update_cycle(
            snapshots=snapshots,
            signals=signals,
            total_bonds=self.total_bonds,
            monitored=self.monitored_count,
            fetch_cost=fetch_cost,
            cycle_cost=cycle_cost,
            market_state=state.market_state,
            last_update=now_str,
            is_trading=self._is_trading_hours(),
            surge_count=surge_count,
            drop_count=drop_count,
            limit_up_count=limit_up_count,
            count_by_level=count_by_level,
            today_stats=stats.summary or '',
        )

        # 更新 pending 信号的峰值和盈亏
        state.update_pending(snapshots)

        # 后验统计 tick: 更新所有活跃信号的追踪数据
        backtest_tracker.tick(snapshots)

        # ── 新架构旁路: 用新管道并行评估 (不干扰旧结果) ──
        try:
            if self._sidecar is None:
                from engine.sidecar_runner import SidecarRunner
                self._sidecar = SidecarRunner()
            sidecar_result = self._sidecar.run()
            state.sidecar_state = sidecar_result  # 写入 shared_state 供仪表盘读取
        except Exception:
            pass  # 旁路失败不影响主链路

        # 更新共识追踪器
        if consensus_mod.consensus_tracker:
            # 构建 cb_code → stock_code 映射 (供共识追踪器龙头匹配)
            stock_of = {}
            if self.fusion and hasattr(self.fusion, '_monitor_map'):
                for cb, mi in self.fusion._monitor_map.items():
                    sc = mi.get('stock_code', '')
                    if sc:
                        stock_of[cb] = sc

            consensus_mod.consensus_tracker.update(snapshots, stock_of)

            # 更新扩散指标 + RRG
            cm = {}
            try:
                if hasattr(self.signal_engine, '_concept_map'):
                    cm = self.signal_engine._concept_map
            except:
                pass
            if consensus_mod.diffusion:
                consensus_mod.diffusion.update(snapshots, cm)
            if consensus_mod.rrg and cm:
                consensus_mod.rrg.update(snapshots, cm)

            # 刷新概念板块指数 (Fuyao, 每60秒自动节流, 增量优化)
            if consensus_mod.concept_index:
                # 注入活跃概念 thscode (stage≥1 → 仅刷新活跃板块, 减少80%+请求)
                active_thscodes = self._get_active_concept_codes()
                if active_thscodes:
                    consensus_mod.concept_index.set_active_codes(active_thscodes)
                consensus_mod.concept_index.refresh()

    def _get_active_concept_codes(self) -> set:
        """从 ConsensusTracker 获取活跃概念 (stage≥1) 并映射为 thscode"""
        active = set()
        if not consensus_mod.consensus_tracker:
            return active
        try:
            all_states = consensus_mod.consensus_tracker.get_all()
            ci = consensus_mod.concept_index
            for name, sdata in all_states.items():
                if sdata.get('stage', 0) >= 1:
                    code = None
                    if ci:
                        code = ci._name_to_code.get(name)
                    if code:
                        active.add(code)
                    # 如果没有 thscode 映射, 尝试子串匹配
                    elif ci:
                        for cn, tc in ci._name_to_code.items():
                            if name in cn or cn in name:
                                active.add(tc)
                                break
        except Exception:
            pass
        return active

    def _handle_non_trading_hours(self):
        """非交易时段处理"""
        now = datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M')
        day_name = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        today_str = now.strftime('%Y%m%d')

        if not trading_calendar.is_trading_day():
            if now.weekday() >= 5:
                status = f"🛑 周末/假日 ({day_name}) - 休眠中"
            else:
                status = f"🛑 非交易日 ({day_name}) - 休眠中"
        else:
            # 交易日盘前刷新选债池 (8:00-9:25, 当日只刷新一次)
            is_morning = now.hour == 8 or (now.hour == 9 and now.minute < 25)
            if is_morning and self._last_cov_date != today_str:
                logger.info("开盘前刷新选债池...")
                self._refresh_bond_pool()
                self._last_cov_date = today_str
                # ── 清空 Fuyao 缓存: 避免昨日数据残留 ──
                logger.info("清空昨日 Fuyao 缓存, 等待调度器重新拉取")
                state.update_fuyao_pool([], ts=time.time(), error='new_day_reset')
                state.update_fuyao_ladder({}, ts=time.time(), error='new_day_reset')

            status = f"⏳ 非交易时段 ({day_name}) - 下一交易时段: 等待9:30"

        OutputFormatter.clear_screen()
        print(f"\n{CYAN}━━━ 可转债日内联动监控{RESET}")
        print(f"\n  {status}")
        print(f"  当前时间: {now_str}")
        print(f"  数据源: 通达信 + 东方财富转债池")
        if now.weekday() < 5:
            if now.hour < 9 or (now.hour == 9 and now.minute < 30):
                print(f"  距离开盘: {(9*60+30) - (now.hour*60+now.minute)} 分钟")
            elif 1130 < now.hour < 13 or (now.hour == 11 and now.minute > 30):
                print(f"  午间休市: 13:00 复盘")
            else:
                print(f"  今日已收盘")
        print(f"\n  {DIM}下一轮检查: 15分钟后{RESET}")
        # 收盘后显示当日统计
        stats_summary = self.daily_stats.summary
        if stats_summary:
            print(f"  {DIM}{stats_summary}{RESET}")

        # 收盘后生成盘后报告 (15:00-15:30 执行一次)
        if now.weekday() < 5 and (now.hour == 15 and now.minute < 30):
            try:
                from scripts.post_market import PostMarketReporter
                reporter = PostMarketReporter()
                report = reporter.generate()
                print(f"\n  {GREEN}━━━ 盘后报告 ━━━{RESET}")
                print(f"  {DIM}{report}{RESET}")
            except Exception as e:
                logger.warning(f"盘后报告生成失败: {e}")

        print(f"\n{CYAN}{'━' * 40}{RESET}")

    def _refresh_fuyao_cache(self):
        """刷新 Fuyao 涨停池 + 连板天梯缓存 (独立于TDX周期, 每5s)
        
        目的: 避免 API 端点 3s 轮询造成 Fuyao 429 限流。
        只有调度器调用 Fuyao API, 端点只读缓存。
        """
        now = time.time()

        # ── 跨日检测: 新交易日首次强制刷新, 忽略60s节流 ──
        cache_date = time.strftime('%Y%m%d', time.localtime(
            self._last_fuyao_refresh)) if self._last_fuyao_refresh > 0 else ''
        today = time.strftime('%Y%m%d', time.localtime(now))
        force_refresh = (cache_date != today)

        if not force_refresh and now - self._last_fuyao_refresh < 90.0:
            return  # 未到刷新间隔 (90s, 避免Fuyao 429限流)
        self._last_fuyao_refresh = now

        try:
            from core.fuyao_client import get_fuyao_client
            fc = get_fuyao_client()
        except Exception:
            return

        # 1. 涨停池
        try:
            pool = fc.get_limit_up_pool(sort_field="limit_up_time", sort_dir="asc", size=200)
            if pool:
                items = pool.get('data', {}).get('item', [])
                state.update_fuyao_pool(items, ts=now)
                if force_refresh:
                    logger.info(f"新交易日 Fuyao 涨停池刷新: {len(items)} 只")
            else:
                # None → 可能是限流, 保留旧缓存不覆盖
                logger.debug("Fuyao 涨停池返回空, 保留旧缓存")
                # 跨日时: 返回空也清理旧缓存, 避免API端点误显昨日数据
                if force_refresh:
                    state.update_fuyao_pool([], ts=now, error='new_day_empty_response')
                    logger.warning("新交易日 Fuyao 涨停池返回空, 已清理昨日缓存")
        except Exception as e:
            logger.debug(f"Fuyao 涨停池刷新异常: {e}")
            if force_refresh:
                state.update_fuyao_pool([], ts=now, error=f'new_day_error: {e}')

        # 2. 连板天梯
        try:
            ladder = fc.get_limit_up_ladder()
            if ladder:
                state.update_fuyao_ladder(ladder, ts=now)
            else:
                logger.debug("Fuyao 连板天梯返回空, 保留旧缓存")
                if force_refresh:
                    state.update_fuyao_ladder({}, ts=now, error='new_day_empty_response')
        except Exception as e:
            logger.debug(f"Fuyao 连板天梯刷新异常: {e}")
            if force_refresh:
                state.update_fuyao_ladder({}, ts=now, error=f'new_day_error: {e}')

    def run(self):
        """主循环"""
        self._refresh_bond_pool()
        self._last_cov_date = datetime.now().strftime('%Y%m%d')

        try:
            while not self._stop:

                if self._is_trading_hours():
                    # 交易时段 - 3秒轮询
                    self._run_cycle()
                    # Fuyao 缓存独立刷新 (每5s, 不受TDX周期影响)
                    self._refresh_fuyao_cache()
                    time.sleep(self.config['spot_interval'])

                else:
                    # 非交易时段 - 精确计算到下次开盘的等待时间
                    self._handle_non_trading_hours()
                    wait = self._seconds_until_next_trading()
                    time.sleep(wait)

        except KeyboardInterrupt:
            self.stop()
        except Exception as e:
            logger.error(f"调度器异常: {e}", exc_info=True)
            self.stop()

    def stop(self):
        """停止调度"""
        self._stop = True
        # 关闭信号日志
        self.signal_logger.close()
        # 关闭通达信连接
        try:
            from core.data_fusion import TdxClient
            TdxClient.close()
        except Exception:
            pass
        print(f"\n  {GRAY}监控已停止{RESET}")
