"""
整合数据表生成器
- 从 TDX 获取全部 CB 数据（名称/价格/溢价/规模/正股）
- 从本地缓存匹配概念
- 输出 Markdown 全量数据表
"""

import sys, os, io, json, time, math
from pathlib import Path

sys.path.insert(0, 'D:/tdx/PYPlugins/user')
from tqcenter import tq

PROJECT_DIR = Path(__file__).parent.parent
CACHE_PATH = PROJECT_DIR / 'cb_concept_map.json'

stderr = sys.stderr


def load_concept_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def main():
    import io as _io
    _stdout = sys.stdout

    sys.stdout = _io.StringIO()
    tq.initialize('datatable')
    sys.stdout = _stdout

    cb_list = tq.get_stock_list('32')
    concept_cache = load_concept_cache()
    print(f'TDX: {len(cb_list)} CB | 缓存: {len(concept_cache)} 只有概念')

    # 批量行情
    sys.stdout = _io.StringIO()
    pv_raw = tq.get_pricevol(stock_list=cb_list)
    sys.stdout = _stdout

    # 批量名称
    names = {}
    for cb_full in cb_list:
        try:
            sys.stdout = _io.StringIO()
            mt = tq.get_match_stkinfo(key_word=cb_full[:6])
            sys.stdout = _stdout
            if mt and len(mt) > 0:
                m = mt[0] if isinstance(mt, list) else mt
                if isinstance(m, dict):
                    names[m.get('Code', '')[:6]] = m.get('Name', '')
        except:
            sys.stdout = _stdout
    print(f'名称: {len(names)} 只')

    # 逐只KZZ数据
    rows = []
    for i, cb_full in enumerate(cb_list):
        code = cb_full[:6]
        pv = pv_raw.get(cb_full)

        # 过滤停牌
        if pv:
            now = float(pv.get('Now', 0))
            vol = float(pv.get('Volume', 0))
            if now <= 0 and vol <= 0:
                continue
        else:
            continue

        sys.stdout = _io.StringIO()
        try:
            kzz = tq.get_kzz_info(stock_code=cb_full)
            sys.stdout = _stdout
            hs = str(kzz.get('HSCode', '')).zfill(6)
            stock_p = float(kzz.get('AGNow', 0))
            premium = float(kzz.get('KZZYj', 999))
            scale = float(kzz.get('RestScope', 0)) / 10000
        except:
            sys.stdout = _stdout
            continue

        # 概念
        concepts = concept_cache.get(code, {}).get('concepts', [])
        top_c = ', '.join(concepts) if concepts else '-'

        rows.append({
            'code': code,
            'name': names.get(code, code),
            'premium': round(premium, 1),
            'scale': scale,
            'stock_code': hs,
            'concept_count': len(concepts),
            'top_concepts': top_c,
        })

    tq.close()

    # 按代码排序
    rows.sort(key=lambda r: r['code'])

    # 输出 MD 表格
    lines = []
    lines.append('# 可转债全量数据表')
    lines.append(f'')
    lines.append(f'**快照时间**: {time.strftime("%Y-%m-%d %H:%M:%S")} | **总计**: {len(rows)} 只（已剔除停牌）')
    lines.append(f'**数据源**: 通达信 TQ + i问财概念缓存')
    lines.append('')
    lines.append('| # | 代码 | 名称 | 溢价% | 规模亿 | 正股代码 | 概念数 | 概念列表 |')
    lines.append('|---|------|------|:-----:|:------:|----------|:-----:|------|')

    for i, r in enumerate(rows, 1):
        lines.append(
            f'| {i} | {r["code"]} | {r["name"]} | '
            f'{r["premium"]:.1f}% | {r["scale"]:.2f} | {r["stock_code"]} | '
            f'{r["concept_count"]} | {r["top_concepts"]} |'
        )

    lines.append('')
    lines.append('---')
    lines.append(f'*数据整合表 | 自动生成*')

    # 保存
    out_path = PROJECT_DIR / f'CB全量数据表_{time.strftime("%Y%m%d_%H%M")}.md'
    content = '\n'.join(lines)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f'\n保存: {out_path}')
    print(f'共 {len(rows)} 只活跃CB')

    # 统计
    with_c = sum(1 for r in rows if r['concept_count'] > 0)
    print(f'有概念: {with_c}/{len(rows)} ({with_c/max(len(rows),1)*100:.0f}%)')


if __name__ == '__main__':
    main()
