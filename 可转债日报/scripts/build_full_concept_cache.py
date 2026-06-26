"""
全量 CB 正股概念缓存构建器 V3
- 过滤停牌CB（now=0 且 vol=0）
- 正股代码来自 get_kzz_info['HSCode']
- CB/正股名称从 push2 缓存补充
- i问财批量查询（200只/批，省API）
"""

import sys, os, io, json, time
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, 'D:/tdx/PYPlugins/user')
from tqcenter import tq
import pywencai

PROJECT_DIR = Path(__file__).parent.parent
CACHE_PATH = PROJECT_DIR / 'cb_concept_map.json'
PUSH2_PATH = PROJECT_DIR / 'cb_top50_full.json'
COOKIE_PATH = PROJECT_DIR / '.wencai_cookie'
BATCH_SIZE = 40   # 超过40 i问财会截断部分结果


def load_cookie() -> str:
    with open(COOKIE_PATH) as f:
        return f.read().strip()


def load_push2_names() -> Dict[str, str]:
    """从 push2 缓存加载 CB名称: {123059: '银信转债'}"""
    names = {}
    if PUSH2_PATH.exists():
        with open(PUSH2_PATH, 'r', encoding='utf-8') as f:
            items = json.load(f)
        for item in items:
            code = str(item.get('f12', ''))
            name = str(item.get('f14', ''))
            if code and name:
                names[code] = name
    return names


def load_existing_cache() -> Dict[str, dict]:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def get_active_cb_stocks(push2_names: Dict[str, str]) -> Dict[str, dict]:
    """
    Step 1: TDX 扫描活跃 CB 的正股代码。
    停牌: Now=0 且 Volume=0
    返回: {cb_code: {'stock_code': '300231', 'cb_name': '银信转债'}}
    """
    print('Step 1/3: TDX 扫描活跃 CB 正股...')
    import io as _io

    _stdout = sys.stdout
    sys.stdout = _io.StringIO()
    tq.initialize('conc_v3')
    sys.stdout = _stdout

    cb_list = tq.get_stock_list('32')
    print(f'  CB列表: {len(cb_list)}')

    # 1a. 批量行情 → 停牌过滤
    sys.stdout = _io.StringIO()
    pv_raw = tq.get_pricevol(stock_list=cb_list)
    sys.stdout = _stdout

    active_codes = set()
    halted = 0
    for cb_full in cb_list:
        code = cb_full[:6]
        pv = pv_raw.get(cb_full)
        if pv:
            now = float(pv.get('Now', 0))
            vol = float(pv.get('Volume', 0))
            if now <= 0 and vol <= 0:
                halted += 1
            else:
                active_codes.add(code)
        else:
            halted += 1
    print(f'  活跃: {len(active_codes)} | 停牌: {halted}')

    # 1b. KZZ信息（仅活跃）
    result = {}
    fail, done = 0, 0
    for cb_full in cb_list:
        code = cb_full[:6]
        if code not in active_codes:
            continue

        sys.stdout = _io.StringIO()
        try:
            kzz = tq.get_kzz_info(stock_code=cb_full)
            sys.stdout = _stdout
            hs = str(kzz.get('HSCode', '')).zfill(6)
            if hs and hs != '000000':
                result[code] = {
                    'stock_code': hs,
                    'cb_name': push2_names.get(code, code),
                }
            else:
                fail += 1
        except:
            sys.stdout = _stdout
            fail += 1

        done += 1
        if done % 100 == 0:
            print(f'  进度: {done}/{len(active_codes)}')

    tq.close()
    print(f'  有效正股: {len(result)} | 无正股: {fail}')
    return result


