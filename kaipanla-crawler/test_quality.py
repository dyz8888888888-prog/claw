#!/usr/bin/env python3
"""快速测试 kaipanla-crawler 数据质量"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(__file__))
from kaipanla_crawler import KaipanlaCrawler

crawler = KaipanlaCrawler()

def test(name, func, *args, **kwargs):
    t0 = time.time()
    try:
        data = func(*args, **kwargs)
        elapsed = time.time() - t0
        print(f"[OK {elapsed:.1f}s] {name}")
        return data
    except Exception as e:
        elapsed = time.time() - t0
        print(f"[FAIL {elapsed:.1f}s] {name}: {e}")
        return None

print("=" * 60)
print("开盘啦数据爬虫 - 快速测试")
print("=" * 60)

# 1. 实时数据 (should be fast)
print("\n--- 实时接口 ---")
mood = test("市场情绪", crawler.get_realtime_market_mood, timeout=15)
if mood:
    print(f"  上涨:{mood.get('上涨家数')} 下跌:{mood.get('下跌家数')} 涨停:{mood.get('涨停家数')} 跌停:{mood.get('跌停家数')}")

lud = test("实际涨跌停", crawler.get_realtime_actual_limit_up_down, timeout=15)
if lud:
    print(f"  实际涨停:{lud.get('actual_limit_up')} 实际跌停:{lud.get('actual_limit_down')}")

idx = test("指数列表", crawler.get_realtime_index_list, timeout=15)
if idx and idx.get('indexes'):
    for i in idx['indexes'][:4]:
        print(f"  {i.get('name')}: {i.get('value'):.2f} ({i.get('change_pct'):+.2f}%)")

# 2. 连板实时数据
print("\n--- 连板梯队 ---")
all_boards = test("全市场连板-实时", crawler.get_market_limit_up_ladder, timeout=15)
if all_boards:
    stats = all_boards.get('statistics', {})
    print(f"  总涨停:{stats.get('total_limit_up')} 最高:{stats.get('max_consecutive')}连板")
    print(f"  分布: {stats.get('ladder_distribution')}")
    ladder = all_boards.get('ladder', {})
    for k in sorted(ladder.keys(), reverse=True)[:3]:
        v = ladder[k]
        names = [s.get('stock_name','') for s in v[:5]]
        print(f"  {k}连板({len(v)}只): {', '.join(names)}")

# 3. 板块排行 (历史)
print("\n--- 板块排行(历史) ---")
sector = test("板块排行-0619", crawler.get_sector_ranking, "2026-06-19", timeout=30)
if sector and sector.get('sectors'):
    print(f"  板块数:{len(sector['sectors'])}")
    for s in sector['sectors'][:5]:
        print(f"  {s['sector_name']}: {s['stock_count']}只涨停")
        for st in s['stocks'][:2]:
            print(f"    {st['股票代码']} {st['股票名称']} {st.get('涨停原因','')}")

# 4. 异动个股
print("\n--- 异动个股 ---")
ab = test("异动个股-实时", crawler.get_abnormal_stocks, timeout=15)
if ab is not None and not ab.empty:
    print(f"  异动数:{len(ab)}")
    for _, row in ab.head(5).iterrows():
        print(f"  {row.get('股票名称')}: {row.get('异动类型')} ({row.get('涨跌幅'):+.2f}%)")

# 5. 百日新高
print("\n--- 百日新高 ---")
nh = test("百日新高-0619", crawler.get_new_high_data, "2026-06-19", timeout=30)
if nh is not None:
    print(f"  0619新增: {nh}")

# 6. 板块资金
print("\n--- 板块资金 ---")
cap = test("板块资金-AI应用-实时", crawler.get_sector_capital_data, "803023", timeout=15)
if cap:
    print(f"  成交额:{cap.get('turnover',0)/1e8:.2f}亿")
    print(f"  主力净额:{cap.get('main_net_inflow',0)/1e8:.2f}亿")
    print(f"  涨跌幅:{cap.get('change_pct',0):+.2f}%")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
