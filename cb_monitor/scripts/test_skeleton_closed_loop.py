"""
新交易骨架 — 端到端模拟交易验证

运行时需在 cb_monitor 目录下:
    cd cb_monitor && python scripts/test_skeleton_closed_loop.py
"""
import sys, os, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 从 cb_monitor 目录内导入 (sys.path 已包含 cb_monitor/)
from domain.enums import Regime, TradeMode, HoldingMode, Decision
from domain.models import MarketSnapshot, MarketContext, TradeIntent

from feeds.snapshot_builder import SnapshotBuilder

from context.market_regime import MarketRegimeClassifier, RegimeInput

from context.strategy_router import StrategyRouter

from strategies.volume_follow import VolumeFollowStrategy

from engine.state_machine import TradingStateMachine
from engine.risk_engine import RiskEngine, RiskLimits
from engine.selector import Selector

from execution.exit_rules import ExitRules
from execution.position_manager import PositionManager
from execution.broker_sim import BrokerSim

from ledger.trade_log import TradeLedger

from replay.fill_model import FillModel


def test_closed_loop():
    """完整交易闭环: 发现 → 评估 → 风控 → 进场 → 持仓 → 退出 → 记账"""
    print("=" * 60)
    print("新交易骨架 — 端到端模拟交易验证")
    print("=" * 60)

    now = time.time()

    # ── 1. 构造市场上下文 ──────────────────────
    ri = RegimeInput(
        ts=now, limit_up=60, broken_limit=5,
        up_down_ratio=0.6, promotion_rate=30,
        pool_up_ratio=0.8, turnover_yi=8000,
    )
    mc = MarketRegimeClassifier()
    ctx = mc.classify(ri, TradeMode.PROBE)
    assert ctx.regime == Regime.MILD, f"Expected MILD, got {ctx.regime}"
    print(f"[1] MarketContext: regime={ctx.regime} mode={ctx.trade_mode}")

    # ── 2. 策略路由 ────────────────────────────
    router = StrategyRouter()
    ctx = router.apply_to(ctx)
    assert "volume_follow" in ctx.enabled_strategies
    print(f"[2] StrategyRouter: enabled={ctx.enabled_strategies}")

    # ── 3. 构造快照 + 策略评估 ──────────────────
    cb_row = {
        "code": "123258", "name": "胜蓝转02", "price": 118.5, "pct": 1.2,
        "bid1": 118.45, "ask1": 118.55, "volume": 5000, "amount": 50_000_000,
        "volume_ratio": 4.2, "stock_code": "300843",
    }
    stock_row = {"code": "300843", "name": "胜蓝股份", "price": 25.0, "pct": 3.0}
    meta_row = {"convert_value": 95.0, "premium": 24.2, "issue_scale": 2.5, "redeem_status": "normal"}

    sb = SnapshotBuilder()
    snap = sb.build_one(cb_row, stock_row, meta_row, now)
    assert snap.halt_risk == "safe"

    strategy = VolumeFollowStrategy()
    intent = strategy.evaluate(snap, ctx, {})
    assert intent is not None, "策略应评估出交易意图"
    assert intent.holding_mode == HoldingMode.INTRADAY_FLAT  # 枚举比较
    print(f"[3] evaluate: score={intent.score} code={intent.cb_code} holding_mode={intent.holding_mode}")

    # ── 4. Selector 排序 ────────────────────────
    sel = Selector()
    recs = sel.rank(
        [intent],
        {intent.cb_code: snap},
        ctx,
    )
    assert len(recs) == 1
    print(f"[4] Selector: top score={recs[0].score}")

    # ── 5. RiskEngine 风控检查 ──────────────────
    engine = RiskEngine()
    result = engine.check_new_trade(
        intent, ctx,
        account_ctx={
            "daily_pnl_pct": 0.0,
            "consecutive_losses": 0,
            "used_cb_trade_count": {},
            "available_risk_budget": 1.0,
            "now_ts": now,
        },
        positions=[],
    )
    assert result.status.value == "allow", f"风控应为allow, 实际: {result.status}"
    print(f"[5] RiskEngine: {result.status}")

    # ── 6. StateMachine 状态流转 ────────────────
    sm = TradingStateMachine()
    s1 = sm.transition(now, ctx, False, False, True, False)
    assert s1 == "idle"
    s2 = sm.transition(now, ctx, False, False, True, False)
    assert s2 == "watching"
    s3 = sm.transition(now, ctx, True, False, True, False)
    assert s3 == "entering"
    print(f"[6] StateMachine: DISABLED → IDLE → WATCHING → ENTERING")

    # ── 7. BrokerSim 模拟成交 ───────────────────
    bs = BrokerSim(FillModel())
    fill = bs.submit_entry(intent, snap, qty=10)
    assert fill.filled, f"应成交: {fill.reason}"
    print(f"[7] BrokerSim entry: filled at {fill.fill_price} slip={fill.slippage_cost:.4f}")

    # ── 8. PositionManager 持仓管理 ──────────────
    pm = PositionManager()
    pos = pm.open_from_fill(intent, fill_price=fill.fill_price, qty=fill.fill_qty)
    assert pos.state == "open"
    assert pos.holding_mode == HoldingMode.INTRADAY_FLAT  # 枚举比较
    print(f"[8] Position opened: {pos.position_id} entry={pos.entry_price}")

    # ── 9. 价格上行 + 更新浮盈 ──────────────────
    snap_up = MarketSnapshot(
        ts=now + 60, cb_code="123258", cb_name="胜蓝转02",
        cb_price=120.0, cb_pct=2.5, cb_volume_ratio=2.5, halt_risk="safe",
    )
    pm.update_marks(pos, 120.0)
    assert pos.max_favorable_pct > 0
    print(f"[9] Update marks: price=120.0 max_fav={pos.max_favorable_pct:.2f}%")

    # ── 10. 价格回落触发移动止盈 ────────────────
    snap_down = MarketSnapshot(
        ts=now + 120, cb_code="123258", cb_name="胜蓝转02",
        cb_price=119.2, cb_pct=1.0, cb_bid1=119.1, cb_ask1=119.3,
        cb_volume_ratio=2.0,
    )
    action = ExitRules.for_volume_follow(pos, snap_down, {
        "stop_loss_pct": -1.0,
        "take_profit_pct": 1.5,
        "trail_drawdown_pct": 0.5,
        "max_hold_seconds": 300,
    })
    assert action[0] == "exit", f"应触发移动止盈: {action}"
    print(f"[10] Exit signal: {action[0]} — {action[1]}")

    # ── 11. 平仓结算 ────────────────────────────
    fill_exit = bs.submit_exit(pos, snap_down)
    assert fill_exit.filled
    settle = pm.close_position(
        pos,
        fill_price=fill_exit.fill_price,
        now_ts=now + 121,
        reason=action[1],
    )
    assert settle["pnl_pct"] > 0, f"应盈利, 实际: {settle['pnl_pct']:.4f}"
    print(f"[11] Settled: pnl={settle['pnl_pct']:.2f}% hold={settle['holding_seconds']}s")

    # ── 12. TradeLedger 双轨记账 ────────────────
    db_path = os.path.join(tempfile.gettempdir(), "test_skeleton_loop.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    ledger = TradeLedger(db_path)

    # 候选记录
    ledger.log_candidates(recs)

    # 开仓记录
    ledger.log_entry(pos, now, 118.5, fill.fill_price, ctx.regime.value, ctx.trade_mode.value, intent.reason_text)

    # 平仓记录
    ledger.log_exit(
        pos.position_id,
        now + 121,
        119.2,
        fill_exit.fill_price,
        settle["gross_pnl"],
        settle["pnl_pct"],
        fill_exit.slippage_cost,
        settle["exit_reason"],
        pos.max_favorable_pct,
        pos.max_adverse_pct,
    )

    # 验证
    trades = ledger.get_today_trades()
    assert len(trades) == 1
    perf = ledger.get_strategy_performance("volume_follow", days=1)
    assert perf["win_rate"] == 100.0

    ledger.close()
    os.remove(db_path)

    print(f"[12] TradeLedger: 1 trade logged, win_rate={perf['win_rate']}%")
    print()
    print("=" * 60)
    print("所有 12 步验证通过 — 新骨架闭环完整")
    print("=" * 60)


if __name__ == "__main__":
    test_closed_loop()