def batch_query_concepts(stock_codes: List[str], cookie: str) -> Dict[str, List[str]]:
    """
    i问财批量查：用 find 参数传入股票代码。
    返回: {stock_code: [概念1, ...]}
    """
    result = {}
    total = len(stock_codes)

    for batch_start in range(0, total, BATCH_SIZE):
        batch = stock_codes[batch_start:batch_start + BATCH_SIZE]

        try:
            df = pywencai.get(
                query='所属概念',
                cookie=cookie,
                log=False,
                perpage=BATCH_SIZE + 50,
                find=batch,
            )

            if hasattr(df, 'columns') and '所属概念' in df.columns:
                batch_set = set(batch)
                for _, row in df.iterrows():
                    rc = str(row.get('code', '')).strip()
                    concepts_str = str(row.get('所属概念', ''))
                    if rc in batch_set and concepts_str and concepts_str != 'nan':
                        result[rc] = [c.strip() for c in concepts_str.split(';') if c.strip()]

        except Exception as e:
            print(f'  批量异常 [{batch_start}]: {e}')
            for sc in batch:
                try:
                    df = pywencai.get(
                        query='所属概念', cookie=cookie, log=False,
                        perpage=5, find=[sc],
                    )
                    if hasattr(df, 'columns') and '所属概念' in df.columns:
                        for _, row in df.iterrows():
                            rc = str(row.get('code', '')).strip()
                            cs = str(row.get('所属概念', ''))
                            if rc == sc and cs and cs != 'nan':
                                result[rc] = [c.strip() for c in cs.split(';') if c.strip()]
                    time.sleep(0.3)
                except:
                    pass

        bn = batch_start // BATCH_SIZE + 1
        tb = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f'  i问财 [{bn}/{tb}]: {min(batch_start+BATCH_SIZE,total)}/{total} | 已获{len(result)}只')

    return result


def build(incremental: bool = True):
    cookie = load_cookie()
    if not cookie:
        print('错误: 未找到 i问财 Cookie')
        return

    push2_names = load_push2_names()
    print(f'push2名称缓存: {len(push2_names)} 只')

    # 1. TDX 扫描
    t0 = time.time()
    cb_stocks = get_active_cb_stocks(push2_names)
    t1 = time.time()
    print(f'TDX耗时: {t1-t0:.0f}s\n')

    # 2. 对比缓存
    existing = load_existing_cache() if incremental else {}
    print(f'Step 2/3: 对比缓存')
    print(f'  现有: {len(existing)} | 活跃: {len(cb_stocks)}')

    if incremental:
        missing = {}
        for code, info in cb_stocks.items():
            if code not in existing or not existing[code].get('concepts'):
                # 正股名已在其他CB缓存中的跳过（正股概念可复用）
                stock_code = info['stock_code']
                already = any(
                    v.get('stock_code') == stock_code and v.get('concepts')
                    for v in existing.values()
                )
                if not already:
                    missing[code] = info

        if not missing:
            print('  缓存已完整！')
            return
        print(f'  需查询: {len(missing)} 只')
    else:
        missing = cb_stocks
        print(f'  全量重建: {len(missing)} 只')

    # 3. 收集正股代码（去重）
    stock_codes: Set[str] = set()
    for info in missing.values():
        stock_codes.add(info['stock_code'])
    print(f'  去重正股: {len(stock_codes)} 只')

    # 4. i问财查询
    t2 = time.time()
    print(f'\nStep 3/3: i问财批量查询 ({BATCH_SIZE}只/批)...')
    stock_concepts = batch_query_concepts(list(stock_codes), cookie)
    t3 = time.time()
    print(f'  获得: {len(stock_concepts)} 只 | 耗时: {t3-t2:.0f}s')

    # 5. 合并到缓存
    hit, skip = 0, 0
    for code, info in cb_stocks.items():
        sc = info['stock_code']
        if sc in stock_concepts:
            existing[code] = {
                'name': info['cb_name'],
                'stock_code': sc,
                'concepts': stock_concepts[sc],
                'concept_count': len(stock_concepts[sc]),
            }
            hit += 1
        elif code not in existing:
            existing[code] = {
                'name': info['cb_name'],
                'stock_code': sc,
                'concepts': [],
                'concept_count': 0,
            }
            skip += 1

    # 6. 保存
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    with_c = sum(1 for v in existing.values() if v.get('concepts'))
    total_c = sum(v.get('concept_count', 0) for v in existing.values())

    print(f'\n===== 完成 =====')
    print(f'  CB总数: {len(existing)} | 有概念: {with_c} ({with_c/max(len(existing),1)*100:.0f}%)')
    print(f'  概念关联: {total_c} | 新填充: {hit} | 无数据: {skip}')
    print(f'  总耗时: {t3-t0:.0f}s')
    print(f'  {CACHE_PATH}')
    print(f'  增量逻辑: 后续运行仅查新增CB，正股概念自动复用')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--full', action='store_true')
    args = p.parse_args()
    build(incremental=not args.full)
