"""
共享状态模块 - DashboardState

线程安全的内存状态存储，Scheduler 写入，Flask API 读取。
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class DashboardState:
    """线程安全的仪表盘共享状态 (读写分离锁)"""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # === 本轮数据 ===
    snapshots: dict = field(default_factory=dict)          # code -> Snapshot
    signals: list = field(default_factory=list)             # [Signal, ...]
    total_bonds: int = 0
    monitored: int = 0
    fetch_cost: float = 0.0
    cycle_cost: float = 0.0   # 主循环总耗时(秒)
    market_state: dict = field(default_factory=dict)  # 市场状态
    last_update: str = ""
    is_trading: bool = False

    # === 盘中统计 ===
    surge_count: int = 0      # 涨幅 >2%
    drop_count: int = 0       # 跌幅 >2%
    limit_up_count: int = 0   # 正股涨停

    # === 今日累计信号 ===
    signal_history: list = field(default_factory=list)      # [{level, type, code, name, desc, score, time}, ...]

    # === 强赎预警 ===
    redeem_warnings: list = field(default_factory=list)     # [{code, name, status}, ...]

    # === 今日统计摘要 ===
    count_by_level: dict = field(default_factory=dict)      # {'S': 0, 'A': 3, ...}
    today_stats: str = ""

    # === 强赎映射 ===
    redeem_map: dict = field(default_factory=dict)          # code_num → redeem_status

    # === Fuyao 数据缓存 (解决 HTTP 429 限流) ===
    fuyao_pool_cache: dict = field(default_factory=dict)    # limit_up_pool 缓存: {items: [...], ts: 0, error: None}
    fuyao_ladder_cache: dict = field(default_factory=dict)  # limit_up_ladder 缓存: {data: [...], ts: 0, error: None}
    fuyao_cache_ttl: float = 5.0   # 缓存有效期 (秒), 调度器每5s刷新

    # === 信号触发后跟踪 ===
    # key: "code_level_time" → {code, name, level, type, trigger_price, trigger_time, peak_price, current_pnl}
    pending_signals: dict = field(default_factory=dict)
    max_pending: int = 20   # 最多保留 N 条待跟踪

    def update_cycle(self, **kwargs):
        """调度器每轮调用，更新本轮数据"""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            # Ensure total_bonds is always set (belt-and-suspenders)
            if 'total_bonds' in kwargs:
                self.total_bonds = kwargs['total_bonds']

    def add_signal_history(self, sig):
        """添加一条信号到历史 (去重)"""
        with self._lock:
            # 简单去重: 同债同等级2分钟内不重复
            key = f"{sig.code}_{sig.level}"
            now = time.time()
            recent = [s for s in self.signal_history
                      if s.get('_key') == key and now - s.get('_ts', 0) < 120]
            if recent:
                # 已有，更新评分取最高
                existing = recent[0]
                if sig.score > existing.get('score', 0):
                    existing.update({
                        'score': sig.score,
                        'desc': sig.description,
                        'time': time.strftime('%H:%M:%S', time.localtime(sig.timestamp)),
                        '_ts': now,
                    })
                return

            self.signal_history.append({
                '_key': key,
                '_ts': now,
                'level': sig.level,
                'type': sig.signal_type,
                'code': sig.code,
                'name': sig.name,
                'desc': sig.description,
                'score': sig.score,
                'time': time.strftime('%H:%M:%S', time.localtime(sig.timestamp)),
            })

            # 最多保留 200 条
            if len(self.signal_history) > 200:
                self.signal_history = self.signal_history[-200:]

    def add_redeem_warning(self, code: str, name: str, status: str):
        """添加强赎预警"""
        with self._lock:
            existing = [r for r in self.redeem_warnings if r['code'] == code]
            if existing:
                existing[0]['status'] = status
            else:
                self.redeem_warnings.append({'code': code, 'name': name, 'status': status})

    def clear_redeem_warnings(self):
        with self._lock:
            self.redeem_warnings.clear()

    def track_signal(self, sig, trigger_price: float):
        """信号触发时记录基准价，用于跟踪触发后涨跌幅"""
        with self._lock:
            key = f"{sig.code}_{sig.level}_{int(sig.timestamp)}"
            now_ts = time.time()
            self.pending_signals[key] = {
                'code': sig.code,
                'name': sig.name,
                'level': sig.level,
                'type': sig.signal_type,
                'trigger_price': round(trigger_price, 2),
                'trigger_time': time.strftime('%H:%M:%S', time.localtime(sig.timestamp)),
                'peak_price': round(trigger_price, 2),
                'current_pnl': 0.0,
                '_ts': now_ts,
            }
            # 限制数量, 删最旧的
            if len(self.pending_signals) > self.max_pending:
                oldest = sorted(self.pending_signals.items(), key=lambda x: x[1].get('_ts', 0))[0][0]
                del self.pending_signals[oldest]

    def update_pending(self, snapshots: dict):
        """每轮用最新快照更新 pending 信号的峰值和当前盈亏"""
        with self._lock:
            to_del = []
            now_ts = time.time()
            for key, ps in self.pending_signals.items():
                code = ps['code']
                snap = snapshots.get(code)
                if snap:
                    price = getattr(snap, 'trade', 0)
                    if price > 0:
                        ps['peak_price'] = round(max(ps['peak_price'], price), 2)
                        ps['current_pnl'] = round((price - ps['trigger_price']) / ps['trigger_price'] * 100, 2)
                # 超过30分钟自动清除
                if now_ts - ps.get('_ts', 0) > 1800:
                    to_del.append(key)
            for key in to_del:
                del self.pending_signals[key]

    def get_pending_top(self, n: int = 10) -> list:
        """取最近 N 条跟踪信号, 按触发时间降序"""
        with self._lock:
            items = sorted(self.pending_signals.values(), key=lambda x: x.get('_ts', 0), reverse=True)
            return items[:n]

    def update_fuyao_pool(self, items: list, ts: float = None, error: str = None):
        """更新 Fuyao 涨停池缓存 (由调度器调用)"""
        with self._lock:
            self.fuyao_pool_cache = {
                'items': items,
                'ts': ts or time.time(),
                'error': error,
            }

    def update_fuyao_ladder(self, data: dict, ts: float = None, error: str = None):
        """更新 Fuyao 连板天梯缓存 (由调度器调用)"""
        with self._lock:
            self.fuyao_ladder_cache = {
                'data': data,
                'ts': ts or time.time(),
                'error': error,
            }

    def get_fuyao_pool(self, max_age: float = None) -> dict:
        """获取 Fuyao 涨停池缓存 (API端点读取)
        
        Returns: {'items': [...], 'ts': float, 'fresh': bool}
        如果缓存不存在、过期、或跨日, items 为空列表
        """
        if max_age is None:
            max_age = self.fuyao_cache_ttl * 2  # 端点默认2倍TTL容忍
        with self._lock:
            cache = dict(self.fuyao_pool_cache)
        now = time.time()
        age = now - cache.get('ts', 0)
        # ── 跨日检测: 缓存时间戳不在今天 → 清空, 避免显示昨日数据 ──
        cache_date = time.strftime('%Y%m%d', time.localtime(cache.get('ts', 0)))
        today = time.strftime('%Y%m%d', time.localtime(now))
        if cache_date != today:
            cache['items'] = []
            cache['fresh'] = False
            cache['age'] = -1
            cache['_stale_reason'] = f'跨日(缓存{cache_date}≠今日{today})'
            return cache
        cache['fresh'] = (age < max_age and len(cache.get('items', [])) > 0)
        cache['age'] = round(age, 1)
        return cache

    def get_fuyao_ladder(self, max_age: float = None) -> dict:
        """获取 Fuyao 连板天梯缓存"""
        if max_age is None:
            max_age = self.fuyao_cache_ttl * 2
        with self._lock:
            cache = dict(self.fuyao_ladder_cache)
        now = time.time()
        age = now - cache.get('ts', 0)
        cache['fresh'] = (age < max_age and bool(cache.get('data')))
        cache['age'] = round(age, 1)
        return cache

    def to_dict(self) -> dict:
        """返回 JSON 安全的数据字典"""
        with self._lock:
            # 快照摘要 (只传 Top30 涨/跌)
            snap_list = []
            for code, snap in self.snapshots.items():
                snap_list.append({
                    'code': code,
                    'name': getattr(snap, 'name', ''),
                    'price': getattr(snap, 'trade', 0),
                    'pct': getattr(snap, 'change_pct', 0),
                    'stock_pct': getattr(snap, 'stock_change_pct'),
                    'premium': getattr(snap, 'premium_ratio'),
                    'volume': getattr(snap, 'volume', 0),
                    'amount': getattr(snap, 'amount', 0),
                })

            # 当前信号 (附加快照数据: 现价/涨跌幅/正股/溢价率)
            sig_list = []
            for sig in self.signals:
                snap = self.snapshots.get(sig.code)
                sig_list.append({
                    'level': sig.level,
                    'type': sig.signal_type,
                    'code': sig.code,
                    'name': sig.name,
                    'desc': sig.description,
                    'score': sig.score,
                    'time': time.strftime('%H:%M:%S', time.localtime(sig.timestamp)),
                    'price': getattr(snap, 'trade', 0) if snap else 0,
                    'pct': getattr(snap, 'change_pct', 0) if snap else 0,
                    'stock_pct': getattr(snap, 'stock_change_pct') if snap else None,
                    'premium': getattr(snap, 'premium_ratio') if snap else None,
                })

            return {
                'last_update': self.last_update,
                'is_trading': self.is_trading,
                'total_bonds': self.total_bonds,
                'monitored': self.monitored,
                'fetch_cost': round(self.fetch_cost, 2),
                'cycle_cost': round(self.cycle_cost, 2),
                'market_state': self.market_state,
                'snapshots': snap_list,
                'signals': sig_list,
                'signal_history': self.signal_history[-50:],  # 最近 50 条
                'surge_count': self.surge_count,
                'drop_count': self.drop_count,
                'limit_up_count': self.limit_up_count,
                'redeem_warnings': self.redeem_warnings,
                'count_by_level': self.count_by_level,
                'today_stats': self.today_stats,
            }


# 全局单例
state = DashboardState()
