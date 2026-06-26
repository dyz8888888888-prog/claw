"""
输出格式化 - OutputFormatter

终端动态刷新信号面板
"""

import time
import logging
from typing import Optional

from config import CONFIG
from core.signal_engine import Signal
from output.colors import (
    RESET, BOLD, DIM,
    RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA, WHITE, GRAY,
    colored_level, colored_pct, colored_trade
)

logger = logging.getLogger(__name__)


class OutputFormatter:
    """输出格式化"""

    @staticmethod
    def clear_screen():
        """清屏"""
        print('\033[2J\033[H', end='', flush=True)

    @staticmethod
    def print_header(now_str: str, total_bonds: int, monitored: int,
                     fetch_cost: float, signal_count: int):
        """打印标题栏"""
        line = "━" * 60
        fetch_info = f"抓取:{fetch_cost:.2f}s" if fetch_cost > 0 else ""
        print(f"\n{BLUE}━━━ 可转债日内联动监控 {RESET} {BOLD}{now_str}{RESET}  "
              f"| 池:{total_bonds}只/选:{monitored}只  |  {fetch_info}  |  信号:{signal_count}个  "
              f"{BLUE}{line}{RESET}")
        print()

    @staticmethod
    def print_signals(signals: list[Signal], snapshots: dict, window, monitor_list: list = None):
        """打印信号列表"""
        if not signals:
            print(f"  {GRAY}暂无信号{RESET}")
            return

        # 表头
        header = f"  {'No':>3} │ {'等级':>3} │ {'代码':>6} │ {'名称':<8} │ {'现价':>8} │ {'涨跌幅':>8} │ {'信号':<12} │ {'详情'}"
        print(f"  {DIM}{'─' * 75}{RESET}")
        print(f"  {header}")
        print(f"  {DIM}{'─' * 75}{RESET}")

        for i, sig in enumerate(signals, 1):
            snap = snapshots.get(sig.code) if snapshots else None
            trade_str = f"{snap.trade:.2f}" if snap and snap.trade else "-"
            pct_str = colored_pct(snap.change_pct) if snap and snap.change_pct is not None else "-"
            level_str = colored_level(sig.level)

            print(f"  {i:>3} │ {level_str} │ {sig.code:>6} │ {sig.name:<8} │ {trade_str:>8} │ {pct_str:>14} │ {sig.signal_type:<12} │ {sig.description}")

        print(f"  {DIM}{'─' * 75}{RESET}")

    @staticmethod
    def print_summary(snapshots: dict, monitor_list: list):
        """打印概览"""
        if not snapshots:
            return

        surge_count = 0
        drop_count = 0
        limit_up_count = 0
        tdx_stock_count = 0

        for code, snap in snapshots.items():
            if snap.change_pct is not None:
                if snap.change_pct > 2:
                    surge_count += 1
                elif snap.change_pct < -2:
                    drop_count += 1
                if snap.stock_change_pct is not None:
                    tdx_stock_count += 1
                    if snap.stock_change_pct >= 9.5:
                        limit_up_count += 1

        summary = (
            f"  {DIM}[持仓速览]{RESET}  "
            f"监控{len(snapshots)}只  |  "
            f"涨幅>2%:{surge_count}  跌幅>2%:{drop_count}  |  "
            f"正股涨停:{limit_up_count}  |  "
            f"通达信追踪:{tdx_stock_count}只正股"
        )
        print(f"  {summary}")

        # 强赎提醒
        redeem_warnings = []
        for item in (monitor_list or []):
            status = item.get('redeem_status', '')
            code = item.get('code_num', '')
            name = item.get('name', '')
            if status == '已公告强赎':
                redeem_warnings.append(f"{RED}●{RESET} {code} {name}")
            elif status == '公告要强赎':
                redeem_warnings.append(f"{YELLOW}▲{RESET} {code} {name}")
        if redeem_warnings:
            print(f"  {DIM}[强赎预警]{RESET}  {' '.join(redeem_warnings)}")

        print(f"  {BLUE}{'━' * 60}{RESET}")

    @staticmethod
    def render_frame(now_str: str, total_bonds: int, monitored: int,
                     snapshots: dict, signals: list[Signal], window,
                     fetch_cost: float, monitor_list: list):
        """渲染一帧完整面板"""
        OutputFormatter.clear_screen()
        OutputFormatter.print_header(now_str, total_bonds, monitored,
                                     fetch_cost, len(signals))
        OutputFormatter.print_signals(signals, snapshots, window, monitor_list)
        OutputFormatter.print_summary(snapshots, monitor_list)
