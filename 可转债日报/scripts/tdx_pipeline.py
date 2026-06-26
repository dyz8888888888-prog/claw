"""
TDX 主流水线 — 通达信 V7.73 单数据源版
取代 push2 + mx-finance-data + i问财查询
概念评分使用 i问财缓存（cb_concept_map.json + cb_concept_heat.json）
TDX 提供概念补充（前次缺失的正股）

执行: python scripts/tdx_pipeline.py
输出: 日报_YYYY-MM-DD_HHMM.md + 日报对比版
"""

import sys
import os
import math
import json
import time
from datetime import datetime
from typing import List, Dict, Tuple, Optional

# 项目路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from tdx_fetcher import TDXFetcher, CBData, MarketBreadth, fix_stock_code, filter_concepts
from market_classifier import MarketClassifier, MarketSnapshot, MarketType, MARKET_CONFIG
from concept_fetcher import compute_concept_score, _load_cookie

# i问财可选导入
try:
    import pywencai
    HAS_WENCAI = True
except ImportError:
    HAS_WENCAI = False

# 通达信 tq — 用于正股名称补充
TDX_PLUGIN_DIR = "D:/tdx/PYPlugins/user"
sys.path.insert(0, TDX_PLUGIN_DIR)
try:
    from tqcenter import tq  # type: ignore
    HAS_TQ = True
except ImportError:
    HAS_TQ = False

# 文件路径
CONCEPT_MAP_PATH = os.path.join(PROJECT_DIR, 'cb_concept_map.json')
CONCEPT_HEAT_PATH = os.path.join(PROJECT_DIR, 'cb_concept_heat.json')
PUSH2_DATA_PATH = os.path.join(PROJECT_DIR, 'cb_top50_full.json')
REPORT_DIR = PROJECT_DIR

# 筛选条件
MAX_SCALE = 120      # 剩余规模上限(亿)
MAX_PREMIUM = 30     # 溢价率上限%
DEMON_PRICE = 800    # 妖债价格阈值
DEMON_PREMIUM = 100  # 妖债溢价率阈值


# ============================================================
# 辅助函数
# ============================================================

def load_cb_names() -> Dict[str, str]:
    """从 push2 JSON 加载 CB 名称映射 {code: name}"""
    names = {}
    if os.path.exists(PUSH2_DATA_PATH):
        try:
            with open(PUSH2_DATA_PATH, 'r', encoding='utf-8') as f:
                items = json.load(f)
            for item in items:
                code = str(item.get('f12', ''))
                name = str(item.get('f14', ''))
                if code and name:
                    names[code] = name
        except Exception:
            pass
    return names


def extend_concept_cache(cb_list: List[CBData],
                         concept_map: Dict,
                         fetcher: TDXFetcher = None,
                         max_new: int = 30) -> int:
    """
    增量扩展 i问财概念缓存。
    对不在缓存中的有效CB，先用TDX补充正股名称，再查i问财概念。
    返回新缓存的CB数量。
    """
    if not HAS_WENCAI:
        return 0

    # 出需要补充的CB（有正股代码，不在缓存，有意义的）
    missing = []
    for cb in cb_list:
        if cb.code in concept_map:
            continue
        # 只对已筛选为有效的CB做扩展（valid池）
        if not cb.stock_code or len(cb.stock_code) < 6:
            continue
        if cb.premium >= MAX_PREMIUM or cb.scale >= MAX_SCALE:
            continue  # 不在候选池的不扩展
        if is_demon(cb.premium, cb.price):
            continue
        missing.append(cb)

    if not missing:
        return 0

    missing = missing[:max_new]

    # 先用 TDX 获取正股名称
    stock_names = []
    cb_by_name = {}
    for cb in missing:
        if cb.stock_name and len(cb.stock_name) > 1:
            name = cb.stock_name
        elif fetcher and cb.stock_full:
            try:
                match = tq.get_match_stkinfo(key_word=cb.stock_code)
                if match and len(match) > 0:
                    # match returns dict: {'Code': '605006.SH', 'Name': '山东玻纤'}
                    if isinstance(match[0], dict):
                        name = str(match[0].get('Name', ''))
                    else:
                        name = str(match[0][1])
                    if name:
                        cb.stock_name = name
                    else:
                        continue
                else:
                    continue
            except Exception:
                continue
        else:
            continue

        stock_names.append(name)
        if name not in cb_by_name:
            cb_by_name[name] = []
        cb_by_name[name].append(cb)

    if not stock_names:
        return 0

    cookie = _load_cookie()
    if not cookie:
        return 0

    print(f'  i问财扩展: {len(stock_names)}只正股...')

    new_count = 0
    try:
        df = pywencai.get(
            query='所属概念',
            cookie=cookie,
            log=False,
            perpage=len(stock_names) + 10,
            find=stock_names,
        )

        if hasattr(df, 'columns') and '所属概念' in df.columns:
            for _, row in df.iterrows():
                row_name = str(row.get('股票简称', ''))
                concepts_str = str(row.get('所属概念', ''))
                if not concepts_str or concepts_str == 'nan':
                    continue
                concepts = [c.strip() for c in concepts_str.split(';') if c.strip()]
                if not concepts:
                    continue

                for search_name, cbs in cb_by_name.items():
                    if search_name in row_name or row_name in search_name:
                        for cb in cbs:
                            if cb.code not in concept_map:
                                concept_map[cb.code] = {
                                    'name': cb.name,
                                    'stock_name': row_name,
                                    'concepts': concepts,
                                    'concept_count': len(concepts),
                                }
                                new_count += 1
                        break

    except Exception as e:
        print(f'    i问财查询: {e}')

    if new_count:
        save_concept_cache(concept_map, new_count)

    return new_count


