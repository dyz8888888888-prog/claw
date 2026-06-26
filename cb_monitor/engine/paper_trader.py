"""
纸面交易引擎 — 不接真实账户，在系统内模拟整个交易闭环。

信号出现 → 模拟成交 → 建立虚拟持仓 → 每轮更新盈亏
→ 触发退出 → 模拟卖出 → 写入账本 → 统计绩效

这是从"发信号"进化到"验证策略是否真能赚钱"的关键一步。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from domain.enums import HoldingMode, RiskCheck, TradeMode
from domain.models import (
    MarketSnapshot, MarketContext, TradeIntent, Position, CandidateRecord,
)
from engine.risk_engine import RiskEngine, RiskResult
from execution.broker_sim import BrokerSim
from execution.position_manager import PositionManager
from replay.fill_model import FillModel

logger = logging.getLogger(__name__)


@dataclass
class PaperAccount:
    """虚拟账户 — 不碰真实资金，只追踪纸面状态"""

    cash: float = 100_000.0              # 虚拟本金
    daily_pnl_pct: float = 0.0           # 今日累计盈亏%
    today_trades: int = 0                # 今日已成交笔数
    today_gross_pnl: float = 0.0         # 今日毛盈亏
    consecutive_losses: int = 0          # 连续亏损次数
    max_daily_loss_pct: float = 2.0      # 单日最大亏损限制%
    max_positions: int = 2               # 同时最大持仓数
    max_trades_today: int = 3            # 今日最多交易数
    per_cb_daily_trades: int = 1         # 单债单日最多交易次数

    # 追踪数据
    used_cb_trade_count: dict[str, int] = field(default_factory=dict)
    last_trade_ts: float = 0.0

    def can_open(self, cb_code: str = "") -> tuple[bool, str]:
        """检查是否允许开新仓"""
        if self.today_trades >= self.max_trades_today:
            return False, "max_trades"
        if self.daily_pnl_pct <= -self.max_daily_loss_pct:
            return False, "daily_loss_limit"
        if self.consecutive_losses >= 3:
            return False, "consecutive_losses"
        if cb_code and self.used_cb_trade_count.get(cb_code, 0) >= self.per_cb_daily_trades:
            return False, f"per_cb_limit:{cb_code}"
        return True, ""

    def record_entry(self, cb_code: str) -> None:
        self.today_trades += 1
        self.used_cb_trade_count[cb_code] = self.used_cb_trade_count.get(cb_code, 0) + 1
        self.last_trade_ts = time.time()

    def record_exit(self, pnl_pct: float) -> None:
        self.today_gross_pnl += pnl_pct
        self.daily_pnl_pct = self.today_gross_pnl
        if pnl_pct > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

    def get_available_budget(self, mode: TradeMode) -> float:
        limits = {
            TradeMode.ATTACK: 2.5,
            TradeMode.PROBE: 2.0,
            TradeMode.DEFENSE: 1.5,
            TradeMode.DISABLED: 0.0,
        }
        max_loss = limits.get(mode, 2.0)
        used = abs(min(self.daily_pnl_pct, 0))
        return max(0, max_loss - used)

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "daily_pnl_pct": round(self.daily_pnl_pct, 3),
            "today_trades": self.today_trades,
            "consecutive_losses": self.consecutive_losses,
            "used_cb_trade_count": dict(self.used_cb_trade_count),
            "available_budget": 1.0 - abs(min(self.daily_pnl_pct / self.max_daily_loss_pct, 0)),
        }

    def reset_for_new_day(self) -> None:
        self.daily_pnl_pct = 0.0
        self.today_trades = 0
        self.today_gross_pnl = 0.0
        self.consecutive_losses = 0
        self.used_cb_trade_count.clear()


class PaperTrader:
    """纸面交易引擎 — 虚拟账户 + 完整交易闭环"""

    def __init__(self, db_path: str = "data/trade_ledger.db") -> None:
        self.account = PaperAccount()
        self.risk_engine = RiskEngine()
        self.broker = BrokerSim(FillModel())
        self.position_manager = PositionManager()

        # 惰性初始化 TradeLedger (避免在无 Flask 环境崩溃)
        self._ledger = None
        self._db_path = db_path
        self._ledger_last_trade_date = ""

    def _get_ledger(self):
        # 按需初始化，每天自动切换
        today = time.strftime("%Y-%m-%d")
        if self._ledger is None or self._ledger_last_trade_date != today:
            if self._ledger:
                try:
                    self._ledger.close()
                except Exception:
                    pass
            from ledger.trade_log import TradeLedger
            self._ledger = TradeLedger(self._db_path)
            self._ledger_last_trade_date = today
        return self._ledger

    @property
    def active_positions(self) -> list[Position]:
        return self.position_manager.get_active()

    @property
    def open_position_count(self) -> int:
        return self.position_manager.active_count()

    # ── 构建 account_ctx ─────────────────

    def _build_account_ctx(self, market_ctx: MarketContext) -> dict:
        return {
            "daily_pnl_pct": self.account.daily_pnl_pct,
            "consecutive_losses": self.account.consecutive_losses,
            "used_cb_trade_count": dict(self.account.used_cb_trade_count),
            "available_risk_budget": self.account.get_available_budget(market_ctx.trade_mode),
            "now_ts": time.time(),
        }

    # ── 主循环入口 ──────────────────────

    def run(
        self,
        market_ctx: MarketContext,
        snapshots: dict[str, MarketSnapshot],
        intents: list[TradeIntent],
        selection_records: list[CandidateRecord],
    ) -> dict:
        """
        每轮主循环调用一次。

        intents: 策略产生的所有交易意图
        selection_records: Selector 排序后的候选列表

        返回: {state, trades_opened, trades_closed, positions, ...}
        """
        now = time.time()
        trades_opened = 0
        trades_closed = 0
        blocked = ""
        ledger = self._get_ledger()

        # ── 第一步: 管理已有持仓 ──
        for pos in self.active_positions:
            snap = snapshots.get(pos.cb_code)
            if not snap:
                continue

            # 更新浮盈浮亏
            self.position_manager.update_marks(pos, snap.cb_price)

            # 按策略分派退出规则
            action = self._dispatch_exit(pos, snap)

            if action[0] == "exit":
                # 模拟卖出
                fill = self.broker.submit_exit(pos, snap)
                if fill.filled:
                    settle = self.position_manager.close_position(
                        pos, fill.fill_price, now, action[1],
                    )
                    self.account.record_exit(settle["pnl_pct"])
                    trades_closed += 1

                    # 记平仓账
                    try:
                        ledger.log_exit(
                            pos.position_id, now,
                            snap.cb_price, fill.fill_price,
                            settle["gross_pnl"], settle["pnl_pct"],
                            fill.slippage_cost, action[1],
                            pos.max_favorable_pct, pos.max_adverse_pct,
                        )
                    except Exception as e:
                        logger.warning(f"平仓记账失败: {e}")

        # ── 第二步: 检查是否允许开新仓 ──
        can_open, block_reason = self.account.can_open()
        if not can_open:
            blocked = block_reason
            return self._build_result(now, trades_opened, trades_closed, blocked)

        if self.open_position_count >= self.account.max_positions:
            blocked = "max_positions"
            return self._build_result(now, trades_opened, trades_closed, blocked)

        # ── 第三步: 尝试开新仓 ──
        if not intents:
            return self._build_result(now, trades_opened, trades_closed, blocked)

        # 取排名第一的候选
        top_intent = intents[0]
        top_snap = snapshots.get(top_intent.cb_code)
        if not top_snap:
            return self._build_result(now, trades_opened, trades_closed, blocked)

        # 风控检查
        risk_result = self.risk_engine.check_new_trade(
            top_intent, market_ctx,
            account_ctx=self._build_account_ctx(market_ctx),
            positions=self.active_positions,
        )

        if risk_result.status != RiskCheck.ALLOW:
            # 标记候选被风控拦截
            for r in selection_records:
                if r.cb_code == top_intent.cb_code:
                    r.selected = False
                    r.rejected_by = risk_result.reason_codes
            return self._build_result(now, trades_opened, trades_closed, blocked,
                                      risk=risk_result.status.value,
                                      risk_reason="|".join(risk_result.reason_codes))

        # 模拟买入
        fill = self.broker.submit_entry(top_intent, top_snap, qty=10)
        if not fill.filled:
            return self._build_result(now, trades_opened, trades_closed, blocked,
                                      entry_failed=fill.reason)

        # 建虚拟仓
        pos = self.position_manager.open_from_fill(
            top_intent, fill_price=fill.fill_price,
            qty=fill.fill_qty, now_ts=now,
        )
        pos.cb_name = top_snap.cb_name
        self.account.record_entry(top_intent.cb_code)
        trades_opened = 1

        # 标记候选被选中
        for r in selection_records:
            if r.cb_code == top_intent.cb_code:
                r.selected = True

        # 记开仓账
        try:
            ledger.log_entry(
                pos, now, top_snap.cb_price, fill.fill_price,
                market_ctx.regime.value, market_ctx.trade_mode.value,
                top_intent.reason_text,
            )
        except Exception as e:
            logger.warning(f"开仓记账失败: {e}")

        return self._build_result(now, trades_opened, trades_closed, blocked)

    def _build_result(self, now, trades_opened, trades_closed, blocked="",
                      risk="", risk_reason="", entry_failed="") -> dict:
        return {
            "ts": now,
            "trades_opened": trades_opened,
            "trades_closed": trades_closed,
            "positions": self.open_position_count,
            "blocked": blocked,
            "risk_top1": risk or "",
            "risk_reason": risk_reason,
            "entry_failed": entry_failed,
            "account": self.account.to_dict(),
        }

    def _dispatch_exit(self, pos: Position, snap: MarketSnapshot) -> tuple[str, str]:
        """按策略ID分派退出规则"""
        from execution.exit_rules import ExitRules

        sid = pos.strategy_id
        params = {
            "stop_loss_pct": pos.stop_loss_pct,
            "take_profit_pct": pos.take_profit_pct,
            "trail_drawdown_pct": pos.trailing_drawdown_pct,
            "max_hold_seconds": 300,
        }

        if sid == "volume_follow":
            return ExitRules.for_volume_follow(pos, snap, params)
        elif sid == "board_spillover":
            # 封板溢出: 在 ExitRules 加 for_board_spillover 后启用
            return ExitRules.for_volume_follow(pos, snap, params)  # 临时代用
        elif sid == "tailwash_overnight":
            # 尾盘隔夜: 用 overnight 检查
            return ExitRules.for_volume_follow(pos, snap, {"max_hold_seconds": 86400, **params})
        else:
            return ExitRules.for_volume_follow(pos, snap, params)

    def start_new_day(self) -> None:
        """新交易日重置"""
        self.account.reset_for_new_day()
        self._ledger = None
        self._ledger_last_trade_date = ""

    def get_portfolio_summary(self) -> dict:
        """返回纸面账户摘要"""
        return {
            "account": self.account.to_dict(),
            "positions": [{
                "id": p.position_id,
                "cb_code": p.cb_code,
                "cb_name": p.cb_name,
                "entry_price": p.entry_price,
                "entry_time": p.entry_ts,
                "max_favorable": p.max_favorable_pct,
                "max_adverse": p.max_adverse_pct,
            } for p in self.active_positions],
        }
