"""
新架构旁路运行器 — 与旧系统并行，不干扰旧链路。

每轮主循环从旧 shared_state 读取快照，
转换为新 MarketSnapshot，经过新管道:
  context → strategies → selector → risk → state_machine → ledger (候选记录)
结果写回 shared_state.sidecar_state，供仪表盘对比。
"""

from __future__ import annotations

import logging
import time
from typing import Any

# 新模块导入 (默认从 cb_monitor/ 目录运行)
from domain.enums import Regime, TradeMode, MachineState, RiskCheck
from domain.models import MarketSnapshot, MarketContext, TradeIntent, CandidateRecord
from context.market_regime import MarketRegimeClassifier, RegimeInput
from context.strategy_router import StrategyRouter
from strategies.volume_follow import VolumeFollowStrategy
from engine.selector import Selector
from engine.risk_engine import RiskEngine, RiskResult
from engine.state_machine import TradingStateMachine
from engine.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class SidecarRunner:
    """新架构旁路 — 只读旧状态，产出新视图"""

    def __init__(self) -> None:
        self._regime_analyzer = MarketRegimeClassifier()
        self._router = StrategyRouter()
        self._selector = Selector()
        self._risk_engine = RiskEngine()
        self._state_machine = TradingStateMachine()
        self._paper_trader = PaperTrader()       # 纸面交易引擎
        self._strategies = [
            VolumeFollowStrategy(),
        ]
        self._ctx: MarketContext | None = None
        self._last_refresh_ts: float = 0.0
        self._candidate_last_ts: dict[str, float] = {}  # 去重: (code+strategy) → last_ts

    # ── 旧快照 → 新 MarketSnapshot 适配 ───

    @staticmethod
    def _convert(snap: Any, ts: float) -> MarketSnapshot:
        """将旧 Snapshot 对象转为新 MarketSnapshot"""
        return MarketSnapshot(
            ts=ts,
            cb_code=str(snap.code or ""),
            cb_name=str(snap.name or ""),
            cb_price=float(snap.trade or 0),
            cb_pct=float(snap.change_pct or 0),
            cb_open=float(getattr(snap, "open", 0) or 0),
            cb_high=float(snap.high or 0),
            cb_low=float(snap.low or 0),
            cb_bid1=float(snap.buy or 0),
            cb_ask1=float(snap.sell or 0),
            cb_bid1_vol=int(getattr(snap, "buy_volume", 0) or 0),
            cb_ask1_vol=int(getattr(snap, "sell_volume", 0) or 0),
            cb_amount=float(snap.amount or 0),
            cb_volume_ratio=float(getattr(snap, "volume_ratio", 0) or 0),
            stock_code=str(getattr(snap, "stock_code", "")),
            stock_name=str(getattr(snap, "stock_name", "")),
            stock_price=float(getattr(snap, "stock_price", 0) or 0),
            stock_pct=float(getattr(snap, "stock_change_pct", 0) or 0),
            premium=float(getattr(snap, "premium_ratio", 0) or 0),
            halt_risk="safe",  # 旧系统无此字段
        )

    @staticmethod
    def _convert_batch(old_snapshots: dict[str, Any], ts: float) -> dict[str, MarketSnapshot]:
        """批量转换旧快照"""
        return {
            code: SidecarRunner._convert(snap, ts)
            for code, snap in old_snapshots.items()
        }

    # ── 从旧状态提取市场情绪输入 ──

    def _build_regime_input(self, old_snapshots: dict[str, Any], now_ts: float) -> RegimeInput | None:
        """从 old_snapshots 提取 RegimeInput，缺失时回退默认"""
        try:
            # 尝试从旧 state 获取 sentiment 数据
            from dashboard.shared_state import state as old_state
            sent = getattr(old_state, "sentiment", {}) or {}
            indicators = {}

            # 尝试读旧的 sentiment_detail
            if hasattr(old_state, "sentiment_detail"):
                sd = old_state.sentiment_detail or {}
                indicators = sd.get("indicators", {}) or {}

            return RegimeInput(
                ts=now_ts,
                limit_up=int(indicators.get("limit_up", indicators.get("intraday_limit_up", 0))),
                broken_limit=int(indicators.get("broken_count", 0)),
                up_down_ratio=float(indicators.get("up_down_ratio", 1.0)),
                promotion_rate=float(indicators.get("promotion_rate", 0)),
                pool_up_ratio=float(indicators.get("pool_ratio", 1.0)),
                turnover_yi=float(indicators.get("turnover_yi", 0)),
            )
        except Exception:
            # 回退：用快照推断
            up = sum(1 for s in old_snapshots.values() if getattr(s, "change_pct", 0) > 0)
            down = sum(1 for s in old_snapshots.values() if getattr(s, "change_pct", 0) <= 0)
            ratio = up / max(down, 1)
            return RegimeInput(ts=now_ts, up_down_ratio=ratio)

    # ── 主入口：被调度器调用 ──────────────────

    def run(self) -> dict:
        """
        运行新架构管道，返回结果摘要。

        调用时机: 每轮 _run_cycle() 末尾 (旧 pipeline 已更新 shared_state)
        """
        now_ts = time.time()
        result = {
            "ts": now_ts,
            "status": "ok",
            "regime": "未知",
            "trade_mode": "未知",
            "enabled_strategies": [],
            "machine_state": "disabled",
            "candidates": 0,
            "intents": 0,
            "error": None,
        }

        try:
            from dashboard.shared_state import state as old_state

            # 1. 读取旧快照并转换
            with old_state._lock:
                old_snaps = dict(old_state.snapshots) if old_state.snapshots else {}
            if not old_snaps:
                result["status"] = "no_data"
                return result

            new_snaps = self._convert_batch(old_snaps, now_ts)

            # 2. 生成市场上下文 (每分钟刷新)
            if now_ts - self._last_refresh_ts > 60:
                ri = self._build_regime_input(old_snaps, now_ts)
                if ri:
                    self._ctx = self._regime_analyzer.classify(ri)
                    self._ctx = self._router.apply_to(self._ctx)
                    self._last_refresh_ts = now_ts
                    logger.info(
                        f"新架构旁路: regime={self._ctx.regime.value} "
                        f"strategies={self._ctx.enabled_strategies}"
                    )

            if self._ctx is None:
                self._ctx = MarketContext(
                    ts=now_ts, regime=Regime.MILD, trade_mode=TradeMode.PROBE,
                    enabled_strategies=("volume_follow",),
                )

            result["regime"] = self._ctx.regime.value
            result["trade_mode"] = self._ctx.trade_mode.value
            result["enabled_strategies"] = list(self._ctx.enabled_strategies)

            # 3. 逐策略评估 (使用纸面账户的真实状态)
            paper_acct = self._paper_trader.account.to_dict()
            all_intents: list[TradeIntent] = []
            for strat in self._strategies:
                if not strat.enabled(self._ctx):
                    continue
                for code, snap in new_snaps.items():
                    intent = strat.evaluate(snap, self._ctx, {
                        "current_positions": self._paper_trader.active_positions,
                        "today_trades": paper_acct["today_trades"],
                        "today_pnl_pct": paper_acct["daily_pnl_pct"],
                        "consecutive_losses": paper_acct["consecutive_losses"],
                    })
                    if intent:
                        all_intents.append(intent)

            result["intents"] = len(all_intents)

            # 4. Selector 排序
            recs: list[CandidateRecord] = []
            if all_intents:
                recs = self._selector.rank(all_intents, new_snaps, self._ctx)
                result["candidates"] = len(recs)

            # 5. 纸面交易引擎接管: 虚拟持仓管理 + 模拟成交 + 记账
            trader_result = self._paper_trader.run(
                self._ctx, new_snaps, all_intents, recs,
            )

            result["trades_opened"] = trader_result.get("trades_opened", 0)
            result["trades_closed"] = trader_result.get("trades_closed", 0)
            result["paper_positions"] = trader_result.get("positions", 0)
            result["paper_account"] = trader_result.get("account", {})
            result["blocked"] = trader_result.get("blocked", "")
            result["top_candidate"] = {
                "code": all_intents[0].cb_code, "score": all_intents[0].score,
                "reason": all_intents[0].reason_text,
            } if all_intents else None

            # 6. 将候选写入 TradeLedger (按 code+strategy 每分钟去重)
            if recs:
                try:
                    from ledger.trade_log import TradeLedger
                    ledger = TradeLedger("data/trade_ledger.db")
                    dedup_recs = []
                    for r in recs:
                        key = f"{r.cb_code}|{r.strategy_id}"
                        last_ts = self._candidate_last_ts.get(key, 0)
                        # 60秒内同一候选不重复写
                        if now_ts - last_ts < 60:
                            continue
                        self._candidate_last_ts[key] = now_ts
                        r.market_regime = self._ctx.regime.value
                        r.trade_mode = self._ctx.trade_mode.value
                        dedup_recs.append(r)
                    if dedup_recs:
                        ledger.log_candidates(dedup_recs)
                    ledger.close()
                except Exception:
                    pass

            # 7. 更新状态机
            has_candidates = len(all_intents) > 0
            has_positions = self._paper_trader.open_position_count > 0
            can_open = trader_result.get("blocked", "") == ""

            self._state_machine.transition(
                now_ts, self._ctx,
                has_candidates=has_candidates and can_open,
                has_open_positions=has_positions,
                risk_allows_new_trade=can_open,
                exit_required=trader_result.get("trades_closed", 0) > 0,
            )
            result["machine_state"] = self._state_machine.current.value
            result["state_reason"] = self._state_machine.reason

        except Exception as e:
            logger.error(f"新架构旁路异常: {e}", exc_info=True)
            result["status"] = "error"
            result["error"] = str(e)

        return result
