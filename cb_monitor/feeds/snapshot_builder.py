"""
快照构建器 — 将多源行情归一化为 MarketSnapshot。

这里只做字段映射与缺失值回退，不做交易判断。
"""

from __future__ import annotations

import time
from typing import Any

from domain.models import MarketSnapshot


class SnapshotBuilder:
    """多源数据 → 统一快照"""

    # ── halt_risk 判定 ────────────────────────

    @staticmethod
    def _classify_halt_risk(cb_price: float, premium: float, redeem_status: str,
                            extras: dict[str, Any] | None = None) -> str:
        """根据价格/溢价/强赎判定临停风险"""
        if redeem_status == "triggered":
            return "halted"
        if cb_price >= 200:
            return "near_30"    # 200+ 临近30%临停
        if cb_price >= 130:
            return "near_20"    # 130+ 临近20%临停
        if premium > 80:
            return "near_30"    # 极高溢价
        if premium > 50:
            return "near_20"
        return "safe"

    # ── 单只构建 ──────────────────────────────

    def build_one(
        self,
        cb_row: dict[str, Any],
        stock_row: dict[str, Any] | None,
        meta_row: dict[str, Any] | None,
        now_ts: float | None = None,
    ) -> MarketSnapshot:
        """
        单只转债快照组装。

        cb_row:   转债实时行情 (来自 TDX push2)
        stock_row: 正股实时行情 (来自 TDX stock)
        meta_row:  转股价/溢价率/规模/强赎 (来自东财)
        """
        if now_ts is None:
            now_ts = time.time()

        # ── 转债行情 (必填字段有默认值) ──
        cb_price = float(cb_row.get("price", 0) or 0)
        cb_bid1 = float(cb_row.get("bid1", 0) or 0)
        cb_ask1 = float(cb_row.get("ask1", 0) or 0)

        # 计算点差
        if cb_bid1 > 0 and cb_ask1 > 0:
            cb_spread_pct = (cb_ask1 - cb_bid1) / cb_bid1 * 100
        else:
            cb_spread_pct = 0.0

        # ── 正股行情 ──
        if stock_row:
            stock_price = float(stock_row.get("price", 0) or 0)
            stock_pct = float(stock_row.get("pct", 0) or 0)
            stock_code = str(stock_row.get("code", ""))
            stock_name = str(stock_row.get("name", ""))
        else:
            stock_price = 0.0
            stock_pct = 0.0
            stock_code = ""
            stock_name = ""

        # ── 静态信息 ──
        if meta_row:
            convert_value = float(meta_row.get("convert_value", 0) or 0)
            premium = float(meta_row.get("premium", 0) or 0)
            issue_scale = float(meta_row.get("issue_scale", 0) or 0)
            redeem_status = str(meta_row.get("redeem_status", "normal"))
        else:
            convert_value = 0.0
            premium = 0.0
            issue_scale = 0.0
            redeem_status = "normal"

        # ── 标签 ──
        tags: list[str] = []
        if issue_scale > 0 and issue_scale < 3:
            tags.append("micro_cap")
        if premium > 40:
            tags.append("high_premium")
        if cb_price > 200:
            tags.append("high_price")

        halt_risk = self._classify_halt_risk(cb_price, premium, redeem_status)

        return MarketSnapshot(
            ts=now_ts,
            cb_code=str(cb_row.get("code", "")),
            cb_name=str(cb_row.get("name", "")),
            cb_price=cb_price,
            cb_pct=float(cb_row.get("pct", 0) or 0),
            cb_open=float(cb_row.get("open", 0) or 0),
            cb_high=float(cb_row.get("high", 0) or 0),
            cb_low=float(cb_row.get("low", 0) or 0),
            cb_volume=int(cb_row.get("volume", 0) or 0),
            cb_amount=float(cb_row.get("amount", 0) or 0),
            cb_bid1=cb_bid1,
            cb_ask1=cb_ask1,
            cb_bid1_vol=int(cb_row.get("bid1_vol", 0) or 0),
            cb_ask1_vol=int(cb_row.get("ask1_vol", 0) or 0),
            cb_spread_pct=cb_spread_pct,
            cb_volume_ratio=float(cb_row.get("volume_ratio", 0) or 0),
            stock_code=stock_code,
            stock_name=stock_name,
            stock_price=stock_price,
            stock_pct=stock_pct,
            convert_value=convert_value,
            premium=premium,
            issue_scale=issue_scale,
            redeem_status=redeem_status,
            halt_risk=halt_risk,
            tags=tuple(tags),
        )

    # ── 批量构建 ──────────────────────────────

    def build_batch(
        self,
        cb_rows: dict[str, dict[str, Any]],
        stock_rows: dict[str, dict[str, Any]] | None = None,
        meta_rows: dict[str, dict[str, Any]] | None = None,
        now_ts: float | None = None,
    ) -> dict[str, MarketSnapshot]:
        """
        批量组装快照。

        cb_rows:    {cb_code: {字段...}}
        stock_rows: {stock_code: {字段...}}  (可选)
        meta_rows:  {cb_code: {字段...}}     (可选)
        返回:      {cb_code: MarketSnapshot}
        """
        if stock_rows is None:
            stock_rows = {}
        if meta_rows is None:
            meta_rows = {}
        if now_ts is None:
            now_ts = time.time()

        result: dict[str, MarketSnapshot] = {}
        for cb_code, cb_row in cb_rows.items():
            # 查找正股对应 (通过 stock_code 映射)
            stock_code = cb_row.get("stock_code", "")
            stock_row = stock_rows.get(stock_code)
            meta_row = meta_rows.get(cb_code)

            snap = self.build_one(cb_row, stock_row, meta_row, now_ts)
            result[cb_code] = snap

        return result