def resolve_names(cb_list: List[CBData], use_tdx: bool = False) -> int:
    """为 CB 补充名称（优先 push2 缓存，可选 TDX 补充）。返回新增名称数"""
    name_map = load_cb_names()
    added = 0
    unnamed = []

    for cb in cb_list:
        if cb.name and len(cb.name) > 2 and not cb.name.isdigit():
            continue  # 已有有效名称
        if cb.code in name_map:
            cb.name = name_map[cb.code]
            added += 1
        elif use_tdx and HAS_TQ:
            unnamed.append(cb)

    # TDX 补充
    if unnamed:
        for cb in unnamed:
            try:
                match = tq.get_match_stkinfo(key_word=cb.code)
                if match and len(match) > 0:
                    name = str(match[0].get('Name', ''))
                    if name and len(name) > 1:
                        cb.name = name
                        added += 1
            except Exception:
                pass

    return added

def normalize(values: List[float], reverse: bool = True) -> List[float]:
    """Min-max 归一化到 [0, 100]"""
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    result = [(v - mn) / (mx - mn) * 100 for v in values]
    return [100 - r for r in result] if reverse else result


def is_demon(premium: float, price: float) -> bool:
    """妖债判定"""
    return premium > DEMON_PREMIUM or price > DEMON_PRICE


# ============================================================
# 主力函数
# ============================================================

def load_concept_cache() -> Tuple[Dict, Dict]:
    """加载 i问财概念缓存"""
    concept_map = {}
    concept_heat = {}
    if os.path.exists(CONCEPT_MAP_PATH):
        with open(CONCEPT_MAP_PATH, 'r', encoding='utf-8') as f:
            concept_map = json.load(f)
    if os.path.exists(CONCEPT_HEAT_PATH):
        with open(CONCEPT_HEAT_PATH, 'r', encoding='utf-8') as f:
            concept_heat = json.load(f)
    return concept_map, concept_heat


def save_concept_cache(concept_map: Dict, new_count: int) -> None:
    """保存概念缓存到文件"""
    try:
        with open(CONCEPT_MAP_PATH, 'w', encoding='utf-8') as f:
            json.dump(concept_map, f, ensure_ascii=False, indent=2)
        print(f'    缓存: +{new_count}只 (总计{len(concept_map)}只)')
    except Exception as e:
        print(f'    保存失败: {e}')


