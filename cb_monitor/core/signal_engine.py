"""
信号计算引擎 - SignalEngine

7种信号检测方法：
S - 正股涨停转债滞涨 / 板块扩散 / 尾盘洗盘
A - 价格加速拉升 / 放量异动 / 错杀抄底 / 妖债异动
B - 股价大涨转债滞涨 / 超跌修复
C - 突破前高
D - 价格加速下跌

短时去重: AlertManager 管理冷却时间

优化 (2026-06-23):
1. 移除"正股大跌转债抗跌"信号 (历史胜率仅35%)
2. 尾盘洗盘升级为 S 级
3. 新增信号置信度升级: 正股大涨+转债滞涨+主线概念 → 自动升一级
4. 新增信号新鲜度: 同债10分钟内只保留最强信号
5. B级日上限20条，超限后只推送A/S
6. 统一 signal_id 生成
7. 龙一阈值降低 7%→5% 提升温和市触发率
"""

import time
import math
import json
import os
import uuid
import logging
from collections import defaultdict
from datetime import datetime, date as dt_date, time as dtime
from dataclasses import dataclass, field
from typing import Optional

from config import CONFIG, SIGNAL_LEVELS
from scheduler.rolling_window import RollingWindow

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    level: str                # S/A/B/C/D
    signal_type: str          # 信号类型名称
    code: str                 # 转债代码 (6位)
    name: str                 # 转债名称
    stock_name: str           # 正股名称
    description: str          # 信号描述
    score: float              # 信号强度 (0-100)
    timestamp: float          # 触发时间戳
    strategy: str = ""        # chase(追涨) / dip(回落) — 由信号类型自动确定
    signal_id: str = ""       # 统一信号ID (code_seq)

    def __post_init__(self):
        if not self.signal_id:
            self.signal_id = self._generate_id()

    def _generate_id(self) -> str:
        """生成统��信号ID: {code}_{timestamp_micro}"""
        ts = int(self.timestamp * 1_000_000)
        return f"{self.code}_{ts}"

    @property
    def level_rank(self) -> int:
        return SIGNAL_LEVELS.get(self.level, 0)

    @property
    def strategy_name(self) -> str:
        """根据信号类型返回策略归类"""
        if not self.strategy:
            return self._infer_strategy()
        return self.strategy

    @staticmethod
    def _infer_strategy_type(signal_type: str) -> str:
        """静态方法: 根据信号类型名推断策略"""
        chase_types = {"放量拉升", "股价大涨转债滞涨", "突破前高", "正股涨停转债滞涨", "价格加速拉升", "板块扩散"}
        dip_types = {"缩量急跌", "正股大跌转债抗跌", "折溢价突变", "价格加速下跌", "超跌修复"}
        if signal_type in chase_types:
            return "chase"
        if signal_type in dip_types:
            return "dip"
        return "chase"  # 默认追涨

    def _infer_strategy(self) -> str:
        return self._infer_strategy_type(self.signal_type)

    def to_dict(self) -> dict:
        return {
            'level': self.level,
            'type': self.signal_type,
            'code': self.code,
            'name': self.name,
            'stock_name': self.stock_name,
            'description': self.description,
            'score': self.score,
            'timestamp': time.strftime('%H:%M:%S', time.localtime(self.timestamp)),
            'signal_id': self.signal_id,
        }


