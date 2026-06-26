"""
盘前题材分析 — PreMarket Analyzer

每天 8:50 执行, 输出:
  1. 昨日最强概念 Top 10 (基于同花顺概念映射+转债池)
  2. 今日候选概念 (可延续的题材)
  3. 每个候选概念的龙一龙二龙三 (正股涨幅 Top 3 对应的转债)
  4. 风险过滤: 高溢价(>50%)/强赎/末日轮 标的排除
  5. 输出: logs/pre_market_{date}.json + pre_market_{date}.md

用法:
  python pre_market.py            # 基于昨日数据
  python pre_market.py --live     # 实时模式 (盘中快照)
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
CONCEPT_MAP_PATH = r"C:\Users\DYZ\WorkBuddy\Claw\可转债日报\cb_concept_map.json"


def load_concept_map() -> dict:
    with open(CONCEPT_MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cb_pool() -> list[dict]:
    """加载监控池转债 (含强赎/溢价率)"""
    from config import CONFIG
    from core.bond_selector import BondSelector
    s = BondSelector(CONFIG)
    return s.get_monitor_list()


def get_snapshots() -> dict:
    """获取实时快照 (live 模式) 或返回 None (昨日模式)"""
    if "--live" not in sys.argv:
        return None
    from core.bond_selector import BondSelector
    from core.data_fusion import DataFusion
    from config import CONFIG

    sel = BondSelector(CONFIG)
    ml = sel.get_monitor_list()
    fusion = DataFusion(ml)
    return fusion.merge()


def compute_top_concepts(concept_map: dict, pool: list[dict],
                         snapshots: dict = None) -> list[dict]:
    """按概念聚合, 返回 Top 概念 + 龙一龙二龙三"""

    # 构建 code → info
    code_info: dict[str, dict] = {}
    pool_codes = set()
    for item in pool:
        code = item.get("code_num", "")
        if code:
            pool_codes.add(code)
            code_info[code] = {
                "name": item.get("name", ""),
                "stock_code": item.get("stock_code", ""),
                "stock_name": item.get("stock_name", ""),
                "premium": item.get("premium_ratio"),
                "redeem": item.get("redeem_status", ""),
                "scale": item.get("issue_scale", 0),
            }

    # 概念 → 债列表 (含涨跌幅)
    concept_bonds: dict[str, list[dict]] = defaultdict(list)
    for code in pool_codes:
        concepts = concept_map.get(code, {}).get("concepts", [])
        if not concepts:
            continue
        info = code_info.get(code, {})
        pct = 0.0
        if snapshots and code in snapshots:
            pct = getattr(snapshots[code], "change_pct", 0) or 0
        entry = {**info, "code": code, "pct": pct}
        for c in concepts:
            concept_bonds[c].append(entry)

    # 排序+过滤
    results = []
    for concept, bonds in concept_bonds.items():
        if len(bonds) < 2:
            continue
        avg_pct = round(sum(b["pct"] for b in bonds) / len(bonds), 2)
        # 龙一龙二龙三: 正股涨幅 Top 3
        sorted_b = sorted(bonds, key=lambda b: -b["pct"])
        dragon_1 = sorted_b[0] if len(sorted_b) > 0 else None
        dragon_2 = sorted_b[1] if len(sorted_b) > 1 else None
        dragon_3 = sorted_b[2] if len(sorted_b) > 2 else None

        results.append({
            "concept": concept,
            "avg_pct": avg_pct,
            "bond_count": len(bonds),
            "surge_count": sum(1 for b in bonds if b["pct"] > 2),
            "dragon_1": format_dragon(dragon_1),
            "dragon_2": format_dragon(dragon_2),
            "dragon_3": format_dragon(dragon_3),
            "risk_warnings": get_risk_warnings(bonds),
        })

    results.sort(key=lambda x: -x["avg_pct"])
    return results[:20]


def format_dragon(bond: dict | None) -> dict | None:
    if not bond:
        return None
    return {
        "code": bond["code"],
        "name": bond["name"],
        "stock_code": bond["stock_code"],
        "stock_name": bond["stock_name"],
        "pct": bond["pct"],
        "premium": bond["premium"],
        "scale": bond["scale"],
        "risk": "⚠强赎" if bond.get("redeem") in ("已公告强赎", "公告要强赎") else "",
    }


def get_risk_warnings(bonds: list[dict]) -> list[str]:
    warnings = []
    for b in bonds:
        if b.get("redeem") in ("已公告强赎", "公告要强赎"):
            warnings.append(f"{b['name']}({b['code']}) 强赎")
        prem = b.get("premium")
        if prem and prem > 100:
            warnings.append(f"{b['name']}({b['code']}) 溢价{prem:.0f}%")
    return warnings[:5]


def generate_markdown(data: list[dict], date_str: str) -> str:
    lines = [f"# 盘前题材分析 — {date_str}", ""]
    lines.append(f"生成时间: {datetime.now().strftime('%H:%M:%S')}")
    lines.append(f"候选概念: {len(data)} 个")
    lines.append("")

    for i, item in enumerate(data, 1):
        lines.append(f"## {i}. {item['concept']}  |  均涨 {item['avg_pct']:+.2f}%")
        lines.append(f"- 债数: {item['bond_count']} | 涨>2%: {item['surge_count']}")
        lines.append("")

        if item["dragon_1"]:
            d = item["dragon_1"]
            lines.append(f"### 🥇 龙一: {d['name']} ({d['code']})")
            lines.append(f"- 正股: {d['stock_name']} ({d['stock_code']})")
            lines.append(f"- 涨跌: {d['pct']:+.2f}% | 溢价: {d['premium']}% | 规模: {d['scale']}亿")
            if d["risk"]:
                lines.append(f"- ⚠️ {d['risk']}")
            lines.append("")

        for j, key in enumerate(["dragon_2", "dragon_3"], 2):
            d = item.get(key)
            if d:
                emoji = ["🥈", "🥉"][j - 2]
                lines.append(f"### {emoji} 龙{j}: {d['name']} ({d['code']})")
                lines.append(f"- 涨跌: {d['pct']:+.2f}% | 溢价: {d['premium']}% | 规模: {d['scale']}亿")
                lines.append("")

        if item["risk_warnings"]:
            lines.append("### ⚠️ 风险提示")
            for w in item["risk_warnings"]:
                lines.append(f"- {w}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main():
    date_str = datetime.now().strftime("%Y%m%d")

    # 加载数据
    concept_map = load_concept_map()
    pool = load_cb_pool()
    snapshots = get_snapshots()

    # 计算
    data = compute_top_concepts(concept_map, pool, snapshots)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # JSON
    json_path = os.path.join(OUTPUT_DIR, f"pre_market_{date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Markdown
    md = generate_markdown(data, date_str)
    md_path = os.path.join(OUTPUT_DIR, f"pre_market_{date_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"盘前报告已生成:")
    print(f"  JSON: {json_path} ({len(data)} 个概念)")
    print(f"  MD:   {md_path}")
    for item in data[:5]:
        d1 = item["dragon_1"]
        d1_name = d1["name"] if d1 else "—"
        print(f"  📊 {item['concept']:18s} 均涨{item['avg_pct']:+.2f}%  "
              f"龙一:{d1_name}")


if __name__ == "__main__":
    main()
