# -*- coding: utf-8 -*-
# 可转债日内联动监控 - 配置文件 (dataclass 类型安全版)
# 所有可调参数集中管理, IDE 自动补全
# 同时支持 [] 访问和 .get() 方法, 向后兼容旧代码

import os
from dataclasses import dataclass, field

# 加载 .env 中的环境变量 (API Key 等敏感信息)
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path)
except ImportError:
    pass


class _ConfigBase:
    """Mixin: 让 dataclass 支持 dict-like 访问, 向后兼容"""

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, key):
        return hasattr(self, key)


# ============================================================
# 子配置
# ============================================================

@dataclass
class SelectorConfig(_ConfigBase):
    """选债条件"""
    min_premium_ratio: float = -5.0   # 最小转股溢价率 (%)
    max_premium_ratio: float = 35.0   # 最大转股溢价率 (%)
    max_issue_scale: float = 10.0     # 最大发行规模 (亿)


@dataclass
class SignalConfig(_ConfigBase):
    """信号阈值"""
    price_surge_min_delta: float = 0.003    # 每轮涨幅阈值 (0.3%)
    price_surge_rounds: int = 3              # 连续检测轮数
    volume_multiplier: float = 2.5           # 放量倍率
    volume_lookback_rounds: int = 20         # 放量基线回溯轮数
    volume_min_peak: int = 500               # 最小峰值成交量 (手)
    volume_min_amount: int = 30000000        # 最小成交额 (元)
    min_trade_amount: int = 1000000          # 最小成交额过滤 (元)
    open_warmup_seconds: int = 300           # 早盘预热
    afternoon_warmup_seconds: int = 60       # 午盘预热
    stock_limit_up: float = 9.8              # 正股涨停阈值 - 主板 (%)
    stock_limit_up_20: float = 19.5          # 正股涨停阈值 - 20cm (%)
    s_signal_max_change: float = 5.0         # S信号转债涨幅上限
    b_divergence_max_change: float = 1.0     # B偏离转债涨幅上限
    b_hold_max_change: float = -1.0          # B抗跌转债跌幅下限
    linkage_gap: float = 5.0                 # 股债联动偏差阈值
    premium_shift_threshold: float = 3.0     # 溢价率突变阈值
    breakout_percentile: float = 0.95        # 突破分位数
    stock_surge_threshold: float = 3.0       # 正股大涨阈值
    stock_plunge_threshold: float = -3.0     # 正股大跌阈值
    signal_score_exponent: float = 2.0       # 评分非线性指数
    lowvol_plunge_min_pct: float = -2.0      # 缩量急跌最小跌幅
    lowvol_plunge_vol_ratio: float = 0.5     # 缩量倍率
    lowvol_plunge_amount_min: int = 5000000  # 缩量急跌最小成交额
    concept_weight_enabled: bool = True       # 概念热度加权
    concept_resonance_threshold: float = 2.0  # 板块共振阈
    concept_lonewolf_threshold: float = 0.5   # 孤狼阈
    concept_map_path: str = os.environ.get('CONCEPT_MAP_PATH', 
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                     '..', '可转债日报', 'cb_concept_map.json'))
    sector_limit_up_threshold: float = 9.5
    sector_limit_up_20: float = 19.5
    diffusion_dragon_stock_min: float = 5.0     # 降低阈值, 温和市也能触发 (原7.0)
    diffusion_dragon_stock_limit: float = 9.8
    diffusion_follower_max_cb: float = 2.0
    diffusion_follower_min_stock: float = 1.0
    # 信号置信度升级阈值
    confidence_stock_surge: float = 3.0          # 正股大涨 → 修正信号
    confidence_bond_lag_max: float = 1.0          # 转债滞涨上限
    confidence_mainline_boost: int = 1             # 主线概念升一级 (B→A, A→S)
    # 信号新鲜度窗口
    signal_freshness_seconds: int = 600            # 同债10分钟内只保留最强信号
    # B级日上限
    max_B_signals_per_day: int = 20                # B级超限后只推送A/S
    miskill_sector_peers_min: float = 0.3
    miskill_stock_max_drop: float = -3.0
    oversold_gap_pct: float = 2.0
    oversold_stock_min_pct: float = -3.0
    tailwash_start_hour: int = 1415
    tailwash_end_hour: int = 1450
    tailwash_cb_drop_min: float = -2.0
    tailwash_vol_ratio: float = 1.5
    demon_max_scale: float = 3.0
    demon_min_turnover: float = 0.05


@dataclass
class FilterConfig(_ConfigBase):
    """不走的路过滤器"""
    cb_max_surge_pct: float = 8.0
    max_chase_premium: float = 50.0
    seal_decay_threshold: float = 0.5
    overnight_premium_warn: float = 40.0
    overnight_warn_hour: int = 1445
    enabled: bool = True


@dataclass
class StrategyTypeConfig(_ConfigBase):
    """单策略配置"""
    label: str = ""
    stop_loss_pct: float = -2.0
    take_profit_pct: float = 3.0
    cooldown_seconds: int = 120


@dataclass
class StrategyConfig(_ConfigBase):
    """策略配置"""
    chase: StrategyTypeConfig = field(default_factory=lambda: StrategyTypeConfig(
        label='追涨', stop_loss_pct=-2.0, take_profit_pct=3.0, cooldown_seconds=120))
    dip: StrategyTypeConfig = field(default_factory=lambda: StrategyTypeConfig(
        label='回落', stop_loss_pct=-3.0, take_profit_pct=2.0, cooldown_seconds=300))


@dataclass
class OutputConfig(_ConfigBase):
    """输出配置"""
    min_signal_level: str = 'B'
    max_signals_per_round: int = 30
    cooldown_seconds: int = 120
    cooldown_seconds_B: int = 300


@dataclass
class LogConfig(_ConfigBase):
    """日志配置"""
    signal_log_enabled: bool = True
    signal_log_dir: str = 'logs'


@dataclass
class NotifyConfig(_ConfigBase):
    """推送通知"""
    enabled: bool = False
    provider: str = 'feishu'
    feishu_webhook: str = field(default_factory=lambda: os.environ.get('FEISHU_WEBHOOK', ''))
    serverchan_key: str = ''
    min_level: str = 'B'


@dataclass
class StatsConfig(_ConfigBase):
    """盘中统计"""
    enabled: bool = True
    max_history: int = 500


@dataclass
class TestConfig(_ConfigBase):
    """测试模式"""
    enable: bool = False
    single_round: bool = False


@dataclass
class DashboardConfig(_ConfigBase):
    """仪表盘"""
    port: int = 5000


@dataclass
class ExternalAPIConfig(_ConfigBase):
    """外部 API 密钥"""
    fuyao_api_key: str = field(default_factory=lambda: os.environ.get("FUYAO_API_KEY", ""))
    fuyao_base_url: str = "https://fuyao.aicubes.cn"


# ============================================================
# 主配置
# ============================================================

@dataclass
class MonitorConfig(_ConfigBase):
    """可转债日内联动监控 - 主配置"""
    spot_interval: int = 3                        # 轮询间隔 (秒)
    cov_refresh_interval: int = 86400              # 全量池刷新间隔 (秒, 24h)

    selector: SelectorConfig = field(default_factory=SelectorConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    log: LogConfig = field(default_factory=LogConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    test: TestConfig = field(default_factory=TestConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    ext_api: ExternalAPIConfig = field(default_factory=ExternalAPIConfig)


# 全局配置实例
CONFIG = MonitorConfig()

# 信号等级定义
SIGNAL_LEVELS = {'S': 5, 'A': 4, 'B': 3, 'C': 2, 'D': 1}
