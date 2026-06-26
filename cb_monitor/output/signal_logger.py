"""
信号日志记录器 - SignalLogger

将每轮信号写入CSV文件，每日滚动
文件: logs/signals_YYYYMMDD.csv
"""

import os
import csv
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class SignalLogger:
    """信号日志记录器"""

    def __init__(self, config: dict):
        self.cfg = config.get('log', {})
        self.enabled = self.cfg.get('signal_log_enabled', True)
        self.log_dir = self.cfg.get('signal_log_dir', 'logs')
        self._today = None
        self._file = None
        self._writer = None
        # 内部冷却字典 (与AlertManager互补: 防止日志重复写入)
        self._cooldowns: dict[str, float] = {}
        output_cfg = config.get('output', {})
        self._cooldown_seconds = output_cfg.get('cooldown_seconds', 120)
        self._cooldown_B = output_cfg.get('cooldown_seconds_B', 300)

    def _ensure_file(self):
        today = time.strftime('%Y%m%d')
        if today == self._today and self._file and not self._file.closed:
            return
        self._today = today
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, f'signals_{today}.csv')
        is_new = not os.path.exists(path)
        self._file = open(path, 'a', newline='', encoding='utf-8')
        self._writer = csv.writer(self._file)
        if is_new:
            self._writer.writerow(['signal_id', '时间', '等级', '类型', '代码', '名称',
                                   '现价', '涨跌幅', '正股涨跌幅', '溢价率',
                                   '评分', '详情'])
            self._file.flush()

    def write(self, signal, snapshot=None):
        """写入一条信号记录 (带冷却去重)"""
        if not self.enabled:
            return
        try:
            # 冷却检查
            cooldown_key = f"{signal.code}_{signal.level}"
            now = time.time()
            last_time = self._cooldowns.get(cooldown_key, 0)
            if now - last_time < self._cooldown_B if signal.level == 'B' else self._cooldown_seconds:
                return  # 冷却中, 跳过写入
            self._cooldowns[cooldown_key] = now

            self._ensure_file()
            self._writer.writerow([
                signal.signal_id,
                time.strftime('%H:%M:%S'),
                signal.level,
                signal.signal_type,
                signal.code,
                signal.name,
                f'{snapshot.trade:.2f}' if snapshot and getattr(snapshot, 'trade', None) else '',
                f'{snapshot.change_pct:+.2f}%' if snapshot and getattr(snapshot, 'change_pct', None) is not None else '',
                f'{snapshot.stock_change_pct:+.2f}%' if snapshot and getattr(snapshot, 'stock_change_pct', None) is not None else '',
                f'{snapshot.premium_ratio:.2f}%' if snapshot and getattr(snapshot, 'premium_ratio', None) is not None else '',
                signal.score,
                signal.description,
            ])
            self._file.flush()
        except Exception as e:
            logger.error(f"写信号日志失败: {e}")

    def write_batch(self, signals, snapshots=None):
        """批量写入信号记录"""
        for sig in signals:
            snap = snapshots.get(sig.code) if snapshots else None
            self.write(sig, snap)

    def close(self):
        """关闭日志文件"""
        try:
            if self._file and not self._file.closed:
                self._file.close()
        except Exception:
            pass
