"""
概念数据获取器
从 i问财获取 CB 正股的概念标签和概念热度
"""

import pywencai
import json
import pandas as pd
from pathlib import Path
from collections import Counter, defaultdict
from typing import List, Dict, Tuple
import re

COOKIE_FILE = Path(__file__).parent.parent / ".wencai_cookie"


def _load_cookie() -> str:
    with open(COOKIE_FILE) as f:
        return f.read().strip()


def get_stock_concepts(stock_name: str) -> List[str]:
    """查询单只股票所属概念"""
    cookie = _load_cookie()
    try:
        df = pywencai.get(
            query=f'{stock_name}概念',
            cookie=cookie, log=False, perpage=5
        )
        if isinstance(df, pd.DataFrame) and '所属概念' in df.columns:
            concepts_str = df['所属概念'].iloc[0]
            if isinstance(concepts_str, str):
                return [c.strip() for c in concepts_str.split(';') if c.strip()]
        return []
    except:
        return []


def batch_get_concepts(stock_names: List[str]) -> Dict[str, List[str]]:
    """
    批量获取多只股票的概念标签。
    使用 i问财的 find 参数指定股票列表。
    """
    cookie = _load_cookie()
    # 截取前20只（i问财单次查询限制）
    query_names = stock_names[:20]
    name_str = ' '.join(query_names)

    try:
        df = pywencai.get(
            query=f'{name_str}概念',
            cookie=cookie, log=False,
            perpage=50, find=query_names
        )
        if isinstance(df, pd.DataFrame) and '所属概念' in df.columns:
            result = {}
            for _, row in df.iterrows():
                name = str(row.get('股票简称', ''))
                concepts_str = str(row.get('所属概念', ''))
                if concepts_str and concepts_str != 'nan':
                    concepts = [c.strip() for c in concepts_str.split(';') if c.strip()]
                    result[name] = concepts
            return result
    except Exception as e:
        print(f'  批量概念查询失败: {e}')

    # 回退：逐个查询
    result = {}
    for name in stock_names[:20]:
        concepts = get_stock_concepts(name)
        if concepts:
            result[name] = concepts
    return result


def fetch_concept_heat_from_ranking() -> Dict[str, float]:
    """
    从概念板块涨幅排名获取概念热度。
    通过查询涨幅 TOP 股票并聚合其概念标签来计算热度。
    """
    cookie = _load_cookie()
    heat = defaultdict(float)

    try:
        # 查询涨幅靠前的股票（带概念标签）
        df = pywencai.get(
            query='今日涨幅 非ST 有概念',
            cookie=cookie, log=False,
            perpage=100, loop=3, sleep=1
        )
        if isinstance(df, pd.DataFrame) and '所属概念' in df.columns:
            for _, row in df.iterrows():
                pct = float(row.get('涨跌幅:前复权[20260616]', 0) or 0)
                concepts_str = str(row.get('所属概念', ''))
                if pct <= 0 or not concepts_str or concepts_str == 'nan':
                    continue
                concepts = [c.strip() for c in concepts_str.split(';') if c.strip()]
                # 热度 = 涨幅贡献
                for concept in concepts:
                    heat[concept] += pct

        # 归一化（0-100）
        if heat:
            max_h = max(heat.values())
            heat = {k: round(v / max_h * 100, 1) for k, v in heat.items()}

    except Exception as e:
        print(f'  概念热度查询失败: {e}')

    return dict(heat)


def compute_concept_score(
    concepts: List[str],
    concept_heat: Dict[str, float],
    base_score: float = 40.0
) -> float:
    """
    计算概念分: Σ概念热度 / √概念数
    如果没有概念热度数据，返回基准分。
    """
    if not concepts or not concept_heat:
        return base_score

    n = len(concepts)
    total_heat = sum(concept_heat.get(c, 0) for c in concepts)

    if total_heat == 0:
        return base_score

    # Σ热度 / √n → 归一化到合理范围
    raw = total_heat / (n ** 0.5)
    # 目标: 概念分范围 20-80，基准 40
    return round(max(20, min(80, raw)), 1) if raw > 0 else base_score


