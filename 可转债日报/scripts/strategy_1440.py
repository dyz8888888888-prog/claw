"""
策略三：14:40 尾盘吸筹
驱动: 尾盘放量 + 正股强势 + CB补位 = 次日高开套利
"""
import os, time
from live_data import LiveData, LiveCB
from typing import List, Tuple


def run_1440(data: LiveData, prev_scan: List[LiveCB] = None) -> List[Tuple[LiveCB, float]]:
    """
    14:40 尾盘吸筹

    条件:
    1. 正股 > 5%（强势确认）
    2. CB 14:30后涨幅加速（最后30分钟贡献 > 全天30%）
    3. CB溢价 < 20%
    4. 规模 < 5亿

    如果没有前行情快照（prev_scan），使用 last_tick 对比
    """
    cbs = data.scan()
    if not cbs:
        return []

    # 快速对比：与上一次扫描比涨幅变化
    if prev_scan:
        prev_map = {c.code: c for c in prev_scan}
    else:
        prev_map = {}

    candidates = []
    for cb in cbs:
        # 硬过滤
        if cb.stock_pct < 5:  # 正股不够强
            continue
        if cb.premium > 20 or cb.premium < -10:
            continue
        if cb.scale > 5:
            continue
        if cb.volume < 500000:
            continue

        # 末段加速判断
        if cb.code in prev_map:
            prev_cb = prev_map[cb.code]
            delta = cb.pct_chg - prev_cb.pct_chg  # 本次扫描间涨幅
        else:
            delta = cb.pct_chg * 0.3  # 估算：假设最后30分钟贡献30%

        if delta < 0.3:
            continue  # 没加速

        # 打分：正股强度 + 加速幅度 + 溢价安全性
        stock_score = min(cb.stock_pct / 10, 5) * 10  # 正股10%满分
        accel_score = min(delta / 2, 5) * 10          # 加速2%满分
        prem_score = 15 if cb.premium < 5 else (8 if cb.premium < 10 else 3)
        scale_score = 15 if cb.scale < 2 else (8 if cb.scale < 3 else 3)

        score = stock_score + accel_score + prem_score + scale_score
        candidates.append((cb, score))

    candidates.sort(key=lambda x: -x[1])
    return candidates


def print_signal(candidates, limit=10):
    if not candidates:
        print("  无信号")
        return
    print(f'\n  ╔══════════════════════════════════════════════════╗')
    print(f'  ║  14:40 尾盘吸筹信号                              ║')
    print(f'  ╠══════════════════════════════════════════════════╣')
    print(f'  ║ {"转债":>8s} {"正股%":>5s} {"CB%":>4s} {"溢价":>4s} {"规模":>4s} {"得分":>4s} ║')
    for cb, score in candidates[:limit]:
        print(f'  ║ {cb.name:>8s} {cb.stock_pct:+4.1f}% {cb.pct_chg:+3.1f}% {cb.premium:3.0f}% {cb.scale:3.1f}亿 {score:4.0f} ║')
    print(f'  ╚══════════════════════════════════════════════════╝')
    print(f'  共 {len(candidates)} 只候选 | 次日竞价卖出')


if __name__ == '__main__':
    data = LiveData(os.path.join(os.path.dirname(__file__), 'strategy_1440.py'))
    # 第一次扫描（模拟14:10快照）
    prev = data.scan()
    time.sleep(1)
    # 第二次扫描（模拟14:40快照）
    sig = run_1440(data, prev)
    print_signal(sig)
    data.close()