def build_concept_scores(cb_list: List[CBData],
                         concept_map: Dict,
                         concept_heat: Dict,
                         fetcher: TDXFetcher = None) -> Dict[str, float]:
    """
    为每只 CB 计算概念分。
    优先用 i问财缓存，缺失时用 TDX 补充。
    """
    scores = {}
    supplement_count = 0

    for cb in cb_list:
        code = cb.code
        concepts = None

        # 1. 优先 i问财缓存
        if code in concept_map:
            info = concept_map[code]
            concepts = info.get('concepts', [])
            if concepts and len(concepts) > 0:
                scores[code] = compute_concept_score(list(concepts), concept_heat)
                continue

        # 2. TDX 补充
        if fetcher and cb.stock_full and cb.concepts:
            concepts = cb.concepts
        elif fetcher and cb.stock_full:
            concepts = fetcher.fetch_concepts(cb.stock_full)
            cb.concepts = concepts
            supplement_count += 1

        # 3. 兜底
        if concepts and len(concepts) > 0:
            scores[code] = compute_concept_score(list(concepts), concept_heat)
        else:
            scores[code] = 40.0  # 基准

    if supplement_count:
        print(f'  TDX补充概念: {supplement_count}只')

    return scores


def compute_candidate_scores_tdx(valid: List[CBData],
                                 market_type: MarketType = None) -> None:
    """
    V8: 纯开盘已知因子 — 不用今日振幅/涨跌，只用昨日+静态数据
    因子: 规模(25%) + 溢价(20%) + 昨成额(20%) + 5日波动(20%) + 昨动量(15%)
    """
    if not valid:
        return

    mtype = market_type if market_type else MarketType.B
    config = MARKET_CONFIG.get(mtype, MARKET_CONFIG[MarketType.B])
    w = config["score_weights"]

    prems = [c.premium for c in valid]
    scales = [c.scale for c in valid]

    # V8: 用昨日数据替代盘中数据
    yest_amounts = [max(c.yest_amount, 1) for c in valid]  # 昨日成交额
    vol5ds = [c.vol5d for c in valid]  # 近5日波动(|%|)
    yest_pcts = [abs(c.yest_pct) for c in valid]  # 昨日涨跌幅绝对值

    prem_s = normalize(prems, True)
    scale_s = normalize(scales, True)
    yest_amt_s = normalize([math.log10(a + 1) for a in yest_amounts], False)
    vol5d_s = normalize(vol5ds, False)
    yest_pct_s = normalize(yest_pcts, False)

    for i, cb in enumerate(valid):
        sb = 15 if cb.scale < 2 else (8 if cb.scale < 5 else 0)
        # 昨日换手活跃加分
        ab = 12 if cb.yest_amount > 5000 else (6 if cb.yest_amount > 1000 else 0)
        pb = 10 if cb.premium < 5 else (5 if cb.premium < 10 else 0)

        cb.candidate_score = round(
            vol5d_s[i] * w.get("amp", 0.20)    # amp权重→5日波动
            + scale_s[i] * w.get("scale", 0.25)
            + yest_amt_s[i] * w.get("amount", 0.20)  # amount权重→昨成额
            + prem_s[i] * w.get("premium", 0.20)
            + yest_pct_s[i] * w.get("pct", 0.15)     # pct权重→昨动量
            + sb + ab + pb, 1)


# ============================================================
# 日报生成
# ============================================================