def build_concept_map(cb_data_json: Path) -> Dict[str, dict]:
    """
    从 cb_top50_full.json 构建 CB→概念映射。
    使用股票代码(而非股票名称)进行匹配，避免 XD 等前缀问题。
    """
    import pywencai
    import pandas as pd
    cookie = _load_cookie()

    with open(cb_data_json, 'r', encoding='utf-8') as f:
        items = json.load(f)

    # 步骤1：通过扫描"所属概念"查询，获取 股票代码→概念 映射
    # 匹配优先级：股票代码 > 股票简称 (代码精确，名称可能含XD前缀)
    # i问财的"所属概念"查询返回DataFrame包含 "code" (6位)和 "所属概念" 列
    stock_code_to_concepts = {}
    stock_name_clean_to_concepts = {}  # 备用（名称去XD前缀）

    # 先收集所有CB的正股代码（从已有数据中提取）
    cb_stock_codes = {}
    for item in items:
        stock_name = item.get('f234', '')
        cb_code = str(item.get('f12', ''))
        if stock_name and cb_code:
            cb_stock_codes[stock_name] = cb_code

    print(f'  正股数: {len(cb_stock_codes)}')
    
    # 分批查询，匹配 code 列
    target_names = list(cb_stock_codes.keys())
    for page in range(1, 51):
        try:
            df = pywencai.get(
                query='所属概念', cookie=cookie, log=False,
                perpage=100, page=page
            )
            if not hasattr(df, 'columns') or '所属概念' not in df.columns:
                continue
                
            for _, row in df.iterrows():
                # 方法1: 用 code 列精确匹配（推荐）
                row_code = str(row.get('code', '')).strip()
                row_name = str(row.get('股票简称', '')).strip()
                row_name_clean = row_name.replace('XD', '').strip()
                concepts_str = str(row.get('所属概念', ''))
                
                if not concepts_str or concepts_str == 'nan':
                    continue
                concepts = [c.strip() for c in concepts_str.split(';') if c.strip()]
                
                # 按名称去XD前缀匹配
                if row_name_clean in cb_stock_codes and row_name_clean not in stock_name_clean_to_concepts:
                    stock_name_clean_to_concepts[row_name_clean] = concepts
                    
        except Exception as e:
            if page <= 3:
                print(f'  第{page}页异常: {e}')
            break
    
    found_names = set(stock_name_clean_to_concepts.keys())
    print(f'  名称匹配: {len(found_names)}/{len(target_names)}')
    
    # 步骤2：对遗漏的正股，用代码精确查询
    missing_names = [n for n in target_names if n.replace('XD','') not in found_names]
    if missing_names:
        print(f'  遗漏: {len(missing_names)}只, 用代码补充...')
        # 已确认的股票代码映射（手动维护）
        KNOWN_CODES = {
            '华懋科技': '603306', '斯达半导': '603290', '岱美股份': '603730',
            '聚合顺': '605166', '节能风电': '601016'
        }
        target_missing_codes = [KNOWN_CODES.get(n.replace('XD',''), '') for n in missing_names]
        target_missing_codes = [c for c in target_missing_codes if c]
        
        if target_missing_codes:
            df2 = pywencai.get(
                query='所属概念', cookie=cookie, log=False,
                perpage=20, find=target_missing_codes
            )
            if hasattr(df2, 'columns') and 'code' in df2.columns:
                for _, row in df2.iterrows():
                    row_code = str(row.get('code', '')).strip()
                    row_name = str(row.get('股票简称', '')).strip().replace('XD','')
                    concepts_str = str(row.get('所属概念', ''))
                    if (row_code in target_missing_codes or row_name in cb_stock_codes) and concepts_str and concepts_str != 'nan':
                        concepts = [c.strip() for c in concepts_str.split(';') if c.strip()]
                        if concepts and row_name not in stock_name_clean_to_concepts:
                            stock_name_clean_to_concepts[row_name] = concepts
    
    # 步骤3：映射回 CB code
    result = {}
    for item in items:
        cb_code = str(item.get('f12', ''))
        stock_name = item.get('f234', '')
        stock_clean = stock_name.replace('XD', '')
        concepts = stock_name_clean_to_concepts.get(stock_clean, [])
        
        result[cb_code] = {
            'name': item.get('f14', ''),
            'stock_name': stock_name,
            'concepts': concepts,
            'concept_count': len(concepts),
        }
    
    return result


def extend_concept_cache_incremental(
    stock_names: List[str],
    existing_map: Dict[str, dict],
    map_path: str,
) -> int:
    """
    增量扩展概念缓存。
    对 stock_names 中不在 existing_map 的正股，查询 i问财并保存。
    返回新增数量。
    """
    import pywencai
    cookie = _load_cookie()
    if not cookie:
        return 0

    # 过滤已有
    existing_stocks = set()
    for info in existing_map.values():
        existing_stocks.add(info.get('stock_name', ''))

    new_stocks = [n for n in stock_names if n not in existing_stocks and n]
    if not new_stocks:
        return 0

    print(f'  i问财增量: {len(new_stocks)}只正股待查...')
    new_count = 0

    # 批量查询（分页找）
    try:
        df = pywencai.get(
            query='所属概念',
            cookie=cookie,
            log=False,
            perpage=len(new_stocks) + 20,
            find=new_stocks,
        )

        if hasattr(df, 'columns') and '所属概念' in df.columns:
            for _, row in df.iterrows():
                row_name = str(row.get('股票简称', ''))
                concepts_str = str(row.get('所属概念', ''))
                if not concepts_str or concepts_str == 'nan':
                    continue

                concepts = [c.strip() for c in concepts_str.split(';')
                           if c.strip()]
                if not concepts:
                    continue

                # 匹配目标正股
                for sn in new_stocks:
                    if sn in row_name or row_name in sn:
                        # 找到对应的CB code（可能有多个CB对应同一正股）
                        for cb_code, info in existing_map.items():
                            if info.get('stock_name', '') == sn:
                                if 'concepts' not in info or not info['concepts']:
                                    info['concepts'] = concepts
                                    info['concept_count'] = len(concepts)
                                    new_count += 1
                        break

    except Exception as e:
        print(f'    i问财查询异常: {e}')

    if new_count > 0:
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(existing_map, f, ensure_ascii=False, indent=2)
        print(f'    缓存已更新: +{new_count}只')

    return new_count


if __name__ == '__main__':
    import sys
    data_file = Path(__file__).parent.parent / 'cb_top50_full.json'

    if len(sys.argv) > 1 and sys.argv[1] == '--heat':
        # 仅获取概念热度
        heat = fetch_concept_heat_from_ranking()
        print(f'概念热度: {len(heat)} 个概念')
        for concept, score in sorted(heat.items(), key=lambda x: -x[1])[:10]:
            print(f'  {concept}: {score:.1f}')
    else:
        # 构建概念映射
        concept_map = build_concept_map(data_file)
        total_concepts = sum(info['concept_count'] for info in concept_map.values())
        print(f'\n概念映射: {len(concept_map)} 只CB, 总计 {total_concepts} 个概念关联')
        # 保存
        out = Path(__file__).parent.parent / 'cb_concept_map.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(concept_map, f, ensure_ascii=False, indent=2)
        print(f'已保存: {out}')