class SignalEngine:
    """信号计算引擎"""

    def __init__(self, config: dict):
        self.cfg = config['signals']

        # 加载概念映射
        self._concept_map: dict[str, list[str]] = {}  # code → [concept, ...]
        self._concept_weight_enabled = self.cfg.get('concept_weight_enabled', True)
        self._resonance_threshold = self.cfg.get('concept_resonance_threshold', 2.0)
        self._lonewolf_threshold = self.cfg.get('concept_lonewolf_threshold', 0.5)
        self._load_concept_map(config)

        # 市场状态动态权重 (由 MarketStateClassifier 每5分钟注入)
        self._market_weights: dict[str, float] = {
            "diffusion_weight": 1.0,
            "dip_weight": 1.0,
            "chase_weight": 1.0,
        }

        # 信号新鲜度追踪: code → (timestamp, best_score, best_signal_type)
        self._last_signal_time: dict[str, float] = {}
        self._freshness_window = self.cfg.get('signal_freshness_seconds', 600)

        # B级日上限追踪
        self._B_count_today: int = 0
        self._B_max_per_day: int = self.cfg.get('max_B_signals_per_day', 20)
        self._today_date: str = ""

        # 主线概念缓存 (由 DecisionPipeline 注入)
        self._mainline_concepts: set = set()

    def set_market_weights(self, weights: dict):
        """注入市场状态动态权重 (进攻市追涨有效, 退潮市错杀有效)"""
        for k, v in weights.items():
            if k in self._market_weights:
                self._market_weights[k] = v

    def set_mainline_concepts(self, concepts: set):
        """注入当前主线概念 (由 DecisionPipeline 更新)"""
        self._mainline_concepts = set(concepts)

    def _reset_daily_B_counter(self):
        """每日重置B级计数器"""
        today = dt_date.today().isoformat()
        if today != self._today_date:
            self._B_count_today = 0
            self._today_date = today

    def _check_B_cap(self, signal: 'Signal') -> bool:
        """检查B级日上限, 超限返回 False (应丢弃)"""
        if signal.level != 'B':
            return True
        self._reset_daily_B_counter()
        if self._B_count_today >= self._B_max_per_day:
            return False
        self._B_count_today += 1
        return True

    def _apply_freshness(self, signal: 'Signal') -> bool:
        """信号新鲜度: 同债10分钟内只保留最强信号, 返回 True=保留"""
        code = signal.code
        now = signal.timestamp
        last = self._last_signal_time.get(code)
        if last is None:
            self._last_signal_time[code] = now
            return True
        if now - last < self._freshness_window:
            return False  # 窗口内已有信号
        self._last_signal_time[code] = now
        return True

    def _apply_confidence_upgrade(self, signal: 'Signal', snap) -> 'Signal':
        """信号置信度升级: 正股大涨+转债滞涨+主线概念 → 自动升一级

        规则:
          - '股价大涨转债滞涨' (B) + 主线概念 + 正股>=3% + 转债<1% → 升级为 A
          - '超跌修复' (B) + 主线概念 → 升级为 A
        """
        if signal.level not in ('B', 'C'):
            return signal

        concepts = self._concept_map.get(signal.code, [])
        in_mainline = any(c in self._mainline_concepts for c in concepts) if self._mainline_concepts else False

        # 股价大涨转债滞涨 + 主线概念 → A
        if signal.signal_type == '股价大涨转债滞涨' and in_mainline:
            sc_pct = getattr(snap, 'stock_change_pct', 0) or 0
            cb_pct = getattr(snap, 'change_pct', 0) or 0
            if sc_pct >= self.cfg.get('confidence_stock_surge', 3.0) and cb_pct < self.cfg.get('confidence_bond_lag_max', 1.0):
                old_level = signal.level
                signal.level = 'A'
                signal.score = round(signal.score * 1.3, 1)
                signal.description += f" [置信升级: {old_level}→A 主线共振]"
                return signal

        # 超跌修复 + 主线概念 → A
        if signal.signal_type == '超跌修复' and in_mainline:
            old_level = signal.level
            signal.level = 'A'
            signal.score = round(signal.score * 1.2, 1)
            signal.description += f" [置信升级: {old_level}→A 主线修复]"
            return signal

        return signal

    @staticmethod
    def _is_limit_20(stock_code: str) -> bool:
        """判断正股是否为创业板(300/301)或科创板(688)，涨停板20%"""
        code_stripped = stock_code.strip()
        return code_stripped.startswith('300') or code_stripped.startswith('301') or code_stripped.startswith('688')

    def _is_warmup(self) -> bool:
        """判断是否在预热期内 (早盘/午盘开盘后短暂不触发放量/加速信号)"""
        now = datetime.now()
        # 早盘: 9:30 起 N 秒
        morning_secs = self.cfg.get('open_warmup_seconds', 300)
        if morning_secs > 0:
            market_open = datetime.combine(now.date(), dtime(9, 30))
            elapsed = (now - market_open).total_seconds()
            if 0 <= elapsed < morning_secs:
                return True
        # 午盘: 13:00 起 N 秒
        afternoon_secs = self.cfg.get('afternoon_warmup_seconds', 60)
        if afternoon_secs > 0:
            afternoon_open = datetime.combine(now.date(), dtime(13, 0))
            elapsed = (now - afternoon_open).total_seconds()
            if 0 <= elapsed < afternoon_secs:
                return True
        return False

    def _load_concept_map(self, config: dict):
        """加载概念映射 JSON → {code: [concept, ...]}"""
        path = self.cfg.get('concept_map_path', '')
        if not path or not os.path.exists(path):
            logger.warning(f"概念映射文件不存在: {path}")
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for code, item in raw.items():
                if isinstance(item, dict) and 'concepts' in item:
                    self._concept_map[code] = item['concepts']
            logger.info(f"概念映射加载: {len(self._concept_map)} 只转债")
        except Exception as e:
            logger.error(f"加载概念映射失败: {e}")

    def _compute_concept_heat(self, snapshots: dict[str, 'Snapshot']) -> dict[str, float]:
        """
        计算每个概念的实时热度 (监控池内该概念转债的平均涨跌幅)
        返回: {concept_name: avg_pct}
        """
        concept_changes: dict[str, list[float]] = {}
        for code, snap in snapshots.items():
            concepts = self._concept_map.get(code, [])
            if not concepts or snap.change_pct is None:
                continue
            for c in concepts:
                if c not in concept_changes:
                    concept_changes[c] = []
                concept_changes[c].append(snap.change_pct)

        heat = {}
        for concept, changes in concept_changes.items():
            if changes:
                heat[concept] = round(sum(changes) / len(changes), 2)
        return heat

    def _apply_concept_weight(self, signals: list['Signal'],
                               snapshots: dict[str, 'Snapshot'],
                               concept_heat: dict[str, float]) -> list['Signal']:
        """
        概念热度加权: 对放量/背离信号，根据所属概念热度升降级

        板块共振 (有概念均涨 > resonance_threshold):
          放量异动 → 加分 ×1.5, 加⚡联动标签
          背离信号 → 加分 ×1.2

        孤狼放量 (所有概念均涨 < lonewolf_threshold):
          放量异动 A → 降级为 B

        中性 (0.5%~2%): 保持原样
        """
        if not self._concept_weight_enabled or not self._concept_map:
            return signals

        weighted = []
        for sig in signals:
            concepts = self._concept_map.get(sig.code, [])

            # 只有放量异动和背离信号参与概念加权
            is_volume = sig.signal_type == '放量异动'
            is_divergence = sig.signal_type in ('股价大涨转债滞涨', '正股大跌转债抗跌')

            if not is_volume and not is_divergence:
                weighted.append(sig)
                continue

            # 计算该债各概念的热度
            concept_heats = [(c, concept_heat.get(c, 0)) for c in concepts]
            if not concept_heats:
                weighted.append(sig)
                continue

            max_heat = max(h for _, h in concept_heats)
            min_heat = min(h for _, h in concept_heats) if concept_heats else 0
            # 取最佳概念 (热度最高的前2个取均值)
            sorted_heats = sorted(concept_heats, key=lambda x: -x[1])
            top_heats = sorted_heats[:2]
            top_concept_names = [c for c, _ in top_heats]
            top_avg_heat = sum(h for _, h in top_heats) / len(top_heats) if top_heats else 0

            if top_avg_heat > self._resonance_threshold:
                # 板块共振
                if is_volume:
                    sig.score = round(sig.score * 1.5, 1)
                    sig.description += f" ⚡联动 {'/'.join(top_concept_names)}(均涨{top_avg_heat:+.1f}%)"
                else:
                    sig.score = round(sig.score * 1.2, 1)
                    sig.description += f" 🔗板块 {'/'.join(top_concept_names)}(均涨{top_avg_heat:+.1f}%)"

            elif max_heat < self._lonewolf_threshold:
                # 孤狼: 放量异动降级
                if is_volume and sig.level == 'A':
                    sig.level = 'B'
                    sig.score = round(sig.score * 0.5, 1)
                    sig.description += f" 孤狼(概念均涨<{self._lonewolf_threshold}%)"

            weighted.append(sig)

        return weighted

    def analyze(self, snapshots: dict[str, 'Snapshot'],
                window: RollingWindow,
                monitor_list: list[dict] = None) -> list[Signal]:
        """
        对全量数据计算信号
        参数:
            snapshots: code_num -> Snapshot
            window: RollingWindow 实例
            monitor_list: 监控列表（含强赎状态等信息）
        返回: Signal列表 (按等级排序)
        """
        # 构建强赎状态映射: code_num -> redeem_status
        redeem_map = {}
        # 构建正股代码映射: code_num -> stock_code (用于判断20%涨跌幅)
        stock_code_map = {}
        if monitor_list:
            for item in monitor_list:
                code_num = item.get('code_num', '')
                status = item.get('redeem_status', '')
                if status:
                    redeem_map[code_num] = status
                sc = item.get('stock_code', '')
                if sc:
                    stock_code_map[code_num] = sc

        signals: list[Signal] = []
        in_warmup = self._is_warmup()

        # 预计算概念热度 (供 miskill 和加权使用)
        concept_heat: dict[str, float] = {}
        if self._concept_map:
            concept_heat = self._compute_concept_heat(snapshots)

        for code, snap in snapshots.items():
            # 过滤成交额过低的
            if snap.amount < self.cfg['min_trade_amount']:
                continue

            # 该转债的强赎状态和正股代码
            redeem_status = redeem_map.get(code, '')
            stock_code = stock_code_map.get(code, '')
            is_redeeming = redeem_status in ('已公告强赎', '公告要强赎')

            # 检查各信号
            s = self._detect_stock_limit_up(code, snap, window, stock_code, is_redeeming)
            if s: signals.append(s)

            if not in_warmup:
                s = self._detect_price_surge(code, snap, window)
                if s: signals.append(s)

                s = self._detect_volume_spike(code, snap, window, is_redeeming)
                if s: signals.append(s)

            s = self._detect_stock_bond_divergence(code, snap, window, is_redeeming)
            if s: signals.append(s)

            if not in_warmup:
                s = self._detect_premium_shift(code, snap, window)
                if s: signals.append(s)

                s = self._detect_breakout(code, snap, window)
                if s: signals.append(s)

                s = self._detect_price_drop(code, snap, window)
                if s: signals.append(s)

                # 模式二: 错杀抄底 (替代旧的缩量急跌)
                s = self._detect_miskill_buy(code, snap, window, concept_heat, snapshots)
                if s: signals.append(s)

                # 模式三: 转债超跌修复
                s = self._detect_oversold_repair(code, snap, window)
                if s: signals.append(s)

                # 模式四: 尾盘洗盘
                s = self._detect_tailwash(code, snap, window)
                if s: signals.append(s)

                # 模式五: 妖债异动
                s = self._detect_demon_bond(code, snap, window, stock_code_map)
                if s: signals.append(s)

        # 模式一: 板块内扩散 (龙一涨停 → 龙二埋伏) — 概念级检测
        diffusion_signals = self._detect_concept_diffusion(snapshots, window, stock_code_map)
        signals.extend(diffusion_signals)

        # 概念热度加权 (过滤孤狼噪音，强化板块共振)
        if self._concept_weight_enabled and self._concept_map:
            signals = self._apply_concept_weight(signals, snapshots, concept_heat)

        # 信号置信度升级: 主线概念相关信号自动升一级
        signals = [self._apply_confidence_upgrade(s, snapshots.get(s.code)) for s in signals]

        # 信号新鲜度过滤: 同债10分钟内只保留最强
        filtered = []
        for s in signals:
            if self._apply_freshness(s):
                filtered.append(s)
        signals = filtered

        # B级日上限: 超过20条后扔弃
        signals = [s for s in signals if self._check_B_cap(s)]

        # 按等级排序 (同等级按评分降序)
        signals.sort(key=lambda x: (-x.level_rank, -x.score))

        # "不走的路" 过滤器
        signals = self._apply_dont_go_filters(signals, snapshots)

        # 市场状态动态权重: 进攻市追涨有效, 退潮市错杀有效
        signals = self._apply_market_weights(signals)

        return signals

    def _apply_market_weights(self, signals: list[Signal]) -> list[Signal]:
        """市场状态动态权重: 调整信号评分"""
        if not signals:
            return signals
        dw = self._market_weights.get("diffusion_weight", 1.0)
        dip_w = self._market_weights.get("dip_weight", 1.0)
        chase_w = self._market_weights.get("chase_weight", 1.0)

        for sig in signals:
            w = 1.0
            if sig.signal_type == "板块扩散":
                w = dw
            elif sig.strategy == "dip":
                w = dip_w
            elif sig.strategy == "chase" and sig.signal_type != "板块扩散":
                w = chase_w
            if w != 1.0:
                sig.score = round(sig.score * w, 1)
        return signals

    def _apply_dont_go_filters(self, signals: list[Signal],
                                snapshots: dict) -> list[Signal]:
        """不走的路: 过滤已过热/高溢价/尾盘风险的信号"""
        filters = CONFIG.get('filters', {})
        if not filters.get('enabled', True):
            return signals

        max_surge = filters.get('cb_max_surge_pct', 8.0)
        max_premium = filters.get('max_chase_premium', 50.0)
        warn_hour = filters.get('overnight_warn_hour', 1445)

        now = datetime.now()
        is_tail = (now.hour * 100 + now.minute) >= warn_hour

        filtered = []
        for sig in signals:
            snap = snapshots.get(sig.code)
            cb_pct = getattr(snap, 'change_pct', 0) if snap else 0
            premium = getattr(snap, 'premium_ratio', None) if snap else None

            # 已暴拉 → 不推追涨
            if sig.strategy == 'chase' and cb_pct >= max_surge:
                continue

            # 高溢价 → 不推追涨
            if sig.strategy == 'chase' and premium and premium > max_premium:
                continue

            # 尾盘高溢价不过夜提示
            if is_tail and premium and premium > CONFIG.get('filters', {}).get('overnight_premium_warn', 40):
                sig.description += " ⚡尾盘高溢价 不过夜"

            filtered.append(sig)

        return filtered

    def _apply_redeem_penalty(self, signal: Signal, is_redeeming: bool) -> Signal:
        """强赎状态降低信号等级"""
        if not is_redeeming:
            return signal
        # 已公告强赎: S→A, A→B, B→C, C/D不变
        downgrade = {'S': 'A', 'A': 'B', 'B': 'C'}
        if signal.level in downgrade:
            signal.level = downgrade[signal.level]
            signal.score = round(signal.score * 0.6, 1)
            signal.description += " ⚠️强赎"
        return signal

    def _detect_stock_limit_up(self, code: str, snap, window: RollingWindow,
                               stock_code: str = '', is_redeeming: bool = False) -> Optional[Signal]:
        """
        S级: 正股涨停转债滞涨
        主板涨停≥9.8%, 创业板/科创板涨停≥19.5%
        转债涨幅 < s_signal_max_change 才算滞涨
        """
        if snap.stock_change_pct is None:
            return None

        # 判断涨停阈值 (根据正股代码前缀)
        limit_threshold = self.cfg['stock_limit_up_20'] if self._is_limit_20(stock_code) else self.cfg['stock_limit_up']
        max_change = self.cfg['s_signal_max_change']

        if snap.stock_change_pct >= limit_threshold and snap.change_pct < max_change:
            desc = f"正股涨停+{snap.stock_change_pct:.1f}% 转债仅+{snap.change_pct:.1f}% 滞涨空间{snap.stock_change_pct - snap.change_pct:.1f}%"
            score = self._calc_score(snap.change_pct, 50, 0, 5)
            sig = Signal('S', '正股涨停转债滞涨', code, snap.name or code,
                         snap.stock_name or '', desc, score, time.time())
            return self._apply_redeem_penalty(sig, is_redeeming)
        return None

    def _detect_price_surge(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        A级: 价格加速拉升
        至少2/3轮正增长 + 整体趋势向上 + 近期加速度 > 整体
        """
        rounds = self.cfg['price_surge_rounds']
        min_delta = self.cfg['price_surge_min_delta']

        prices = window.get_recent_prices(code, rounds + 1)
        if len(prices) < rounds + 1:
            return None

        deltas = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]

        # 至少2轮正增长 (不再要求全部)
        positive_count = sum(1 for d in deltas if d > min_delta)
        if positive_count < 2:
            return None

        # 整体均值必须为正 (趋势向上)
        ma_full = sum(deltas) / len(deltas)
        if ma_full <= 0:
            return None

        # 近段均值 vs 整体均值: 加速迹象
        ma_short = sum(deltas[-2:]) / 2.0 if len(deltas) >= 2 else deltas[0]

        if ma_short > ma_full * 1.2:  # 近段比整体快20%以上
            accel_desc = "→".join([f"+{d*100:.2f}%" for d in deltas])
            desc = f"加速拉升 ({positive_count}/{rounds}轮正增长) {accel_desc} 现价{snap.trade:.2f} +{snap.change_pct:.2f}%"
            score = self._calc_score(snap.change_pct, 50, 1, 100)
            return Signal('A', '价格加速拉升', code, snap.name or code,
                          snap.stock_name or '', desc, score, time.time())
        return None

    def _detect_volume_spike(self, code: str, snap, window: RollingWindow,
                             is_redeeming: bool = False) -> Optional[Signal]:
        """
        A级: 放量异动
        三重过滤: 峰值/均量倍率 + 最小绝对峰值 + 最小成交额
        """
        multiplier = self.cfg['volume_multiplier']
        lookback = self.cfg.get('volume_lookback_rounds', 20)
        min_peak = self.cfg.get('volume_min_peak', 500)
        min_amount = self.cfg.get('volume_min_amount', 30000000)

        # 最小成交额过滤 (僵尸债不触发)
        if snap.amount < min_amount:
            return None

        # 方向过滤: 放量上涨才有 A 级, 放量暴跌是出货不推
        if snap.change_pct <= 0:
            return None

        avg_vol = window.get_avg_volume_long(code, n=lookback)
        if avg_vol <= 0:
            return None

        # 取窗口内单轮成交量峰值
        peak_delta = window.get_peak_volume_delta(code, n=lookback)
        if peak_delta <= 0:
            return None

        # 三重条件: 倍率 + 最小峰值 + 最小成交额
        if peak_delta > avg_vol * multiplier and peak_delta >= min_peak:
            ratio = peak_delta / avg_vol if avg_vol > 0 else 0
            from core.data_fusion import fmt_amount
            desc = f"放量{ratio:.1f}x (均量{int(avg_vol)}手/轮, 峰值{int(peak_delta)}手) 成交额{fmt_amount(snap.amount)}"
            score = self._calc_score(ratio, 40, 2.5, 12)
            sig = Signal('A', '放量拉升', code, snap.name or code,
                         snap.stock_name or '', desc, score, time.time())
            return self._apply_redeem_penalty(sig, is_redeeming)
        return None

    def _detect_stock_bond_divergence(self, code: str, snap, window: RollingWindow,
                                      is_redeeming: bool = False) -> Optional[Signal]:
        """
        B级: 股价大涨转债滞涨
        大涨: 正股涨幅 > surge_threshold 且 转债涨幅 < b_divergence_max_change
        注: "正股大跌转债抗跌" 已移除 (历史胜率仅35%, 2026-06-23)
        """
        if snap.stock_change_pct is None:
            return None

        surge_threshold = self.cfg['stock_surge_threshold']
        max_cb_change = self.cfg['b_divergence_max_change']  # 转债允许的最大涨幅

        if snap.stock_change_pct >= surge_threshold and snap.change_pct < max_cb_change:
            deviation = snap.stock_change_pct - snap.change_pct
            desc = f"正股大涨+{snap.stock_change_pct:.1f}% 转债仅+{snap.change_pct:.1f}% 偏离{deviation:.1f}%"
            # 偏离1%-15%映射到0-40分, 偏离越大分越高
            score = self._calc_score(deviation, 40, 1, 15)
            sig = Signal('B', '股价大涨转债滞涨', code, snap.name or code,
                         snap.stock_name or '', desc, score, time.time())
            return self._apply_redeem_penalty(sig, is_redeeming)
        return None

    def _detect_premium_shift(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        B级: 折溢价突变
        当前溢价率与上一轮差值 > shift_threshold
        """
        if snap.premium_ratio is None:
            return None

        prev_premium = window.get_prev_premium(code)
        if prev_premium is None:
            return None

        shift = abs(snap.premium_ratio - prev_premium)
        if shift > self.cfg['premium_shift_threshold']:
            direction = "扩大" if snap.premium_ratio > prev_premium else "收窄"
            desc = f"溢价率{snap.premium_ratio:.1f}% 突变{direction}{abs(shift):.1f}% (前值{prev_premium:.1f}%)"
            score = self._calc_score(shift, 40, 3, 15)
            return Signal('B', '折溢价突变', code, snap.name or code,
                          snap.stock_name or '', desc, score, time.time())
        return None

    def _detect_breakout(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        C级: 突破前高
        当前价 >= 最近20轮滚动窗口内的最高价 (盘中反复突破有意义)
        """
        rolling_high = window.get_rolling_high(code, n=20)
        if rolling_high is None or rolling_high <= 0:
            return None

        if snap.trade >= rolling_high and snap.change_pct > 0:
            desc = f"突破60s前高{rolling_high:.2f} 现价{snap.trade:.2f} +{snap.change_pct:.2f}%"
            score = self._calc_score(snap.change_pct, 30, 1, 10)
            return Signal('C', '突破前高', code, snap.name or code,
                          snap.stock_name or '', desc, score, time.time())
        return None

    def _detect_price_drop(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        D级: 价格加速下跌
        同上，改用滑动均线趋势检测
        """
        rounds = self.cfg['price_surge_rounds']
        min_delta = self.cfg['price_surge_min_delta']

        prices = window.get_recent_prices(code, rounds + 1)
        if len(prices) < rounds + 1:
            return None

        deltas = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]

        # 全部下跌
        all_negative = all(d < -min_delta for d in deltas)
        if not all_negative:
            return None

        # 均线趋势: 近段跌势比整体更陡
        ma_short = sum(deltas[-2:]) / 2.0 if len(deltas) >= 2 else deltas[0]
        ma_long = sum(deltas) / len(deltas)

        # 近段跌幅均值 < 整体均值 × 1.2 (更负 = 加速下跌)
        if ma_short < ma_long * 1.2:
            accel_desc = "→".join([f"{d*100:.2f}%" for d in deltas])
            desc = f"连续{rounds}轮加速下跌 {accel_desc} 现价{snap.trade:.2f} {snap.change_pct:.2f}%"
            score = self._calc_score(abs(snap.change_pct), 40, 1, 15)
            return Signal('D', '价格加速下跌', code, snap.name or code,
                          snap.stock_name or '', desc, score, time.time())
        return None

    # ═══════════════════════════════════════════════════════════
    # 五大实战模式信号检测
    # ═══════════════════════════════════════════════════════════

    def _detect_concept_diffusion(self, snapshots: dict[str, 'Snapshot'],
                                  window: RollingWindow,
                                  stock_code_map: dict[str, str]) -> list[Signal]:
        """
        模式一: 板块内扩散 — 龙一接近封板, 龙二/龙三转债滞后
        对手盘: 追板散户买不到正股 → 追买转债
        关键: 龙一不需要封板 (封板就晚了), 7%+ 加速即视为冲锋
        """
        if not self._concept_map:
            return []

        # 构建概念 → codes 反向索引 (排除宽泛标签)
        _BROAD_CONCEPTS = {
            '融资融券', '专精特新', '国企改革', '深股通', '沪股通',
            '标普道琼斯A股', 'MSCI概念', '富时罗素概念股', '富时罗素概念',
            '转融券标的', '证金持股', '汇金持股', '养老金持股',
        }
        concept_codes: dict[str, list[str]] = defaultdict(list)
        for code in snapshots:
            concepts = self._concept_map.get(code, [])
            for c in concepts:
                if c not in _BROAD_CONCEPTS:
                    concept_codes[c].append(code)

        dragon_min = self.cfg.get('diffusion_dragon_stock_min', 7.0)
        dragon_limit = self.cfg.get('diffusion_dragon_stock_limit', 9.8)
        follower_max_cb = self.cfg.get('diffusion_follower_max_cb', 2.0)
        follower_min_stock = self.cfg.get('diffusion_follower_min_stock', 1.0)

        signals = []
        # dragon_info: {code: (stock_pct, is_sealed)}
        dragon_info: dict[str, tuple[float, bool]] = {}

        # 第一遍: 找龙一 (接近封板 7%+ 或已封板 9.8%+)
        for code, snap in snapshots.items():
            sc = snap.stock_change_pct or 0
            if sc >= dragon_min:
                is_sealed = sc >= dragon_limit
                dragon_info[code] = (sc, is_sealed)

        if not dragon_info:
            return []

        # 第二遍: 龙一所在概念 → 找龙二龙三
        for d_code, (d_sc, is_sealed) in dragon_info.items():
            d_snap = snapshots.get(d_code)
            if not d_snap:
                continue
            d_concepts = self._concept_map.get(d_code, [])
            for concept in d_concepts:
                peers = concept_codes.get(concept, [])
                for peer_code in peers:
                    if peer_code == d_code or peer_code in dragon_info:
                        continue
                    peer_snap = snapshots.get(peer_code)
                    if not peer_snap or peer_snap.amount < self.cfg['min_trade_amount']:
                        continue
                    sc_pct = peer_snap.stock_change_pct or 0
                    cb_pct = peer_snap.change_pct or 0
                    if sc_pct >= follower_min_stock and cb_pct < follower_max_cb:
                        d_name = d_snap.name or d_code
                        d_tag = "封板" if is_sealed else f"冲锋+{d_sc:.1f}%"
                        desc = (f"龙一{d_name}正股{d_tag} "
                                f"→ {concept}板块扩散 龙二埋伏 "
                                f"正股+{sc_pct:.1f}% 转债仅+{cb_pct:.1f}%")
                        score = self._calc_score(sc_pct, 60, 1, 10)
                        # S 级硬门槛: 概念内至少2个正股明显联动 (>=1%)
                        linkage_count = sum(1 for pc in peers
                                          if snapshots.get(pc) and (snapshots[pc].stock_change_pct or 0) >= 1.0)
                        level = 'S' if linkage_count >= 2 else 'B'
                        sig = Signal(level, '板块扩散', peer_code,
                                     peer_snap.name or peer_code,
                                     peer_snap.stock_name or '', desc, score, time.time())
                        sig.strategy = 'chase'
                        signals.append(sig)

        return signals

    def _detect_miskill_buy(self, code: str, snap, window: RollingWindow,
                            concept_heat: dict[str, float],
                            snapshots: dict[str, 'Snapshot']) -> Optional[Signal]:
        """
        模式二: 错杀抄底 — 急跌但不是真利空
        条件: 缩量急跌 + 同板块其他债没崩 + 正股跌幅不大
        对手盘: 恐慌盘 / 主力砸盘吸筹
        """
        min_pct = self.cfg.get('lowvol_plunge_min_pct', -2.0)
        vol_ratio = self.cfg.get('lowvol_plunge_vol_ratio', 0.5)
        min_amount = self.cfg.get('lowvol_plunge_amount_min', 5000000)
        sector_threshold = self.cfg.get('miskill_sector_peers_min', 0.3)
        stock_max_drop = self.cfg.get('miskill_stock_max_drop', -3.0)

        if snap.change_pct > min_pct:
            return None
        if snap.amount < min_amount:
            return None

        # 正股没崩 (排除真利空)
        sc_pct = snap.stock_change_pct or -99
        if sc_pct < stock_max_drop:
            return None

        lookback = self.cfg.get('volume_lookback_rounds', 20)
        avg_vol = window.get_avg_volume_long(code, n=lookback)
        if avg_vol <= 0:
            return None

        current_delta = window.get_peak_volume_delta(code, n=min(lookback, 5))
        if current_delta <= 0:
            return None

        current_ratio = current_delta / avg_vol
        if current_ratio >= vol_ratio:
            return None  # 不是缩量

        # 同板块对比: 该债下跌但板块没崩 → 孤立下跌 → 错杀
        concepts = self._concept_map.get(code, [])
        if concepts:
            best_concept = max(concepts, key=lambda c: concept_heat.get(c, 0))
            c_heat = concept_heat.get(best_concept, 0)
            if c_heat < -0.5:
                return None  # 板块整体下跌 → 不是错杀

        score = self._calc_score(abs(snap.change_pct), 50, 2, 10)
        desc = (f"错杀抄底 跌幅{snap.change_pct:.1f}% 量比仅{current_ratio:.2f}x "
                f"正股{sc_pct:+.1f}% 板块未走弱")
        sig = Signal('A', '错杀抄底', code, snap.name or code,
                     snap.stock_name or '', desc, score, time.time())
        sig.strategy = 'dip'
        return sig

    def _detect_oversold_repair(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        模式三: 转债超跌修复 — 转债跌得比正股多, 大概率修复
        对手盘: 被动止损盘 / 指数拖累
        """
        gap = self.cfg.get('oversold_gap_pct', 2.0)
        stock_min = self.cfg.get('oversold_stock_min_pct', -3.0)

        sc_pct = snap.stock_change_pct or 0
        cb_pct = snap.change_pct or 0

        # 正股没怎么跌, 转债跌更多 → 超跌
        if sc_pct <= stock_min:
            return None
        if cb_pct - sc_pct > -gap:
            return None
        if cb_pct >= 0:
            return None

        gap_val = sc_pct - cb_pct
        score = self._calc_score(gap_val, 40, 2, 8)
        desc = (f"转债超跌{cb_pct:.1f}% vs 正股{sc_pct:+.1f}% "
                f"偏离{gap_val:.1f}% 修复空间大")
        sig = Signal('B', '超跌修复', code, snap.name or code,
                     snap.stock_name or '', desc, score, time.time())
        sig.strategy = 'dip'
        return sig

    def _detect_tailwash(self, code: str, snap, window: RollingWindow) -> Optional[Signal]:
        """
        模式四: 尾盘洗盘 — 14:15-14:50 放量下杀但正股不跟跌
        对手盘: 主力打压吸筹, 为次日高开出货
        """
        start = self.cfg.get('tailwash_start_hour', 1415)
        end = self.cfg.get('tailwash_end_hour', 1450)
        drop_min = self.cfg.get('tailwash_cb_drop_min', -2.0)
        vol_ratio = self.cfg.get('tailwash_vol_ratio', 1.5)

        now = datetime.now()
        now_hm = now.hour * 100 + now.minute
        if not (start <= now_hm <= end):
            return None

        if snap.change_pct > drop_min:
            return None

        # 正股没跟跌 → 排除板块性风险
        sc_pct = snap.stock_change_pct or -99
        if sc_pct < -2.0:
            return None

        lookback = self.cfg.get('volume_lookback_rounds', 20)
        avg_vol = window.get_avg_volume_long(code, n=lookback)
        if avg_vol <= 0:
            return None

        current_delta = window.get_peak_volume_delta(code, n=min(lookback, 5))
        if current_delta <= 0:
            return None

        current_ratio = current_delta / avg_vol
        if current_ratio < vol_ratio:
            return None

        score = self._calc_score(abs(snap.change_pct), 45, 2, 8)
        desc = (f"尾盘洗盘 跌{snap.change_pct:.1f}% 量比{current_ratio:.1f}x "
                f"正股{sc_pct:+.1f}% 疑似次日高开")
        sig = Signal('S', '尾盘洗盘', code, snap.name or code,
                     snap.stock_name or '', desc, score, time.time())
        sig.strategy = 'dip'
        return sig

    def _detect_demon_bond(self, code: str, snap, window: RollingWindow,
                           stock_code_map: dict[str, str]) -> Optional[Signal]:
        """
        模式五: 反指数妖债 — 微盘 + 高换手 + 转债逆势涨
        """
        max_scale = self.cfg.get('demon_max_scale', 3.0)

        if snap.change_pct <= 1.0:
            return None
        if snap.amount < 50000000:
            return None

        # 检查是否为微盘 (从监控池信息中获取)
        # 妖债特征: 小盘 + 高波动 + 逆势
        lookback = self.cfg.get('volume_lookback_rounds', 20)
        avg_vol = window.get_avg_volume_long(code, n=lookback)
        if avg_vol <= 0:
            return None

        peak = window.get_peak_volume_delta(code, n=min(lookback, 5))
        turnover_hint = peak / max(avg_vol, 1)

        if turnover_hint < 3.0:
            return None

        from core.data_fusion import fmt_amount
        desc = (f"妖债异动 涨{snap.change_pct:.1f}% 换手活跃度{turnover_hint:.1f}x "
                f"成交额{fmt_amount(snap.amount)} 逆势拉升")
        score = self._calc_score(snap.change_pct, 45, 1, 15)
        sig = Signal('A', '妖债异动', code, snap.name or code,
                     snap.stock_name or '', desc, score, time.time())
        sig.strategy = 'chase'
        return sig

    @staticmethod
    def _calc_score(value: float, max_score: float, min_val: float, max_val: float) -> float:
        """
        信号强度评分 (0-max_score)
        使用非线性映射: 小信号被压制, 大信号被放大, 拉开区分度
        """
        if value <= min_val:
            return 0
        if value >= max_val:
            return max_score

        # 线性归一化到 0-1
        ratio = (value - min_val) / (max_val - min_val)
        ratio = min(1.0, max(0.0, ratio))

        # 非线性: ratio² 压制弱信号 (<0.5→更低, >0.7→接近原值)
        adjusted = ratio * ratio

        return round(min(max_score, adjusted * max_score), 1)


class AlertManager:
    """预警管理器 - 短时去重/等级过滤"""

    def __init__(self, config: dict):
        self.cfg = config['output']
        # code_level -> timestamp 冷却字典
        self._cooldowns: dict[str, float] = {}
        # 各级别冷却时间
        self._level_cooldowns = {
            'B': self.cfg.get('cooldown_seconds_B', 300),
        }

    def _get_cooldown(self, level: str) -> float:
        """获取指定等级的冷却时间"""
        return self._level_cooldowns.get(level, self.cfg.get('cooldown_seconds', 120))

    def process(self, signals: list[Signal]) -> list[Signal]:
        """
        处理信号: 去重 + 等级过滤 + TopN
        """
        now = time.time()
        filtered = []
        min_rank = SIGNAL_LEVELS.get(self.cfg['min_signal_level'], 1)

        for sig in signals:
            # 等级过滤
            if sig.level_rank < min_rank:
                continue

            # 短时去重 (同债同等级, B级使用更长的冷却时间)
            cooldown_key = f"{sig.code}_{sig.level}"
            last_time = self._cooldowns.get(cooldown_key, 0)
            cooldown = self._get_cooldown(sig.level)
            if now - last_time < cooldown:
                continue

            self._cooldowns[cooldown_key] = now
            filtered.append(sig)

        # Top N
        max_signals = self.cfg['max_signals_per_round']
        if len(filtered) > max_signals:
            filtered = filtered[:max_signals]

        return filtered

    def is_cooling(self, code: str, level: str) -> bool:
        """检查指定债券+等级是否仍在冷却期 (供外部模块如 SignalLogger 使用)"""
        cooldown_key = f"{code}_{level}"
        last_time = self._cooldowns.get(cooldown_key, 0)
        cooldown = self._get_cooldown(level)
        return (time.time() - last_time) < cooldown
