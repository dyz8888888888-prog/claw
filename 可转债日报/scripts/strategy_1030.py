"""
策略一：10:30 大盘情绪回升
驱动: 全市场涨跌比 → 大盘情绪 → CB滞后补涨
"""

from live_data import LiveData, LiveCB
from typing import List, Tuple


def run_1030(data: LiveData) -> List[Tuple[LiveCB, float]]:
    """
    10:30 大盘情绪回升扫描

    条件:
    1. 涨跌比 > 1.2（大盘偏暖）
    2. CB溢价 < 20%（跟涨效率）
    3. CB涨幅 < 正股涨幅 × 0.6（滞涨）
    4. 规模 < 8亿（弹性）

    打分: 滞涨度 + 规模分 + 溢价分
    """

    # 1. 大盘情绪
    breadth = data.market_breadth()
    ratio = breadth['ratio']
    if ratio < 1.2:
        print(f'  大盘涨跌比 {ratio:.2f} < 1.2，信号不触发')
        return []

    # 2. 扫描全市场 CB
    cbs = data.scan()
    print(f'  扫描 {len(cbs)} 只CB | 涨跌比 {ratio:.2f} | 涨停 {breadth["limit_up"]}')

    # 3. 筛选 + 打分
    candidates = []
    for cb in cbs:
        # 硬过滤
        if cb.premium > 20 or cb.premium < -10:
            continue
        if cb.scale > 8:
            continue
        if cb.pct_chg < -3:
            continue  # 大跌不碰
        if cb.stock_pct <= 0.5:
            continue  # 正股不动，CB没动力
        if cb.volume < 1000000:
            continue  # 无流动性

        # 滞涨度: 正股涨了但CB没跟
        if cb.stock_pct <= 0:
            lag = 0
        else:
            lag = 1 - min(cb.pct_chg / cb.stock_pct, 1)
            lag = max(lag, 0)

        if lag < 0.1:
            continue  # 跟涨到位了

        # 打分
        scale_score = 10 if cb.scale < 2 else (5 if cb.scale < 5 else 0)
        prem_score = 15 if cb.premium < 5 else (8 if cb.premium < 10 else 3)
        score = lag * 50 + scale_score + prem_score

        candidates.append((cb, score))

    candidates.sort(key=lambda x: -x[1])
    return candidates


def print_signal(candidates: List[Tuple[LiveCB, float]], limit: int = 10):
    """格式化输出信号"""
    if not candidates:
        print("  无信号")
        return

    sh_idx = 0
    try:
        from tqcenter import tq
        snap = tq.get_market_snapshot(stock_code='999999.SH')
        sh_idx = (float(snap.get('Now', 1)) / float(snap.get('LastClose', 1)) - 1) * 100
    except:
        pass

    print(f'\n  ╔══════════════════════════════════════════════╗')
    print(f'  ║  10:30 大盘情绪回升信号  |  上证 {sh_idx:+.2f}%           ║')
    print(f'  ╠══════════════════════════════════════════════╣')
    print(f'  ║ {"转债":>10s} {"正股%":>6s} {"CB%":>5s} {"溢价%":>5s} {"规模":>5s} {"得分":>4s} ║')

    for cb, score in candidates[:limit]:
        print(f'  ║ {cb.name:>10s} {cb.stock_pct:+5.1f}% {cb.pct_chg:+4.1f}% {cb.premium:4.1f}% {cb.scale:4.1f}亿 {score:4.0f} ║')

    print(f'  ╚══════════════════════════════════════════════╝')
    print(f'  共 {len(candidates)} 只候选')


if __name__ == '__main__':
    import os
    data = LiveData(os.path.join(os.path.dirname(__file__), 'strategy_1030.py'))
    sig = run_1030(data)
    print_signal(sig)
    data.close()
