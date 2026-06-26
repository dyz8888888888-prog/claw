"""
全系统稳定枚举 — 禁止在别处使用裸字符串。

用法: from cb_monitor.domain.enums import Regime, TradeMode, ...
"""

from enum import StrEnum


class Regime(StrEnum):
    """市场环境"""
    OVERHEAT = "overheat"   # 高潮 — 涨停强、晋级强、破板低
    ACTIVE = "active"       # 发酵 — 强但未极端
    MILD = "mild"           # 温和 — 中性偏可做
    EBB = "ebb"             # 退潮 — 赚钱效应衰退
    FREEZE = "freeze"       # 冰点 — 几乎无机会


class TradeMode(StrEnum):
    """今日交易模式"""
    ATTACK = "attack"       # 进攻 — 3笔、隔夜允许
    PROBE = "probe"         # 试错 — 2笔、不隔夜
    DEFENSE = "defense"     # 防守 — 1笔、日内平仓
    DISABLED = "disabled"   # 暂停 — 0笔


class HoldingMode(StrEnum):
    """持仓模式"""
    INTRADAY_FLAT = "intraday_flat"      # 当日必须平仓
    OVERNIGHT_CARRY = "overnight_carry"  # 可隔夜


class Decision(StrEnum):
    """策略决策"""
    NO_TRADE = "no_trade"    # 无机会
    WATCH = "watch"          # 持续观察
    ENTER = "enter"          # 进场
    HOLD = "hold"            # 继续持有
    REDUCE = "reduce"        # 减仓
    EXIT = "exit"            # 全部退出


class MachineState(StrEnum):
    """交易状态机状态"""
    DISABLED = "disabled"    # 今日不交易
    IDLE = "idle"            # 空仓待命
    WATCHING = "watching"    # 候选观察中
    ENTERING = "entering"    # 进场执行中
    HOLDING = "holding"      # 持仓中
    EXITING = "exiting"      # 离场执行中
    COOLDOWN = "cooldown"    # 冷却期
    DONE = "done"            # 今日停止


class RiskCheck(StrEnum):
    """风控检查结果"""
    ALLOW = "allow"
    DENY = "deny"
    COOLDOWN = "cooldown"
