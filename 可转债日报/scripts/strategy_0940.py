"""
策略二：9:40 题材热度补涨
驱动: 竞价确认热概念 → 同概念CB滞涨 → 买入
"""
import os, json
from live_data import LiveData, LiveCB
from typing import List, Tuple, Dict

# 概念缓存路径
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONCEPT_MAP_PATH = os.path.join(_PROJECT_DIR, 'cb_concept_map.json')
CONCEPT_HEAT_PATH = os.path.join(_PROJECT_DIR, 'cb_concept_heat.json')


def _get_concept_heat() -> Dict[str, float]:
    """获取概念热度：优先 pywencai 实时，回退缓存"""
    # 1. 先试 pywencai 实时
    try:
        import pywencai
        cookie_path = os.path.join(_PROJECT_DIR, '.wencai_cookie')
        cookie = open(cookie_path).read().strip()
        df = pywencai.get(query='概念板块涨幅排序', cookie=cookie, log=False, perpage=30)
        heat = {}
        if hasattr(df, 'columns') and '板块名称' in df.columns:
            for _, row in df.iterrows():
                name = str(row.get('板块名称', ''))
                try:
                    pct = float(row.get('板块涨幅', 0))
                except:
                    pct = 0
                heat[name] = abs(pct) * 10
            if heat:
                return heat
    except:
        pass

    # 2. 回退：用概念热度缓存
    if os.path.exists(CONCEPT_HEAT_PATH):
        with open(CONCEPT_HEAT_PATH, encoding='utf-8') as f:
            return json.load(f)

    # 3. 最后回退：从概念映射聚合频次
    if os.path.exists(CONCEPT_MAP_PATH):
        with open(CONCEPT_MAP_PATH, encoding='utf-8') as f:
            old = json.load(f)
        heat = {}
        for info in old.values():
            for c in info.get('concepts', []):
                heat[c] = heat.get(c, 0) + 1
        return heat
    return {}


def run_0940(data: LiveData) -> List[Tuple[LiveCB, float, str]]:
    """
    9:40 题材热度补涨

    条件:
    1. 概念在TOP热榜
    2. 该概念下正股>3%但CB跟涨<50%
    3. CB溢价<30%, 规模<8亿
    """
    heat = _get_concept_heat()
    if not heat:
        print('  概念热度获取失败')
        return []

    top_concepts = sorted(heat.items(), key=lambda x: -x[1])[:20]
    print(f'  TOP20热概念: {[(c[:6], round(s,1)) for c,s in top_concepts[:8]]}')

    # 加载概念→CB映射
    concept_stocks = {}
    for cb_code, info in _load_concept_map().items():
        for c in info.get('concepts', []):
            if c in dict(top_concepts):
                if c not in concept_stocks:
                    concept_stocks[c] = []
                concept_stocks[c].append(cb_code)

    cbs = data.scan()
    print(f'  扫描: {len(cbs)}只CB')

    candidates = []
    for cb in cbs:
        # 硬过滤
        if cb.premium > 30 or cb.premium < -10:
            continue
        if cb.scale > 8:
            continue
        if cb.stock_pct < 2:  # 正股还没动
            continue
        if cb.volume < 500000:
            continue
        if cb.pct_chg > 2:  # CB已经涨了
            continue

        # 跟涨比
        if cb.stock_pct > 0:
            follow = cb.pct_chg / cb.stock_pct
        else:
            follow = 1
        if follow > 0.5:
            continue  # 跟涨到位了

        # 是否在热概念中
        cb_concepts = []
        for concept_name, cb_codes in concept_stocks.items():
            if cb.code in cb_codes:
                cb_concepts.append(concept_name)

        if not cb_concepts:
            continue  # 不在热概念中

        # 打分
        heat_score = sum(heat.get(c, 0) for c in cb_concepts[:3])
        lag_score = (1 - follow) * 50
        prem_score = 10 if cb.premium < 10 else (5 if cb.premium < 20 else 0)
        score = heat_score * 0.3 + lag_score * 0.5 + prem_score * 0.2

        candidates.append((cb, score, '+'.join(cb_concepts[:3])))

    candidates.sort(key=lambda x: -x[1])
    return candidates


def _load_concept_map() -> dict:
    if os.path.exists(CONCEPT_MAP_PATH):
        with open(CONCEPT_MAP_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def print_signal(candidates, limit=10):
    if not candidates:
        print("  无信号")
        return
    print(f'\n  ╔══════════════════════════════════════════════════╗')
    print(f'  ║  9:40 题材热度补涨信号                          ║')
    print(f'  ╠══════════════════════════════════════════════════╣')
    print(f'  ║ {"转债":>8s} {"正股%":>5s} {"CB%":>4s} {"溢价":>4s} {"得分":>4s} {"概念":20s} ║')
    for cb, score, concepts in candidates[:limit]:
        print(f'  ║ {cb.name:>8s} {cb.stock_pct:+4.1f}% {cb.pct_chg:+3.1f}% {cb.premium:3.0f}% {score:4.0f} {concepts:20s} ║')
    print(f'  ╚══════════════════════════════════════════════════╝')
    print(f'  共 {len(candidates)} 只候选')


if __name__ == '__main__':
    data = LiveData(os.path.join(os.path.dirname(__file__), 'strategy_0940.py'))
    sig = run_0940(data)
    print_signal(sig)
    data.close()
