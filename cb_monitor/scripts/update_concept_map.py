#!/usr/bin/env python3
"""
概念映射更新脚本 — 每日运行，刷新 cb_concept_map.json

数据源: pywencai (i问财) 查询每只可转债正股的概念标签
输出:   C:/Users/DYZ/WorkBuddy/Claw/可转债日报/cb_concept_map.json

用法:   python update_concept_map.py
      python update_concept_map.py --incremental  # 仅更新新增转债
"""

import json
import os
import time
import sys
import logging
import argparse

import akshare as ak
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_PATH = r'C:\Users\DYZ\WorkBuddy\Claw\可转债日报\cb_concept_map.json'


def get_active_bonds() -> dict[str, dict]:
    """获取当前活跃可转债列表 (代码 → name, stock_code, stock_name)"""
    try:
        df = ak.bond_zh_cov()
        if df is None or df.empty:
            logger.error("bond_zh_cov() 返回空")
            return {}
    except Exception as e:
        logger.error(f"获取转债列表失败: {e}")
        return {}

    bonds = {}
    for _, row in df.iterrows():
        code = str(row.get('债券代码', '')).strip()
        code_num = ''.join(c for c in code if c.isdigit())[-6:] if code else ''
        if not code_num or len(code_num) != 6:
            continue
        bp = float(row.get('债现价', 0) or 0)
        # 跳过占位符 (price≈100 的退市/未上市标的)
        if 99.5 <= bp <= 100.5:
            continue

        bonds[code_num] = {
            'name': str(row.get('债券简称', '')),
            'stock_code': str(row.get('正股代码', '')),
            'stock_name': str(row.get('正股简称', '')),
        }
    logger.info(f"活跃转债: {len(bonds)} 只")
    return bonds


def load_existing_map() -> dict:
    """加载已有概念映射"""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def query_concepts_pywencai(stock_code: str, stock_name: str) -> list[str]:
    """
    通过 pywencai 查询正股的概念标签
    返回: ["概念1", "概念2", ...]
    """
    try:
        import pywencai
    except ImportError:
        logger.warning("pywencai 未安装, 跳过概念查询")
        return []

    try:
        # 用问财查询: 正股代码 + 所属概念
        query = f"{stock_code} 所属概念"
        result = pywencai.get(query=query, loop=True)
        if result is None or result.empty:
            return []

        # 提取概念列 (通常在列名中含"概念"或"题材")
        for col in result.columns:
            if '概念' in col or '题材' in col or '板块' in col:
                val = result.iloc[0][col]
                if isinstance(val, str) and val:
                    concepts = [c.strip() for c in val.split(';') if c.strip()]
                    return concepts
                if isinstance(val, list):
                    return [str(c).strip() for c in val if str(c).strip()]
        return []
    except Exception as e:
        logger.debug(f"{stock_code} {stock_name} 概念查询失败: {e}")
        return []


def update(force_full: bool = False):
    """主更新逻辑"""
    existing = load_existing_map()
    bonds = get_active_bonds()

    new_count = 0
    updated_count = 0
    skip_count = 0

    for code_num, info in bonds.items():
        sc = info['stock_code']
        sn = info['stock_name']

        # 增量模式: 已有概念数据的跳过
        if not force_full and code_num in existing and existing[code_num].get('concepts'):
            skip_count += 1
            continue

        logger.info(f"查询: {code_num} {info['name']} → {sc} {sn}")
        concepts = query_concepts_pywencai(sc, sn)

        if concepts:
            if code_num not in existing:
                new_count += 1
            else:
                updated_count += 1

            existing[code_num] = {
                'name': info['name'],
                'stock_name': sn,
                'concepts': concepts,
                'concept_count': len(concepts),
            }
        else:
            # 查询失败也保留基本信息
            if code_num not in existing:
                existing[code_num] = {
                    'name': info['name'],
                    'stock_name': sn,
                    'concepts': [],
                    'concept_count': 0,
                }

        # 每 20 只休息一下，避免问财限流
        if (new_count + updated_count) % 20 == 0 and (new_count + updated_count) > 0:
            time.sleep(3)

    # 写入文件
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    logger.info(f"更新完成: 新增 {new_count}, 更新 {updated_count}, 跳过 {skip_count}, 总计 {len(existing)} 只")
    return existing


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='更新可转债概念映射')
    parser.add_argument('--full', action='store_true', help='全量更新 (默认增量)')
    parser.add_argument('--test', type=str, nargs='?', const='123258', help='测试单只转债 (默认胜蓝转02)')
    args = parser.parse_args()

    if args.test:
        # 测试模式
        code = args.test
        bonds = get_active_bonds()
        info = bonds.get(code)
        if info:
            concepts = query_concepts_pywencai(info['stock_code'], info['stock_name'])
            print(f"{code} {info['name']} ({info['stock_code']} {info['stock_name']})")
            print(f"概念: {concepts}")
        else:
            print(f"未找到转债: {code}")
    else:
        update(force_full=args.full)
