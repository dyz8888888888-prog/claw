"""
策略总调度 — 7个交易窗口自动触发
启动: python scripts/strategy_runner.py [--window 0940|1030|1440]
"""
import os, sys, time, argparse
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from live_data import LiveData
from strategy_1030 import run_1030, print_signal as print_1030
from strategy_0940 import run_0940, print_signal as print_0940
from strategy_1440 import run_1440, print_signal as print_1440


def wait_until(target_time: str) -> str:
    """等待到目标时间"""
    now = datetime.now()
    target = datetime.strptime(target_time, '%H:%M').replace(
        year=now.year, month=now.month, day=now.day)
    if target < now:
        target_str = target.strftime('%H:%M:%S')
        now_str = now.strftime('%H:%M:%S')
        print(f'  [{target_str} 已过(当前{now_str})]')
        return None
    wait_sec = (target - now).total_seconds()
    if wait_sec > 0:
        print(f'  等待 {int(wait_sec)}s 到 {target_time}...')
        time.sleep(wait_sec)
    return datetime.now().strftime('%H:%M:%S')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--window', type=str, default=None,
                        help='单窗口模式: 0940, 1030, 1440')
    args = parser.parse_args()

    single_window = args.window
    if single_window and single_window not in ('0940', '1030', '1440'):
        print(f'无效窗口: {single_window}')
        return

    print('=' * 60)
    if single_window:
        print(f'  可转债策略引擎 — 单窗口模式 {single_window}')
    else:
        print('  可转债策略引擎 — 7窗口自动调度')
    print(f'  启动: {datetime.now().strftime("%H:%M:%S")}')
    print('=' * 60)

    # 日报文件
    today = datetime.now().strftime('%Y-%m-%d')
    report_path = os.path.join(os.path.dirname(SCRIPT_DIR),
                               f'报告_策略引擎_{today}.md')
    report_lines = [
        f'# 可转债策略引擎日报 — {today}',
        '',
        f'启动: {datetime.now().strftime("%H:%M:%S")}',
        '',
    ]

    def append_report(section_title, signals, limit=10):
        report_lines.append(f'## {section_title}')
        report_lines.append('')
        if not signals:
            report_lines.append('*无信号*')
        else:
            report_lines.append('| # | 转债 | 正股% | CB% | 溢价% | 规模 | 得分 | 概念 |')
            report_lines.append('|---|------|:----:|:---:|:-----:|:----:|:---:|------|')
            for i, item in enumerate(signals[:limit], 1):
                if len(item) == 3:
                    cb, score, concepts = item
                else:
                    cb, score = item
                    concepts = ''
                nm = cb.name or cb.code
                report_lines.append(
                    f'| {i} | {nm} | {cb.stock_pct:+.1f}% | {cb.pct_chg:+.1f}% | '
                    f'{cb.premium:.0f}% | {cb.scale:.1f}亿 | {score:.0f} | {concepts} |')
        report_lines.append('')
        # 保存中间状态
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))

    print()
    print('窗口列表:')
    print('  9:31 开盘秒冲 (手快)')
    print('  9:40 题材热度补涨 ← 策略二')
    print('  10:00 方向确认')
    print('  10:30 大盘情绪回升 ← 策略一')
    print('  11:10 午盘偷跑')
    print('  13:00 午间消息')
    print('  14:00 尾盘前回调')
    print('  14:40 尾盘吸筹 ← 策略三')
    print()

    # 预热数据层（一次性加载溢价/规模/正股）
    script_path = os.path.join(SCRIPT_DIR, 'strategy_runner.py')
    data = LiveData(script_path)

    if single_window:
        # 单窗口模式：跳过所有 wait_until
        run_single_window(data, single_window, append_report, report_path)
        data.close()
        return

    # === 全窗口模式（原逻辑） ===

    # === 9:31 ===
    if wait_until('09:31'):
        print(f'[1/7] 9:31 开盘秒冲 — 待开发')
    print()

    # === 9:40 ===
    if wait_until('09:40'):
        print(f'[2/7] 9:40 题材热度补涨')
        sig_0940 = run_0940(data)
        print_0940(sig_0940)
        append_report('9:40 题材热度补涨', sig_0940)
    else:
        sig_0940 = []
    print()

    # === 10:00 ===
    if wait_until('10:00'):
        print(f'[3/7] 10:00 方向确认 — 待开发')
    print()

    # === 10:30 ===
    if wait_until('10:30'):
        print(f'[4/7] 10:30 大盘情绪回升')
        sig_1030 = run_1030(data)
        print_1030(sig_1030)
        append_report('10:30 大盘情绪回升', sig_1030)
    print()

    # === 11:10 ===
    if wait_until('11:10'):
        print(f'[5/7] 11:10 午盘偷跑 — 待开发')
    print()

    # === 13:00 ===
    if wait_until('13:00'):
        print(f'[6/7] 13:00 午间消息 — 待开发')
    print()

    # === 14:00 ===
    if wait_until('14:00'):
        print(f'[7/7] 14:00 尾盘前回调 — 待开发')
    print()

    # === 14:40 ===
    if wait_until('14:40'):
        print(f'[8/8] 14:40 尾盘吸筹')
        prev = data.scan()
        time.sleep(10)
        sig_1440 = run_1440(data, prev)
        print_1440(sig_1440)
        append_report('14:40 尾盘吸筹', sig_1440)

    data.close()
    report_lines.append(f'\n---\n*生成: {datetime.now().strftime("%H:%M:%S")} | 数据: 通达信TQ*')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    print(f'\n 日报已保存: {report_path}')
    print(f'\n✓ 全天完毕 ({datetime.now().strftime("%H:%M:%S")})')


    return


def run_single_window(data, window, append_report, report_path):
    """单窗口模式：只运行指定窗口，不等待，完成后写入日报"""
    now = datetime.now()
    print(f'\n[{window}] 运行中...')

    if window == '0940':
        sig = run_0940(data)
        print_0940(sig)
        append_report('9:40 题材热度补涨', sig)
    elif window == '1030':
        sig = run_1030(data)
        print_1030(sig)
        append_report('10:30 大盘情绪回升', sig)
    elif window == '1440':
        prev = data.scan()
        time.sleep(10)
        sig = run_1440(data, prev)
        print_1440(sig)
        append_report('14:40 尾盘吸筹', sig)

    # append_report 已写入完整日报，追加页脚
    with open(report_path, 'a', encoding='utf-8') as f:
        f.write(f'\n---\n*{window}窗口 | 生成: {datetime.now().strftime("%H:%M:%S")} | 数据: 通达信TQ*')
    print(f'\n✓ 完成 ({datetime.now().strftime("%H:%M:%S")})')


if __name__ == '__main__':
    main()
