"""
信号后验统计追踪器 — SignalTracker

每条信号触发后，在固定时间窗口 (10s/30s/60s/180s/300s) 自动记录：
  - 当前收益 (pnl_pct)
  - 最大浮盈 (max_profit)
  - 最大回撤 (max_drawdown)

完成后写入 CSV 并自动清除，供盘后统计分析信号有效性。
"""

import csv
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

CHECKPOINTS = [10, 30, 60, 180, 300]  # 秒


class SignalBacktestRecord:
    """单条信号的后验记录"""

    __slots__ = (
        "signal_id", "code", "name", "signal_type", "level", "strategy",
        "trigger_price", "trigger_time", "trigger_ts",
        "peak_price", "trough_price", "current_price", "current_pnl",
        "checkpoints",  # {10: {pnl, max_profit, max_drawdown}, 30: ...}
        "completed", "concept", "limit_up_count", "diffusion_rank",
    )

    def __init__(self, signal_id: str, code: str, name: str,
                 signal_type: str, level: str, strategy: str,
                 trigger_price: float, trigger_time: str, trigger_ts: float):
        self.signal_id = signal_id
        self.code = code
        self.name = name
        self.signal_type = signal_type
        self.level = level
        self.strategy = strategy
        self.trigger_price = trigger_price
        self.trigger_time = trigger_time
        self.trigger_ts = trigger_ts
        self.peak_price = trigger_price
        self.trough_price = trigger_price
        self.current_price = trigger_price
        self.current_pnl = 0.0
        self.checkpoints = {}
        self.completed = False
        self.concept = ""
        self.limit_up_count = 0
        self.diffusion_rank = 0

    def update(self, price: float, now_ts: float):
        """每轮 tick 更新峰值/低谷/当前盈亏"""
        self.current_price = price
        self.peak_price = max(self.peak_price, price)
        self.trough_price = min(self.trough_price, price)
        self.current_pnl = round(
            (price - self.trigger_price) / self.trigger_price * 100, 2
        )

        # 检查是否到达检查点
        elapsed = now_ts - self.trigger_ts
        for cp in CHECKPOINTS:
            if cp not in self.checkpoints and elapsed >= cp:
                self.checkpoints[cp] = {
                    "pnl": self.current_pnl,
                    "max_profit": round(
                        (self.peak_price - self.trigger_price) / self.trigger_price * 100, 2
                    ),
                    "max_drawdown": round(
                        (self.trough_price - self.trigger_price) / self.trigger_price * 100, 2
                    ),
                }

        # 300s 后标记完成
        if elapsed >= 300 and not self.completed:
            self.completed = True

    def to_row(self) -> list:
        """转 CSV 行"""
        return [
            self.signal_id,
            self.code,
            self.name,
            self.signal_type,
            self.level,
            self.strategy,
            self.trigger_time,
            round(self.trigger_price, 2),
            round(self.peak_price, 2),
            round(self.trough_price, 2),
            self.current_pnl,
            self.checkpoints.get(10, {}).get("pnl", ""),
            self.checkpoints.get(10, {}).get("max_profit", ""),
            self.checkpoints.get(10, {}).get("max_drawdown", ""),
            self.checkpoints.get(30, {}).get("pnl", ""),
            self.checkpoints.get(30, {}).get("max_profit", ""),
            self.checkpoints.get(30, {}).get("max_drawdown", ""),
            self.checkpoints.get(60, {}).get("pnl", ""),
            self.checkpoints.get(60, {}).get("max_profit", ""),
            self.checkpoints.get(60, {}).get("max_drawdown", ""),
            self.checkpoints.get(180, {}).get("pnl", ""),
            self.checkpoints.get(180, {}).get("max_profit", ""),
            self.checkpoints.get(180, {}).get("max_drawdown", ""),
            self.checkpoints.get(300, {}).get("pnl", ""),
            self.checkpoints.get(300, {}).get("max_profit", ""),
            self.checkpoints.get(300, {}).get("max_drawdown", ""),
            self.concept,
        ]

    def to_dict(self) -> dict:
        """转 API JSON"""
        return {
            "id": self.signal_id,
            "code": self.code,
            "name": self.name,
            "type": self.signal_type,
            "level": self.level,
            "strategy": self.strategy,
            "trigger_price": round(self.trigger_price, 2),
            "trigger_time": self.trigger_time,
            "current_pnl": self.current_pnl,
            "peak_price": round(self.peak_price, 2),
            "trough_price": round(self.trough_price, 2),
            "completed": self.completed,
            "checkpoints": {
                str(k): v for k, v in self.checkpoints.items()
            },
        }

    @staticmethod
    def csv_header() -> list:
        """CSV 表头"""
        return [
            "signal_id", "code", "name", "signal_type", "level", "strategy",
            "trigger_time", "trigger_price", "peak_price", "trough_price",
            "final_pnl",
            "10s_pnl", "10s_max_profit", "10s_max_drawdown",
            "30s_pnl", "30s_max_profit", "30s_max_drawdown",
            "60s_pnl", "60s_max_profit", "60s_max_drawdown",
            "180s_pnl", "180s_max_profit", "180s_max_drawdown",
            "300s_pnl", "300s_max_profit", "300s_max_drawdown",
            "concept",
        ]


