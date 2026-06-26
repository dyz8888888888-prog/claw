"""
问财概念龙头批量查询 — pre-market

流程:
  1. 从 cb_concept_map.json 取 Top 50 活跃概念 (按池内债券数排序)
  2. pywencai 批量查每个概念的 "概念龙头股"
  3. 写入 data/concept_leaders_{date}.json
  4. 决策管道启动时加载 → 盘中龙一龙二龙三精确匹配

API: ~50 次 pywencai 查询, ~1-2 分钟
用法: python query_concept_leaders.py
"""

import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CONCEPT_MAP_PATH = r"C:\Users\DYZ\WorkBuddy\Claw\可转债日报\cb_concept_map.json"


def load_concepts() -> list[tuple[str, int]]:
    """加载概念并按池内债券数排序"""
    with open(CONCEPT_MAP_PATH, "r", encoding="utf-8") as f:
        cmap = json.load(f)

    counter = Counter()
    for info in cmap.values():
        for c in info.get("concepts", []):
            counter[c] += 1

    return counter.most_common(50)


def query_leaders(concept: str, today: str) -> dict:
    """用 pywencai 查询单个概念的龙头股"""
    import pywencai
    try:
        df = pywencai.get(query=f"{concept}概念龙头股", loop=True)
        field = f"概念龙头[{today}]"
        leaders = []
        for _, row in df.iterrows():
            is_leader = row.get(field, "")
            code = str(row.get("股票代码", "")).split(".")[0]
            name = row.get("股票简称", "")
            if is_leader and code:
                leaders.append({
                    "code": code,
                    "name": name,
                    "rank": len(leaders) + 1,
                })
            if len(leaders) >= 3:
                break
        return {"concept": concept, "leaders": leaders, "count": len(df), "error": ""}
    except Exception as e:
        return {"concept": concept, "leaders": [], "count": 0, "error": str(e)}


def build_ticker_index(leaders_map: dict[str, list[dict]]) -> dict[str, list[str]]:
    """构建正股代码→龙头概念 反向索引"""
    ticker_index: dict[str, list[str]] = {}
    for concept, leaders in leaders_map.items():
        for ldr in leaders:
            code = ldr["code"]
            if code not in ticker_index:
                ticker_index[code] = []
            ticker_index[code].append(concept)
    return ticker_index


def main():
    today = time.strftime("%Y%m%d")
    concepts = load_concepts()
    print(f"活跃概念 Top 50 (按池内债券数)")
    for c, cnt in concepts[:10]:
        print(f"  {c:15s} {cnt} 只")

    leaders_map: dict[str, list[dict]] = {}
    errors = 0

    for i, (concept, _) in enumerate(concepts):
        qname = concept.replace("概念", "")
        print(f"[{i+1:2d}/50] {qname}", end=" ", flush=True)
        result = query_leaders(qname, today)
        if result["error"]:
            errors += 1
            print(f"⚠ {result['error'][:30]}")
        else:
            leaders = result["leaders"]
            print(f"→ {len(leaders)} 只龙头: {' / '.join(l['name'] for l in leaders)}")
        leaders_map[concept] = result["leaders"]
        time.sleep(1.2)  # 控制请求频率

    # 构建反向索引
    ticker_index = build_ticker_index(leaders_map)

    # 存盘
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output = {
        "date": today,
        "total_concepts": len(leaders_map),
        "concepts_with_leaders": sum(1 for v in leaders_map.values() if v),
        "errors": errors,
        "leaders": leaders_map,
        "ticker_index": ticker_index,
    }
    path = os.path.join(OUTPUT_DIR, f"concept_leaders_{today}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n完成: {output['concepts_with_leaders']}/{len(leaders_map)} 个概念有龙头, "
          f"错误 {errors}")
    print(f"输出: {path}")


if __name__ == "__main__":
    main()
