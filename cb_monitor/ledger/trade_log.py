"""
交易账本 — SQLite 双轨持久化。

candidate_events:  记录所有候选标的 (不管做没做)
executed_trades:   记录所有已执行的交易 (进场+离场)
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from domain.models import CandidateRecord, MarketSnapshot, Position, TradeIntent


class TradeLedger:
    """SQLite 交易账本 — 双表双轨"""

    def __init__(self, db_path: str = "data/trade_ledger.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    # ── 建表 ──────────────────────────────────

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                cb_code     TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL DEFAULT '',
                score       REAL DEFAULT 0,
                selected    INTEGER DEFAULT 0,
                rejected_by TEXT DEFAULT '',
                market_regime TEXT DEFAULT '',
                trade_mode  TEXT DEFAULT '',
                reason_text TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS executed_trades (
                position_id         TEXT PRIMARY KEY,
                trade_date          TEXT NOT NULL,
                strategy_id         TEXT NOT NULL,
                strategy_version    TEXT NOT NULL DEFAULT '',
                holding_mode        TEXT NOT NULL DEFAULT '',
                cb_code             TEXT NOT NULL,
                cb_name             TEXT NOT NULL DEFAULT '',
                entry_signal_ts     REAL NOT NULL,
                entry_ts            REAL NOT NULL,
                entry_theoretical_price REAL NOT NULL,
                entry_fill_price    REAL NOT NULL,
                qty                 INTEGER NOT NULL DEFAULT 1,
                market_regime       TEXT DEFAULT '',
                trade_mode          TEXT DEFAULT '',
                entry_reason        TEXT DEFAULT '',
                exit_ts             REAL,
                exit_theoretical_price REAL,
                exit_fill_price     REAL,
                gross_pnl           REAL,
                net_pnl             REAL,
                slippage_cost       REAL DEFAULT 0,
                holding_seconds     INTEGER,
                exit_reason         TEXT DEFAULT '',
                max_favorable_pct   REAL DEFAULT 0,
                max_adverse_pct     REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_candidate_ts ON candidate_events(ts);
            CREATE INDEX IF NOT EXISTS idx_candidate_strategy ON candidate_events(strategy_id);
            CREATE INDEX IF NOT EXISTS idx_trade_date ON executed_trades(trade_date);
            CREATE INDEX IF NOT EXISTS idx_trade_strategy ON executed_trades(strategy_id);
        """)
        self.conn.commit()

    # ── 候选轨道 ──────────────────────────────

    def log_candidate(self, record: CandidateRecord) -> None:
        """记录候选标的 (不管是否选中)"""
        self.conn.execute(
            """INSERT INTO candidate_events
               (ts, cb_code, strategy_id, strategy_version,
                score, selected, rejected_by, market_regime, trade_mode, reason_text, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.ts,
                record.cb_code,
                record.strategy_id,
                record.strategy_version,
                record.score,
                1 if record.selected else 0,
                "|".join(record.rejected_by) if record.rejected_by else "",
                record.market_regime,
                record.trade_mode,
                "",  # reason_text
                "",  # payload_json
            ),
        )
        self.conn.commit()

    def log_candidates(self, records: list[CandidateRecord]) -> None:
        """批量记录候选"""
        for r in records:
            self.log_candidate(r)

    # ── 交易轨道 ──────────────────────────────

    def log_entry(
        self,
        position: Position,
        signal_ts: float,
        theoretical_price: float,
        fill_price: float,
        market_regime: str,
        trade_mode: str,
        entry_reason: str,
    ) -> None:
        """记录开仓"""
        self.conn.execute(
            """INSERT INTO executed_trades
               (position_id, trade_date, strategy_id, strategy_version,
                holding_mode, cb_code, cb_name,
                entry_signal_ts, entry_ts, entry_theoretical_price, entry_fill_price,
                qty, market_regime, trade_mode, entry_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.position_id,
                time.strftime("%Y-%m-%d", time.localtime(position.entry_ts)),
                position.strategy_id,
                position.strategy_version,
                position.holding_mode,
                position.cb_code,
                position.cb_name,
                signal_ts,
                position.entry_ts,
                theoretical_price,
                fill_price,
                position.qty,
                market_regime,
                trade_mode,
                entry_reason,
            ),
        )
        self.conn.commit()

    def log_exit(
        self,
        position_id: str,
        exit_ts: float,
        theoretical_price: float,
        fill_price: float,
        gross_pnl: float,
        net_pnl: float,
        slippage_cost: float,
        exit_reason: str,
        max_favorable_pct: float,
        max_adverse_pct: float,
    ) -> None:
        """记录平仓 (更新已有开仓记录)"""
        # 计算持有秒数
        row = self.conn.execute(
            "SELECT entry_ts FROM executed_trades WHERE position_id = ?",
            (position_id,),
        ).fetchone()

        entry_ts = row["entry_ts"] if row else exit_ts
        holding_seconds = int(exit_ts - entry_ts)

        self.conn.execute(
            """UPDATE executed_trades SET
               exit_ts = ?, exit_theoretical_price = ?, exit_fill_price = ?,
               gross_pnl = ?, net_pnl = ?, slippage_cost = ?,
               holding_seconds = ?, exit_reason = ?,
               max_favorable_pct = ?, max_adverse_pct = ?
               WHERE position_id = ?""",
            (
                exit_ts,
                theoretical_price,
                fill_price,
                gross_pnl,
                net_pnl,
                slippage_cost,
                holding_seconds,
                exit_reason,
                max_favorable_pct,
                max_adverse_pct,
                position_id,
            ),
        )
        self.conn.commit()

    # ── 查询接口 ──────────────────────────────

    def get_today_trades(self, trade_date: str | None = None) -> list[dict]:
        """返回某日所有已成交交易"""
        if trade_date is None:
            trade_date = time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM executed_trades WHERE trade_date = ? ORDER BY entry_ts",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today_candidates(self, trade_date: str | None = None) -> list[dict]:
        """返回某日所有候选记录"""
        if trade_date is None:
            trade_date = time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM candidate_events WHERE date(ts, 'unixepoch') = ? ORDER BY ts",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_strategy_performance(self, strategy_id: str, days: int = 20) -> dict:
        """近N日策略绩效摘要"""
        cutoff = time.time() - days * 86400
        rows = self.conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                 AVG(net_pnl) as avg_pnl,
                 MIN(net_pnl) as max_loss,
                 MAX(net_pnl) as max_win,
                 AVG(holding_seconds) as avg_hold,
                 AVG(slippage_cost) as avg_slippage
               FROM executed_trades
               WHERE strategy_id = ? AND entry_ts > ? AND exit_ts IS NOT NULL""",
            (strategy_id, cutoff),
        ).fetchone()

        if not rows or rows["total_trades"] == 0:
            return {"total_trades": 0, "verdict": "insufficient_data"}

        total = rows["total_trades"]
        wins = rows["wins"]
        return {
            "total_trades": total,
            "win_rate": round(wins / total * 100, 1),
            "avg_pnl": round(rows["avg_pnl"] or 0, 4),
            "max_loss": round(rows["max_loss"] or 0, 4),
            "max_win": round(rows["max_win"] or 0, 4),
            "avg_hold_seconds": round(rows["avg_hold"] or 0, 1),
            "avg_slippage": round(rows["avg_slippage"] or 0, 4),
        }