class SignalTracker:
    """后验统计追踪器 — 管理所有 active 和 completed 记录"""

    def __init__(self, log_dir: str = "logs"):
        self._active: dict[str, SignalBacktestRecord] = {}  # signal_id → record
        self._completed: list[dict] = []  # 当日完成记录 (最多保留200条)
        self._log_dir = log_dir
        self._today = ""
        self._csv_writer = None
        self._csv_file = None
        self._id_seq = 0
        # 同债同策略去重: key="code_strategy" → timestamp, 120s内不重复追踪
        self._dedup: dict[str, float] = {}

    def on_signal(self, sig, trigger_price: float, strategy: str = "chase",
                  concept: str = "", limit_up_count: int = 0,
                  diffusion_rank: int = 0, ask_price: float = 0):
        """信号触发时注册追踪 (同债同策略120s内去重)"""
        # 去重: 同债同策略120s内只追踪一次
        dedup_key = f"{sig.code}_{strategy}"
        now_ts = time.time()
        last_ts = self._dedup.get(dedup_key, 0)
        if now_ts - last_ts < 120:
            return  # 冷却中, 跳过追踪
        self._dedup[dedup_key] = now_ts

        self._id_seq += 1
        # 统一使用 Signal 的 signal_id, 确保信号CSV和回测CSV可关联
        signal_id = getattr(sig, 'signal_id', '') or f"{sig.code}_{self._id_seq}"
        # 用 ask1 作为实际买入价做纸面收益 (更真实)
        effective_entry = ask_price if ask_price > 0 else trigger_price
        record = SignalBacktestRecord(
            signal_id=signal_id,
            code=sig.code,
            name=sig.name,
            signal_type=sig.signal_type,
            level=sig.level,
            strategy=strategy,
            trigger_price=round(effective_entry, 2),
            trigger_time=time.strftime("%H:%M:%S", time.localtime(sig.timestamp)),
            trigger_ts=now_ts,
        )
        record.concept = concept
        record.limit_up_count = limit_up_count
        record.diffusion_rank = diffusion_rank
        self._active[signal_id] = record
        logger.info(f"追踪信号: [{sig.level}] {sig.name} {sig.signal_type} "
                     f"@¥{trigger_price:.2f} 策略={strategy}")

    def tick(self, snapshots: dict):
        """每轮更新所有活跃追踪"""
        now_ts = time.time()
        completed = []
        prices_moved = 0
        prices_stale = 0

        for sid, record in self._active.items():
            snap = snapshots.get(record.code)
            if snap:
                # 用 bid1 作为实际卖出价 (更真实的纸面收益)
                bid1 = getattr(snap, "bid1", 0)
                trade = getattr(snap, "trade", 0)
                price = bid1 if bid1 > 0 else trade
                if price > 0:
                    prev = record.current_price
                    record.update(price, now_ts)
                    if record.current_price != prev:
                        prices_moved += 1
                    else:
                        prices_stale += 1

            if record.completed:
                completed.append(sid)

        # 行情停滞检测: 超过 60s 且所有活跃记录的 price 均未变化 → 告警
        if self._active and prices_moved == 0 and prices_stale > 0:
            elapsed = now_ts - getattr(self, '_last_stale_warn', 0)
            if elapsed > 60:
                self._last_stale_warn = now_ts
                logger.warning(f"行情停滞: {prices_stale} 条追踪无价格变化, 请检查数据源")

        # 保存完成记录并清理
        for sid in completed:
            record = self._active.pop(sid)
            self._save(record)
            self._completed.append(record.to_dict())
            if len(self._completed) > 200:
                self._completed = self._completed[-200:]

    def _save(self, record: SignalBacktestRecord):
        """写入 CSV"""
        today = time.strftime("%Y%m%d")
        path = os.path.join(self._log_dir, f"backtest_{today}.csv")

        if today != self._today:
            self._today = today
            if self._csv_file:
                self._csv_file.close()
            os.makedirs(self._log_dir, exist_ok=True)
            is_new = not os.path.exists(path)
            self._csv_file = open(path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            if is_new:
                self._csv_writer.writerow(SignalBacktestRecord.csv_header())

        if self._csv_writer:
            self._csv_writer.writerow(record.to_row())
            self._csv_file.flush()

    def get_active(self) -> list[dict]:
        return [r.to_dict() for r in self._active.values()]

    def get_completed(self) -> list[dict]:
        return self._completed[-50:]  # 最近50条

    def get_stats(self) -> dict:
        """当日统计摘要"""
        if not self._completed:
            return {}
        records = self._completed
        total = len(records)
        positive = sum(1 for r in records if r["current_pnl"] > 0)
        win_rate = round(positive / total * 100, 1) if total > 0 else 0
        avg_pnl = round(sum(r["current_pnl"] for r in records) / total, 2)

        # 按策略分组统计
        by_strategy = {}
        for strategy in ["chase", "dip"]:
            subset = [r for r in records if r.get("strategy") == strategy]
            if subset:
                s_total = len(subset)
                s_positive = sum(1 for r in subset if r["current_pnl"] > 0)
                by_strategy[strategy] = {
                    "total": s_total,
                    "win_rate": round(s_positive / s_total * 100, 1),
                    "avg_pnl": round(sum(r["current_pnl"] for r in subset) / s_total, 2),
                    "best": max(subset, key=lambda r: r["current_pnl"]),
                    "worst": min(subset, key=lambda r: r["current_pnl"]),
                }

        # 按检查点统计
        checkpoint_stats = {}
        for cp in [10, 30, 60, 180, 300]:
            cp_data = [r for r in records if str(cp) in r.get("checkpoints", {})]
            if cp_data:
                cp_pnls = [r["checkpoints"][str(cp)]["pnl"] for r in cp_data]
                checkpoint_stats[str(cp)] = {
                    "count": len(cp_data),
                    "avg_pnl": round(sum(cp_pnls) / len(cp_pnls), 2),
                    "win_rate": round(
                        sum(1 for p in cp_pnls if p > 0) / len(cp_pnls) * 100, 1
                    ),
                    "best": round(max(cp_pnls), 2),
                    "worst": round(min(cp_pnls), 2),
                }

        return {
            "total": total,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "by_strategy": by_strategy,
            "by_checkpoint": checkpoint_stats,
        }

    def close(self):
        if self._csv_file:
            self._csv_file.close()


# 全局单例
tracker = SignalTracker()