def generate_report(valid: List[CBData],
                    demons: List[CBData],
                    breadth: MarketBreadth,
                    market_type: MarketType,
                    concept_scores: Dict[str, float],
                    concept_map: Dict,
                    ) -> str:
    """生成 Markdown 日报"""
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    time_str = now.strftime('%H:%M')
    mtype_name = market_type.value if isinstance(market_type, MarketType) else str(market_type)
    mtype_human = {
        'A': '强势普涨', 'B': '温和偏强', 'C': '微涨分化',
        'D': '横盘震荡', 'E': '微跌分化', 'F': '弱势普跌',
    }.get(mtype_name, mtype_name)

    lines = []
    lines.append(f'# 可转债日报 — {date_str} {time_str}')
    lines.append('')
    lines.append(f'**行情**: {mtype_human}型 | '
                 f'上证 {breadth.sh_pct:+.2f}% | '
                 f'深证 {breadth.sz_pct:+.2f}% | '
                 f'涨停 {breadth.limit_up_count}')
    lines.append(f'上涨 {breadth.up_count}({breadth.up_count*100//max(breadth.total_stocks,1)}%) '
                 f'| 下跌 {breadth.down_count}({breadth.down_count*100//max(breadth.total_stocks,1)}%) '
                 f'| 总 {breadth.total_stocks}')
    lines.append(f'有效CB: {len(valid)}只 | 妖债: {len(demons)}只')
    lines.append('')

    # TOP20
    lines.append('## TOP20 排名 (V9 候选+概念双向确认)')
    lines.append('')
    lines.append('| # | 转债 | 候选分 | 概念分 | 最终分 | 溢价% | 规模 | 价格 | 5日波动 | 昨成额 |')
    lines.append('|---|------|:-----:|:-----:|:-----:|:------:|:----:|-----:|:------:|:------:|')

    for i, cb in enumerate(valid[:20], 1):
        final = cb.final_score
        conc = getattr(cb, '_concept_score', concept_scores.get(cb.code, 40))
        yest_amt = f'{cb.yest_amount/10000:.2f}亿'
        lines.append(
            f'| {i} | {cb.name or cb.code} | {cb.candidate_score:.1f} | '
            f'{conc:.0f} | {final:.1f} | {cb.premium:.1f} | '
            f'{cb.scale:.2f}亿 | {cb.price:.0f} | {cb.vol5d:.1f}% | {yest_amt} |')

    lines.append('')

    # 概念贡献
    lines.append('## 概念分贡献（TOP10）')
    lines.append('')
    lines.append('| 转债 | 概念数 | 概念分 | 热门概念 |')
    lines.append('|------|:---:|:---:|------|')

    for cb in valid[:10]:
        cc = concept_scores.get(cb.code, 40)
        concepts = cb.concepts or []
        hot = [c for c in concepts if c in CONCEPT_HEAT_CACHE and CONCEPT_HEAT_CACHE[c] > 40][:3]
        lines.append(
            f'| {cb.name or cb.code} | {len(concepts)} | {cc:.0f} | '
            f'{", ".join(hot) if hot else "-"} |')

    lines.append('')

    # 妖债
    if demons:
        lines.append('## 妖债预警（溢价>100% 或 价格>800）')
        lines.append('')
        lines.append('| 转债 | 溢价% | 价格 | 类型 |')
        lines.append('|------|:----:|-----:|------|')
        for d in sorted(demons, key=lambda x: x.price, reverse=True):
            reason = '超高溢价' if d.premium > DEMON_PREMIUM else '超高价'
            lines.append(f'| {d.name or d.code} | {d.premium:.0f} | {d.price:.0f} | {reason} |')
        lines.append('')

    # 元信息
    lines.append('---')
    lines.append(f'*数据源: 通达信 V7.73 | 快照: {breadth.snapshot_time} | '
                 f'V9 候选+概念双向确认 | 自动生成*')

    return '\n'.join(lines)


# ============================================================
# 全局缓存引用
# ============================================================
CONCEPT_HEAT_CACHE = {}


# ============================================================
# 主函数
# ============================================================

