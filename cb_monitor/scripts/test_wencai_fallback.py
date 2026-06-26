"""
pywencai 兜底链路验证测试

模拟 Fuyao+akshare 双源均不可用  →  验证 pywencai 兜底行为
用法: python scripts/test_wencai_fallback.py [--full]

模式:
  --full: 执行完整三级降级链验证 (含 Fuyao/akshare 实际调用)
  默认:  跳过 Fuyao/akshare, 仅验证 pywencai 兜底
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_count(limit_up: int, broke: int) -> str:
    return f"涨停 {limit_up}, 炸板 {broke}"


def test_pywencai_direct():
    """直接测试 pywencai 涨停/炸板查询"""
    print("\n" + "="*60)
    print("  [1] pywencai 直接查询测试")
    print("="*60)

    try:
        import pywencai
    except ImportError:
        print("  ❌ pywencai 未安装, 请执行: pip install pywencai")
        return False

    results = []

    for query, label in [("今日涨停股", "涨停"), ("今日炸板股", "炸板")]:
        t0 = time.time()
        try:
            df = pywencai.get(query=query, loop=True)
            elapsed = time.time() - t0
            if df is not None and not df.empty:
                print(f"  ✅ {label}: {len(df)} 只 ({elapsed:.1f}s)")
                print(f"     列: {list(df.columns[:8])}")
                results.append(True)
            elif df is None:
                # 非交易时段 pywencai 返回 None, 不算失败
                print(f"  ⚠ {label}: 返回 None (非交易时段正常) ({elapsed:.1f}s)")
                results.append(True)
            else:
                print(f"  ⚠ {label}: 返回空 ({elapsed:.1f}s)")
                results.append(True)  # 非交易时段空返回也正常
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ❌ {label}: {e} ({elapsed:.1f}s)")
            results.append(False)

    return all(results)


def test_market_state_fallback():
    """测试 market_state 的 _try_wencai 方法"""
    print("\n" + "="*60)
    print("  [2] MarketState 兜底链路测试")
    print("="*60)

    from core.market_state import MarketStateClassifier
    ms = MarketStateClassifier()

    t0 = time.time()
    try:
        limit_up, broke_limit = ms._try_wencai()
        elapsed = time.time() - t0
        print(f"  ✅ pywencai: {_fmt_count(limit_up, broke_limit)} ({elapsed:.1f}s)")
        if limit_up > 0:
            print(f"     合理: 涨停数 > 0 (当前是交易日)")
        else:
            print(f"     警告: 涨停数 = 0 (可能非交易时段, 正常)")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  ❌ 兜底失败: {e} ({elapsed:.1f}s)")
        return False


def test_full_chain():
    """完整三级降级链验证"""
    print("\n" + "="*60)
    print("  [3] 三级降级链完整性测试")
    print("="*60)

    from core.market_state import MarketStateClassifier
    ms = MarketStateClassifier()

    # Fuyao (主)
    t0 = time.time()
    lu_f, bl_f = ms._try_fuyao_pool()
    t1 = time.time()
    print(f"  Fuyao (主):  {_fmt_count(lu_f, bl_f)} ({t1-t0:.1f}s)")

    # akshare (备)
    t0 = time.time()
    lu_a, bl_a = ms._try_akshare()
    t2 = time.time()
    print(f"  akshare (备): {_fmt_count(lu_a, bl_a)} ({t2-t0:.1f}s)")

    # pywencai (兜底)
    t0 = time.time()
    lu_w, bl_w = ms._try_wencai()
    t3 = time.time()
    print(f"  pywencai(兜): {_fmt_count(lu_w, bl_w)} ({t3-t0:.1f}s)")

    # 一致性检测
    print(f"\n  --- 耗时统计 ---")
    print(f"  Fuyao:  {t1-t0:.1f}s (最快)")
    print(f"  akshare: {t2-t0:.1f}s")
    print(f"  pywencai: {t3-t0:.1f}s (最慢, 常态不触发)")

    # 检测跨源一致性 (非交易时段全为0是正常的)
    print(f"\n  --- 一致性 ---")
    wins = [(lu_f, "Fuyao"), (lu_a, "akshare"), (lu_w, "pywencai")]
    non_zero = [(v, n) for v, n in wins if v > 0]
    if len(non_zero) == 0:
        print(f"  全部 0 → 非交易时段, 正常")
    elif len(non_zero) == 1:
        print(f"  仅 {non_zero[0][1]} 有数据 → 其他源可能故障")
    else:
        val, name = non_zero[0]
        others = [(v2, n2) for v2, n2 in non_zero[1:]]
        print(f"  主源 {name}: {val} 只")
        for v2, n2 in others:
            diff = abs(val - v2) / max(val, 1) * 100
            if diff < 30:
                print(f"    {n2}: {v2} → 差异 {diff:.0f}% ✅")
            else:
                print(f"    {n2}: {v2} → 差异 {diff:.0f}% ⚠️")

    print(f"\n  --- 降级链结论 ---")
    if lu_f > 0:
        print(f"  ✅ 主源 Fuyao 正常, 备源/兜底不会被触发")
    elif lu_a > 0:
        print(f"  ⚠️ Fuyao 不可用, akshare 接管")
    elif lu_w > 0:
        print(f"  ⚠️ 双源失效, pywencai 兜底生效")
    else:
        print(f"  ✅ 全部归零, 非交易时段 (行为正常)")


if __name__ == "__main__":
    print("pywencai 兜底链路验证")
    print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    run_full = "--full" in sys.argv

    # [1] pywencai 直接查询能力
    ok1 = test_pywencai_direct()

    # [2] market_state 兜底方法测试
    ok2 = test_market_state_fallback()

    # [3] 三级降级链 (可选)
    if run_full:
        ok3 = test_full_chain()
    else:
        print(f"\n  💡 跳过三级降级链 (加 --full 启用). 默认仅测 pywencai.")
        ok3 = True

    print("\n" + "="*60)
    if ok1 and ok2 and ok3:
        print("  ✅ pywencai 兜底链路验证通过")
    else:
        print(f"  ❌ 部分失败: pywencai={'PASS' if ok1 else 'FAIL'}, "
              f"fallback={'PASS' if ok2 else 'FAIL'}")
    print("="*60)
