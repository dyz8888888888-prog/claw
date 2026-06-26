#!/usr/bin/env python3.12
"""
可转债日内联动监控 - 主入口

用法:
    python3.12 main.py              # 启动监控 (交易时段自动运行)
    python3.12 main.py --test       # 单轮测试 (验证数据获取和信号计算)
    python3.12 main.py --debug      # 启动监控 (开启调试日志)
"""

import sys
import time
import signal
import logging
import logging.handlers
import argparse
import threading

# 确保项目根目录在路径中
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG
from core.bond_selector import BondSelector
from core.data_fusion import DataFusion
from core.signal_engine import SignalEngine, AlertManager
from scheduler.rolling_window import RollingWindow
from output.formatter import OutputFormatter
from output.colors import RESET, GREEN, RED, YELLOW, CYAN, BOLD, DIM
from output.signal_logger import SignalLogger
from output.daily_stats import DailyStats


def setup_logging(debug: bool = False):
    """配置日志 (控制台 + 文件轮转)"""
    level = logging.DEBUG if debug else logging.INFO
    fmt = '%(asctime)s [%(name)s] %(levelname)s: %(message)s'
    formatter = logging.Formatter(fmt, datefmt='%H:%M:%S')

    # 控制台输出
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)

    # 文件轮转 (10MB/文件, 保留5个备份)
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'monitor.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 避免 basicConfig 重复添加
    root.addHandler(console)
    root.addHandler(file_handler)


def run_test():
    """单轮测试模式 - 验证数据获取和信号计算"""
    print(f"\n{CYAN}━━━ 可转债日内联动监控 - 测试模式{RESET}\n")

    # 1. 加载选债池
    print(f"{YELLOW}[1/4]{RESET} 加载选债池...")
    selector = BondSelector(CONFIG)
    monitor_list = selector.get_monitor_list()
    total = len(selector._cov_pool) if selector._cov_pool is not None else 0
    print(f"  → 总池 {total} 只, 筛选后 {len(monitor_list)} 只\n")

    # 2. 获取实时行情
    print(f"{YELLOW}[2/4]{RESET} 获取实时行情...")
    fusion = DataFusion(monitor_list)
    t0 = time.time()
    snapshots = fusion.merge()
    cost = time.time() - t0
    if snapshots:
        print(f"  → 获取 {len(snapshots)} 只快照, 耗时 {cost:.2f}s")
        # 打印前5只
        for i, (code, snap) in enumerate(list(snapshots.items())[:5]):
            stock_info = f" | 正股:{snap.stock_change_pct:+.2f}%" if snap.stock_change_pct is not None else ""
            print(f"  {i+1}. {code} {snap.trade:.2f} {snap.change_pct:+.2f}% vol:{snap.volume}{stock_info}")
        print()
    else:
        print(f"  {RED}× 无数据返回{RESET}\n")
        return

    # 3. 填充滚动窗口 (模拟几轮数据)
    print(f"{YELLOW}[3/4]{RESET} 填充滚动窗口...")
    window = RollingWindow(max_window=60)
    for _ in range(6):
        for code, snap in snapshots.items():
            window.push(code, snap)
    print(f"  → 已填充60只 × 6轮快照\n")

    # 4. 计算信号
    print(f"{YELLOW}[4/4]{RESET} 计算信号...")
    engine = SignalEngine(CONFIG)
    alert_mgr = AlertManager(CONFIG)
    raw_signals = engine.analyze(snapshots, window, monitor_list)
    signals = alert_mgr.process(raw_signals)

    if signals:
        print(f"  → 原始信号 {len(raw_signals)} 个, 过滤后 {len(signals)} 个\n")
    else:
        print(f"  → 暂无信号\n")

    # 日志+统计 (测试模式也记录)
    signal_logger = SignalLogger(CONFIG)
    signal_logger.write_batch(signals, snapshots)
    daily_stats = DailyStats(CONFIG)
    daily_stats.record_batch(signals)

    # 输出结果
    print(f"{CYAN}━━━ 测试结果输出{RESET}\n")
    OutputFormatter.render_frame(
        now_str=time.strftime('%H:%M:%S'),
        total_bonds=total,
        monitored=len(monitor_list),
        snapshots=snapshots,
        signals=signals,
        window=window,
        fetch_cost=cost,
        monitor_list=monitor_list,
    )

    stats_summary = daily_stats.summary
    if stats_summary:
        print(f"  {DIM}{stats_summary}{RESET}")

    print(f"\n{GREEN}测试完成{RESET} (共 {total} 只转债, 监控 {len(monitor_list)} 只, 信号 {len(signals)} 个)\n")


def run_monitor():
    """启动持续监控"""
    from scheduler.scheduler import Scheduler
    from dashboard.server import start_server, _cleanup_tdx

    # 优雅关闭: 只在主线程注册 signal (子线程中会报 ValueError)
    def _on_shutdown(sig, frame):
        print(f"\n  {YELLOW}收到终止信号, 正在清理...{RESET}")
        _cleanup_tdx()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    # 启动仪表盘 Web 服务 (后台线程)
    dash_host = CONFIG.get('dashboard', {}).get('host', None) or os.environ.get('DASHBOARD_HOST', '0.0.0.0')
    dash_port = CONFIG.get('dashboard', {}).get('port', 5000)
    flask_thread = threading.Thread(
        target=start_server,
        kwargs={'host': dash_host, 'port': dash_port},
        daemon=True,
        name='DashboardServer'
    )
    flask_thread.start()
    local_ip = _get_local_ip()
    print(f"\n{CYAN}━━━ 可转债日内联动监控 启动{RESET}")
    print(f"  {DIM}数据源: 通达信行情 + 东方财富转债池(转股价/溢价率){RESET}")
    print(f"  {DIM}交易时段: 9:30-11:30 / 13:00-15:00 (工作日){RESET}")
    print(f"  {DIM}信号等级: {CONFIG['output']['min_signal_level']}级及以上{RESET}")
    print(f"  {DIM}发行规模上限: {CONFIG['selector']['max_issue_scale']}亿{RESET}")
    print(f"  {DIM}溢价率范围: {CONFIG['selector']['min_premium_ratio']}%~{CONFIG['selector']['max_premium_ratio']}%{RESET}")
    print(f"  {DIM}轮询间隔: {CONFIG['spot_interval']}秒/次{RESET}")
    print(f"  {DIM}仪表盘: http://{local_ip}:{dash_port}{RESET}")
    print(f"  {DIM}按 Ctrl+C 停止{RESET}\n")

    scheduler = Scheduler()
    try:
        scheduler.run()
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}用户中断{RESET}")
    finally:
        scheduler.stop()


def _get_local_ip() -> str:
    """获取本机局域网IP"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def main():
    parser = argparse.ArgumentParser(description='可转债日内联动监控')
    parser.add_argument('--test', action='store_true', help='单轮测试模式')
    parser.add_argument('--debug', action='store_true', help='开启调试日志')
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    if args.test:
        run_test()
    else:
        run_monitor()


if __name__ == '__main__':
    main()