def main():
    global CONCEPT_HEAT_CACHE

    print('=' * 60)
    print(f'  可转债日报 — TDX 流水线')
    print(f'  启动: {datetime.now().strftime("%H:%M:%S")}')
    print('=' * 60)

    # 1. 连接 TDX
    print('\n[1/6] 连接通达信...')
    script_path = os.path.join(SCRIPT_DIR, 'tdx_pipeline.py')
    fetcher = TDXFetcher(script_path=script_path)

    # 2. 获取可转债数据
    print('[2/6] 获取可转债数据...')
    t0 = time.time()
    all_cbs, fail_count = fetcher.fetch_all_cb()
    print(f'  {len(all_cbs)}只 (失败{fail_count}) ({time.time()-t0:.1f}s)')

    # 补充名称（先 push2，筛选后再对有效CB做 TDX 补充）
    n1 = resolve_names(all_cbs, use_tdx=False)
    print(f'  名称: push2覆盖{n1}只')

    # 3. 筛选 + 妖债分离
    print('[3/6] 筛选...')
    valid, demons = [], []
    for cb in all_cbs:
        if cb.price <= 0:
            continue
        if is_demon(cb.premium, cb.price):
            demons.append(cb)
        elif cb.scale < MAX_SCALE and cb.premium < MAX_PREMIUM:
            valid.append(cb)

    print(f'  正常: {len(valid)}只 | 妖债: {len(demons)}只 | '
          f'规模<{MAX_SCALE}亿 & 溢价<{MAX_PREMIUM}%')

    # 对有效CB+妖债用 TDX 补充名称
    n2 = resolve_names(valid + demons, use_tdx=True)
    if n2:
        print(f'  名称: TDX补充{n2}只')

    # 3.5 加载昨日盘后数据（V8: 纯开盘已知因子）
    print('[3.5/6] 加载昨日盘后数据...')
    n3 = fetcher.load_yesterday_data(all_cbs)
    yest_valid = sum(1 for cb in valid if cb.yest_amount > 0)
    print(f'  昨日数据: {n3}只 | 候选池覆盖: {yest_valid}/{len(valid)}')

    # 4. 市场广度 + 行情分类
    print('[4/6] 市场广度...')
    breadth = fetcher.fetch_market_breadth()
    print(f'  涨 {breadth.up_count}/{breadth.down_count} '
          f'| 涨停 {breadth.limit_up_count} '
          f'| 上证{breadth.sh_pct:+.2f}% 深证{breadth.sz_pct:+.2f}%')

    snap = MarketSnapshot(
        snapshot_time=breadth.snapshot_time,
        up_count=breadth.up_count,
        down_count=breadth.down_count,
        limit_up_count=breadth.limit_up_count,
        limit_down_count=breadth.limit_down_count,
        index_change_pct=breadth.sh_pct,
        volume_ratio=1.0,
    )
    classifier = MarketClassifier()
    result = classifier.classify(snap)
    print(f'  行情: {result.market_type.value}型 - {result.description}')
    print(f'  策略: {MARKET_CONFIG[result.market_type]["philosophy"]}')

    mconfig = MARKET_CONFIG[result.market_type]
    cand_w = mconfig["candidate_weight"]
    conc_w = mconfig["concept_weight"]

    # 5. 概念分
    print(f'[5/6] 概念评分 ({mconfig["name"]}策略)...')
    concept_map, concept_heat = load_concept_cache()
    CONCEPT_HEAT_CACHE = concept_heat
    extend_concept_cache(all_cbs, concept_map, fetcher)
    concept_scores = build_concept_scores(all_cbs, concept_map, concept_heat, fetcher)
    have_concepts = sum(1 for c in all_cbs if c.concepts)
    print(f'  i问财覆盖: {len(concept_map)}只 | TDX补充: {have_concepts}只')

    # 6. 候选分 (行情自适应)
    print(f'[6/6] 候选评分...')
    compute_candidate_scores_tdx(valid, result.market_type)
    w_str = " ".join(f"{k}={v:.0%}" for k, v in mconfig["score_weights"].items())
    print(f'  权重: {w_str}')

    # 最终分: V8候选分 + 概念分 (V9: 结构×方向 双向确认)
    # 概念权重由行情类型决定: A/B强势→追热点(30%) C均衡→各半 D/E/F弱势→重质量(概念≤20%)
    for cb in valid:
        conc = concept_scores.get(cb.code, 40)
        cb.final_score = round(cb.candidate_score * cand_w + conc * conc_w, 1)
        cb._concept_score = conc  # 暂存用于日报

    valid.sort(key=lambda x: x.final_score, reverse=True)

    # TOP10
    print(f'\n{"="*60}')
    print(f'TOP10:')
    for i, cb in enumerate(valid[:10], 1):
        print(f'  {i:2}. {cb.name or cb.code:8s} '
              f'score={cb.final_score:.1f} '
              f'prem={cb.premium:.1f}% scale={cb.scale:.2f}亿 price={cb.price:.0f}')

    # 生成日报
    report = generate_report(valid, demons, breadth,
                             result.market_type, concept_scores, concept_map)

    report_file = os.path.join(REPORT_DIR,
                               f'日报_TDX_{datetime.now().strftime("%Y-%m-%d_%H%M")}.md')
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)

    # 断开连接
    fetcher.close()

    print(f'\n✓ 日报已保存: {report_file}')
    print(f'✓ 完成 ({datetime.now().strftime("%H:%M:%S")})')

    return valid, demons, breadth, report_file


if __name__ == '__main__':
    main()
