"""
全市场概念成分股缓存构建器

模式:
  python build_concept_cache.py                 # 全量: 388 次 API (首次)
  python build_concept_cache.py --incremental   # 全量更新: 388 次 API (每日刷新全部概念)

数据源: 同花顺扶摇 API → 写入 data/concept_stocks.json
"""

import json
import os
import sys
import time

# 确保能 import 项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG
from core.fuyao_client import get_fuyao_client

API_KEY = CONFIG.ext_api.fuyao_api_key
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "data", "concept_stocks.json")

_client = None

def _get_client():
    """统一 FuyaoClient (复用重试机制)"""
    global _client
    if _client is None:
        _client = get_fuyao_client()
    return _client

def api_get(path: str) -> dict:
    """Fuyao API 调用 (复用 Client 的重试机制)"""
    result = _get_client()._get(path)
    if result is None:
        raise RuntimeError(f"API 请求失败: {path}")
    return result


def load_existing() -> dict:
    """加载已有缓存"""
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("concept_stocks", {})
    return {}


def count_cb_bonds(concept_stocks: dict[str, list[str]]) -> dict[str, int]:
    """统计每个概念在转债池中的债券数"""
    from core.bond_selector import BondSelector
    sel = BondSelector(CONFIG)
    pool = sel.load_cov_pool(force=True)

    cb_stock_tickers: set[str] = set()
    for _, row in pool.iterrows():
        sc = str(row.get("stock_code", "")).strip()
        if sc:
            cb_stock_tickers.add(sc)

    counts = {}
    for concept, stocks in concept_stocks.items():
        tickers = set(s.split(".")[0] for s in stocks)
        counts[concept] = len(tickers & cb_stock_tickers)
    return counts


def build_full():
    """全量构建: 388 次 API"""
    print("全量模式: 拉取全部概念...")
    data = api_get("/api/a-share-index/catalog/ths-index-list?tag=cn_concept")
    concepts = data["data"]["item"]
    print(f"  {len(concepts)} 个概念")

    concept_stocks = {}
    errors = 0

    for i, item in enumerate(concepts):
        thscode = item["thscode"]
        name = item["name"]
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(concepts)}] {name}...")
        try:
            data = api_get(
                f"/api/a-share-index/constituents/ths-stock-list?thscode={thscode}"
            )
            stocks = [s["thscode"] for s in data["data"]["item"]
                      if s.get("thscode")]
            concept_stocks[name] = stocks
            time.sleep(0.15)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ⚠ {name}: {e}")

    # 排序: 权重股在前
    for c in concept_stocks:
        concept_stocks[c].sort(key=_stock_rank)

    save(concept_stocks, errors)


def build_incremental():
    """增量模式: 更新全部概念 (全量刷新成分股列表)"""
    existing = load_existing()
    if not existing:
        print("无现有缓存, 切换到全量模式...")
        build_full()
        return

    print(f"增量模式: 全量更新 {len(existing)} 个概念")
    print(f"  已有缓存 {len(existing)} 个概念")

    # 获取概念列表(只需要 thscode 映射)
    data = api_get("/api/a-share-index/catalog/ths-index-list?tag=cn_concept")
    concepts = {item["name"]: item["thscode"] for item in data["data"]["item"]}
    print(f"  同花顺返回 {len(concepts)} 个概念")

    concept_stocks = dict(existing)
    errors = 0
    updated = 0
    skipped = 0

    total = len(existing)
    for i, name in enumerate(sorted(existing.keys())):
        thscode = concepts.get(name)
        if not thscode:
            skipped += 1
            continue
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{total}] {name}...")
        try:
            data = api_get(
                f"/api/a-share-index/constituents/ths-stock-list?thscode={thscode}"
            )
            stocks = [s["thscode"] for s in data["data"]["item"]
                      if s.get("thscode")]
            stocks.sort(key=_stock_rank)
            concept_stocks[name] = stocks
            updated += 1
            time.sleep(0.15)  # 全量模式统一间隔
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ⚠ {name}: {e}")

    save(concept_stocks, errors)
    print(f"  更新: {updated} 个, 跳过: {skipped}, 总概念: {len(concept_stocks)}")


def save(concept_stocks: dict, errors: int):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    avg = round(sum(len(v) for v in concept_stocks.values())
                / max(len(concept_stocks), 1), 0)
    stats = {
        "total_concepts": len(concept_stocks),
        "avg_stocks": avg,
        "errors": errors,
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"stats": stats, "concept_stocks": concept_stocks},
                  f, ensure_ascii=False)
    print(f"完成: {len(concept_stocks)} 个概念, 每概念 {avg:.0f} 只, 错误 {errors}")


def _stock_rank(thscode: str) -> int:
    code = thscode.split(".")[0]
    if code.startswith("600") or code.startswith("601") or code.startswith("603"):
        return 1
    if code.startswith("000") or code.startswith("001") or code.startswith("002"):
        return 2
    if code.startswith("300") or code.startswith("301"):
        return 3
    if code.startswith("688"):
        return 4
    if code.startswith("920") or code.startswith("4") or code.startswith("8"):
        return 5
    return 9


if __name__ == "__main__":
    if "--incremental" in sys.argv:
        build_incremental()
    else:
        build_full()
