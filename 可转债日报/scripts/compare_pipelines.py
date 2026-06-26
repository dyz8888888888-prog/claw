"""
双流水线对比分析脚本
收盘后运行：python compare_pipelines.py [日期]

功能：
1. 解析当日所有日报（OLD + TDX），提取各快照 TOP 排名
2. 用 TDX 获取收盘价，计算各流水线选股表现
3. 输出对比报告
"""
import sys
import os
import re
import json
import glob
from datetime import datetime, date
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

# ============================================================
# 数据结构
# ============================================================

@dataclass
class Pick:
    rank: int
    name: str          # CB 名称
    code: str           # 代码（如果能解析到）
    price: float        # 快照时价格
    premium: float      # 溢价率
    scale: float        # 规模
    score: float        # 排序分（不同流水线含义不同）

@dataclass
class Snapshot:
    pipeline: str       # "OLD" | "TDX"
    time: str           # "09:40" 等
    picks: List[Pick]
    market_type: str    # 行情类型

def _to_float(s: str) -> float:
    """字符串转浮点数，'-' 视为 0"""
    s = s.strip().replace(',', '').replace('%', '')
    if s in ('', '-', '—', '--'):
        return 0.0
    return float(s)

# ============================================================
# 解析器
# ============================================================

def parse_old_report(path: str) -> Optional[Snapshot]:
    """解析旧流水线日报 (cb_report_*.md 或 日报_*.md 但不含TDX前缀)"""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    # 提取快照时间
    time_match = re.search(r'快照[：:]\s*(\d{2}:\d{2})', text)
    if not time_match:
        time_match = re.search(r'_(\d{4})\.md$', os.path.basename(path))
        if time_match:
            t = time_match.group(1)
            time_str = f"{t[:2]}:{t[2:]}"
        else:
            return None
    else:
        time_str = time_match.group(1)

    # 解析排名表（旧格式：| # | 名称 | 溢价 | 规模 | ...）
    picks = []
    in_table = False
    for line in text.split('\n'):
        if line.startswith('| # |') or line.startswith('|#|'):
            in_table = True
            continue
        if in_table and line.startswith('|---'):
            continue
        if in_table and not line.startswith('|'):
            break
        if in_table:
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) < 4:
                continue
            try:
                rank = int(cols[0])
                name = cols[1]
                premium = float(cols[2]) if cols[2] else 0
                scale = float(cols[3]) if cols[3] else 0
                picks.append(Pick(rank=rank, name=name, code='',
                                  price=0, premium=premium, scale=scale, score=0))
            except (ValueError, IndexError):
                continue

    return Snapshot(pipeline='OLD', time=time_str, picks=picks[:20], market_type='-')


def parse_tdx_report(path: str) -> Optional[Snapshot]:
    """解析新 TDX 流水线日报 (日报_TDX_*.md)"""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    # 提取行情类型和快照时间
    mtype_match = re.search(r'行情\**\s*[：:]\s*(\S+)型', text)
    market_type = mtype_match.group(1) if mtype_match else '-'

    time_str = None
    # 文件名末端: ..._HHMM.md → 提取时间
    fn_time = re.search(r'_(\d{4})\.md$', os.path.basename(path))
    if fn_time:
        t = fn_time.group(1)
        time_str = f"{t[:2]}:{t[2:]}"
    # 内容中: T09:34 或 ...09:34...
    if not time_str:
        content_time = re.search(r'T(\d{2}:\d{2})', text)
        if content_time:
            time_str = content_time.group(1)
    # 报告标题行: # 可转债日报 — YYYY-MM-DD HH:MM
    if not time_str:
        title_time = re.search(r'(\d{2}:\d{2})', text)
        if title_time:
            time_str = title_time.group(1)
    if not time_str:
        return None

    # 解析排名表（新格式：| # | 转债 | 候选分 | 概念分 | 最终分 | 溢价% | 规模 | 价格 | 振幅% |）
    picks = []
    in_table = False
    for line in text.split('\n'):
        if 'TOP20 排名' in line or 'TOP 20' in line:
            in_table = False
            continue
        if '| # | 转债' in line or '|#|转债' in line:
            in_table = True
            continue
        if in_table and line.startswith('|---'):
            continue
        if in_table and not line.startswith('|'):
            break
        if in_table and line.startswith('|') and len(line) > 10:
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) < 8:
                continue
            try:
                rank = int(cols[0])
                name = cols[1]
                # Format: | # | 转债 | 候选分 | 概念分 | 最终分 | 溢价% | 规模 | 价格 | 振幅% |
                # Index:    0     1      2        3        4       5      6      7      8
                price = _to_float(cols[7]) if len(cols) > 7 else 0
                premium = _to_float(cols[5]) if len(cols) > 5 else 0
                scale_raw = cols[6] if len(cols) > 6 else '0'
                scale = float(scale_raw.replace('亿', '').replace('-', '0'))
                score = _to_float(cols[4]) if len(cols) > 4 else 0  # 最终分
                picks.append(Pick(rank=rank, name=name, code='',
                                  price=price, premium=premium, scale=scale, score=score))
            except (ValueError, IndexError):
                continue

    return Snapshot(pipeline='TDX', time=time_str, picks=picks[:20], market_type=market_type)


