"""
同花顺API 概念映射更新脚本 (替代 pywencai)

数据流:
  1. GET /api/a-share-index/catalog/ths-index-list?tag=cn_concept → 388个概念板块
  2. 对每个概念 GET /api/a-share-index/constituents/ths-stock-list → 正股列表
  3. 加载监控池转债, 通过正股代码反查 → {转债代码: {concepts: [概念1, 概念2, ...]}}

用法:
  python update_concept_map_ths.py                    # 全量重建
  python update_concept_map_ths.py --incremental      # 增量更新(仅更新新上市转债)
"""

import json
import time
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict

BASE_URL = "https://fuyao.aicubes.cn"
API_KEY = "sk-fuyao-ZbW5ky_FP-yQ-xXQPeUT_IrLAA7ZoaS7"
OUTPUT_PATH = r"C:\Users\DYZ\WorkBuddy\Claw\可转债日报\cb_concept_map.json"


def api_get(path: str) -> dict:
    """调用同花顺 REST API"""
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"X-api-key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.reason}")
        return {}
    except Exception as e:
        print(f"  请求异常: {e}")
        return {}


def get_all_concepts() -> list[dict]:
    """获取全部概念板块"""
    print("获取概念板块清单...")
    data = api_get("/api/a-share-index/catalog/ths-index-list?tag=cn_concept")
    items = data.get("data", {}).get("item", [])
    print(f"  → {len(items)} 个概念板块")
    return items


def get_concept_constituents(thscode: str) -> list[dict]:
    """获取某个概念板块的成分股"""
    data = api_get(
        f"/api/a-share-index/constituents/ths-stock-list?thscode={thscode}"
    )
    return data.get("data", {}).get("item", [])


def load_cb_monitor_list() -> list[dict]:
    """加载监控池转债 (含正股代码)"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from config import CONFIG
        from core.bond_selector import BondSelector

        selector = BondSelector(CONFIG)
        pool = selector.load_cov_pool(force=True)
        items = []
        for _, row in pool.iterrows():
            code = str(row.get("code", "")).strip()
            # 提取6位数字
            import re
            m = re.search(r"(\d{6})", code)
            code_num = m.group(1) if m else code
            stock_code = str(row.get("stock_code", "")).strip()
            items.append({
                "code": code_num,
                "name": str(row.get("name", "")),
                "stock_code": stock_code,
            })
        print(f"加载转债池: {len(items)} 只 (含正股代码)")
        return items
    except Exception as e:
        print(f"加载转债池失败: {e}")
        return []


def build_concept_map(
    concepts: list[dict], cb_list: list[dict]
) -> dict:
    """
    构建 {转债代码: {concepts: [概念1, 概念2, ...]}} 映射

    策略: 遍历所有概念板块, 取成分股(ticker), 与转债正股代码匹配
    """
    # 正股代码 → 概念列表  (正向索引)
    stock_to_concepts: dict[str, list[str]] = defaultdict(list)

    total = len(concepts)
    for i, concept in enumerate(concepts):
        thscode = concept["thscode"]
        concept_name = concept["name"]
        print(f"  [{i+1}/{total}] {concept_name} ({thscode})...", end=" ")

        constituents = get_concept_constituents(thscode)
        count = len(constituents)

        for stock in constituents:
            ticker = stock.get("ticker", "")
            if ticker:
                stock_to_concepts[ticker].append(concept_name)

        print(f"{count} 只成分股")
        time.sleep(0.3)  # 控制请求频率

    # 构建正股代码集合 (用于匹配)
    cb_stock_codes = {item["stock_code"] for item in cb_list if item["stock_code"]}

    # 反查: 转债代码 → 概念列表
    result = {}
    matched = 0

    for item in cb_list:
        code_num = item["code"]
        stock_code = item["stock_code"]

        concepts_list = stock_to_concepts.get(stock_code, [])
        if concepts_list:
            matched += 1
            result[code_num] = {
                "name": item["name"],
                "concepts": concepts_list,
            }

    print(f"\n匹配结果: {matched}/{len(cb_list)} 只转债有概念映射")
    print(f"涉及概念: {len(set(c for v in result.values() for c in v['concepts']))} 个")
    return result


def main():
    incremental = "--incremental" in sys.argv

    # 加载已有映射 (增量模式)
    existing = {}
    if incremental and os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        print(f"增量模式: 已有 {len(existing)} 条映射")

    # 1. 获取概念板块
    concepts = get_all_concepts()
    if not concepts:
        print("ERROR: 无法获取概念板块, 中止")
        sys.exit(1)

    # 2. 加载转债池
    cb_list = load_cb_monitor_list()
    if not cb_list:
        print("ERROR: 无法加载转债池, 中止")
        sys.exit(1)

    # 3. 如果已有正股→概念缓存, 可以复用 (跳过增量模式)
    if incremental and existing:
        # 增量模式: 只处理新上市转债
        new_codes = [c for c in cb_list if c["code"] not in existing]
        if not new_codes:
            print("增量模式: 无需更新, 所有转债已有映射")
            return

        print(f"增量模式: {len(new_codes)} 只新转债需更新")
        # 对每只新转债, 遍历概念找匹配
        result = dict(existing)
        for item in new_codes:
            code_num = item["code"]
            stock_code = item["stock_code"]
            matched_concepts = []
            # 只能全量遍历一次 (慢但确切)
            for concept in concepts:
                constituents = get_concept_constituents(concept["thscode"])
                for stock in constituents:
                    if stock.get("ticker") == stock_code:
                        matched_concepts.append(concept["name"])
                        break
                time.sleep(0.1)
            if matched_concepts:
                result[code_num] = {
                    "name": item["name"],
                    "concepts": matched_concepts,
                }
            print(f"  {code_num} {item['name']}: {len(matched_concepts)} 个概念")
        print(f"增量更新完成: {len(result)} 只")
    else:
        # 全量重建
        result = build_concept_map(concepts, cb_list)

    # 4. 写入
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"已写入: {OUTPUT_PATH}")
    print(f"总计: {len(result)} 只转债, 涉及 "
          f"{len(set(c for v in result.values() for c in v['concepts']))} 个概念")


if __name__ == "__main__":
    main()