def find_today_reports(report_dir: str, date_str: str) -> Tuple[List[str], List[str]]:
    """返回 (old_reports, tdx_reports)"""
    old_reports = []
    tdx_reports = []

    # OLD: 日报_YYYY-MM-DD_HHMM.md
    for f in glob.glob(os.path.join(report_dir, f'日报_{date_str}_*.md')):
        basename = os.path.basename(f)
        if '_TDX_' in basename:
            tdx_reports.append(f)
        else:
            old_reports.append(f)

    # TDX: 日报_TDX_YYYY-MM-DD_HHMM.md
    for f in glob.glob(os.path.join(report_dir, f'日报_TDX_{date_str}_*.md')):
        tdx_reports.append(f)

    # 去重
    tdx_reports = list(set(tdx_reports))
    old_reports.sort()
    tdx_reports.sort()
    return old_reports, tdx_reports


# ============================================================
# 对比分析
# ============================================================

def _time_minutes(t: str) -> int:
    """"09:34" → 574 (minutes since midnight)"""
    parts = t.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def _align_snapshots(old_snaps: List[Snapshot], tdx_snaps: List[Snapshot],
                     max_diff_min: int = 10) -> List[Tuple[Snapshot, Snapshot]]:
    """
    按时间最近原则对齐两路快照。
    返回 [(old, tdx), ...] 配对列表，未配对的不返回。
    """
    old_ordered = sorted(old_snaps, key=lambda s: _time_minutes(s.time))
    tdx_ordered = sorted(tdx_snaps, key=lambda s: _time_minutes(s.time))

    pairs = []
    used_tdx = set()
    for old in old_ordered:
        old_min = _time_minutes(old.time)
        best = None
        best_diff = max_diff_min + 1
        for tdx in tdx_ordered:
            if tdx.time in used_tdx:
                continue
            diff = abs(_time_minutes(tdx.time) - old_min)
            if diff < best_diff:
                best_diff = diff
                best = tdx
        if best and best_diff <= max_diff_min:
            pairs.append((old, best))
            used_tdx.add(best.time)
    return pairs


def compare_rankings(old_snaps: List[Snapshot], tdx_snaps: List[Snapshot]):
    """对比排名重叠度"""
    print("\n" + "=" * 70)
    print("  排名重叠度对比")
    print("=" * 70)

    pairs = _align_snapshots(old_snaps, tdx_snaps)
    if not pairs:
        print("\n  (无时间匹配的快照对)")
        return

    for old, tdx in pairs:
        old_names = {p.name for p in old.picks[:10]}
        tdx_names = {p.name for p in tdx.picks[:10]}
        overlap = old_names & tdx_names

        print(f"\n  {old.time}(OLD) ↔ {tdx.time}(TDX)  [{tdx.market_type}型]:")
        print(f"    OLD TOP10: {', '.join(list(old_names)[:5])}...")
        print(f"    TDX TOP10: {', '.join(list(tdx_names)[:5])}...")
        print(f"    重叠: {len(overlap)}/10  ({len(overlap)*10}%)")
        if overlap:
            print(f"    共同: {', '.join(sorted(overlap))}")
        only_old = old_names - tdx_names
        only_tdx = tdx_names - old_names
        if only_old:
            print(f"    仅OLD: {', '.join(sorted(list(only_old))[:5])}")
        if only_tdx:
            print(f"    仅TDX: {', '.join(sorted(list(only_tdx))[:5])}")


def compare_scores(old_snaps: List[Snapshot], tdx_snaps: List[Snapshot]):
    """对比评分结构"""
    print("\n" + "=" * 70)
    print("  评分结构对比")
    print("=" * 70)

    pairs = _align_snapshots(old_snaps, tdx_snaps)
    if not pairs:
        print("\n  (无时间匹配的快照对)")
        return

    for old, tdx in pairs:
        print(f"\n  {old.time}(OLD) ↔ {tdx.time}(TDX):")
        if old.picks:
            avg_prem_o = sum(p.premium for p in old.picks[:10]) / len(old.picks[:10])
            avg_scale_o = sum(p.scale for p in old.picks[:10]) / len(old.picks[:10])
            print(f"    OLD: 均价溢={avg_prem_o:.1f}%  均价规模={avg_scale_o:.2f}亿")
        if tdx.picks:
            avg_prem_t = sum(p.premium for p in tdx.picks[:10]) / len(tdx.picks[:10])
            avg_scale_t = sum(p.scale for p in tdx.picks[:10]) / len(tdx.picks[:10])
            avg_score_t = sum(p.score for p in tdx.picks[:10]) / len(tdx.picks[:10])
            avg_price_t = sum(p.price for p in tdx.picks[:10]) / len(tdx.picks[:10])
            print(f"    TDX: 均价溢={avg_prem_t:.1f}%  均价规模={avg_scale_t:.2f}亿  "
                  f"均分={avg_score_t:.0f}  均价={avg_price_t:.0f}")


# ============================================================
# 主函数
# ============================================================

def main():
    date_str = date.today().strftime('%Y-%m-%d')
    if len(sys.argv) > 1:
        date_str = sys.argv[1]

    report_dir = PROJECT_DIR

    print(f"双流水线对比分析 — {date_str}")
    print("-" * 70)

    # 1. 找日报
    old_files, tdx_files = find_today_reports(report_dir, date_str)
    print(f"旧流水线日报: {len(old_files)} 个")
    print(f"TDX流水线日报: {len(tdx_files)} 个")

    if not old_files and not tdx_files:
        print("\n⚠️ 未找到任何日报文件，请确认流水线已运行")
        return

    # 2. 解析
    old_snaps, tdx_snaps = [], []
    for f in old_files:
        snap = parse_old_report(f)
        if snap:
            old_snaps.append(snap)
            print(f"  解析 OLD: {os.path.basename(f)} → {snap.time} ({len(snap.picks)}只)")
    for f in tdx_files:
        snap = parse_tdx_report(f)
        if snap:
            tdx_snaps.append(snap)
            print(f"  解析 TDX: {os.path.basename(f)} → {snap.time} ({len(snap.picks)}只) [{snap.market_type}型]")

    # 3. 对比
    compare_rankings(old_snaps, tdx_snaps)
    compare_scores(old_snaps, tdx_snaps)

    # 4. 汇总
    print("\n" + "=" * 70)
    print("  结论")
    print("=" * 70)
    old_times = {s.time for s in old_snaps}
    tdx_times = {s.time for s in tdx_snaps}
    pairs = _align_snapshots(old_snaps, tdx_snaps)
    total_overlap = 0
    total_pairs = len(pairs)
    for old, tdx in pairs:
        overlap = len({p.name for p in old.picks[:10]} & {p.name for p in tdx.picks[:10]})
        total_overlap += overlap
    if total_pairs:
        avg_overlap = total_overlap / total_pairs
        print(f"\n平均 TOP10 重叠率: {avg_overlap:.1f}/10 ({avg_overlap*10:.0f}%)")
        if avg_overlap >= 8:
            print("→ 两条流水线高度一致，选股逻辑趋同")
        elif avg_overlap >= 5:
            print("→ 中等重叠，各有偏好")
        else:
            print("→ 低重叠，两条流水线差异巨大")
    print(f"\nOLD 覆盖 {len(old_times)} 个快照 | TDX 覆盖 {len(tdx_times)} 个快照")


if __name__ == '__main__':
    main()
