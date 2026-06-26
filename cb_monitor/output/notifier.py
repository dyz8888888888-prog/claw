"""
推送通知模块 - Notifier

支持:
- 钉钉自定义机器人 (webhook)
- 飞书自定义机器人 (webhook)
- Server酱 (serverchan)
"""

import logging
import time
from config import SIGNAL_LEVELS

logger = logging.getLogger(__name__)


class Notifier:
    """推送通知"""

    def __init__(self, config: dict):
        self.cfg = config.get('notify', {})
        self.enabled = self.cfg.get('enabled', False)
        self.provider = self.cfg.get('provider', 'dingtalk')
        webhook_map = {
            'dingtalk': self.cfg.get('dingtalk_webhook', ''),
            'feishu': self.cfg.get('feishu_webhook', ''),
            'serverchan': self.cfg.get('serverchan_key', ''),
        }
        self.webhook = webhook_map.get(self.provider, '')
        self.min_level = self.cfg.get('min_level', 'A')
        self._current_feishu_payload = None

    def send(self, signals, snapshots=None):
        """推送信号通知 (仅推送等级 >= min_level 的)"""
        if not self.enabled or not self.webhook or not signals:
            return

        min_rank = SIGNAL_LEVELS.get(self.min_level, 4)
        for sig in signals:
            if sig.level_rank < min_rank:
                continue
            snap = snapshots.get(sig.code) if snapshots else None
            if self.provider == 'feishu':
                self._current_feishu_payload = self._format_msg_feishu(sig, snap)
            else:
                msg = self._format_msg(sig, snap)
                self._current_feishu_payload = None
            self._push(msg if self.provider != 'feishu' else '')

    def _format_msg(self, sig, snap=None) -> str:
        parts = [
            f'【{sig.level}级信号】{sig.name}({sig.code})',
            f'类型: {sig.signal_type}',
        ]
        if snap:
            if getattr(snap, 'trade', None):
                parts.append(f'现价: {snap.trade:.2f}')
            if getattr(snap, 'change_pct', None) is not None:
                parts.append(f'涨幅: {snap.change_pct:+.2f}%')
            if getattr(snap, 'stock_change_pct', None) is not None:
                parts.append(f'正股: {snap.stock_change_pct:+.2f}%')
        parts.append(f'详情: {sig.description}')
        return '\n'.join(parts)

    def _format_msg_feishu(self, sig, snap=None) -> dict:
        """飞书消息卡片格式"""
        level_color = {'S': 'red', 'A': 'orange', 'B': 'blue', 'C': 'purple', 'D': 'grey'}
        color = level_color.get(sig.level, 'blue')
        lines = [
            f"**{sig.level}级信号 · {sig.name}**（{sig.code}）",
            f"类型：{sig.signal_type}",
        ]
        if snap:
            if getattr(snap, 'trade', None):
                lines.append(f"现价：{snap.trade:.2f}")
            if getattr(snap, 'change_pct', None) is not None:
                lines.append(f"涨幅：{snap.change_pct:+.2f}%")
            if getattr(snap, 'stock_change_pct', None) is not None:
                lines.append(f"正股：{snap.stock_change_pct:+.2f}%")
        lines.append(f"详情：{sig.description}")
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"可转债 {sig.level}级信号"},
                    "template": color,
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
                    {"tag": "hr"},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": f"可转债日内联动监控 · {time.strftime('%H:%M:%S', time.localtime(sig.timestamp))}"}]},
                ],
            },
        }

    def _push(self, msg: str):
        """发送推送"""
        try:
            import requests
            if self.provider == 'dingtalk':
                resp = requests.post(
                    self.webhook,
                    json={'msgtype': 'text', 'text': {'content': msg}},
                    timeout=5,
                )
            elif self.provider == 'feishu':
                resp = requests.post(
                    self.webhook,
                    json=self._current_feishu_payload or {'msg_type': 'text', 'content': {'text': msg}},
                    headers={'Content-Type': 'application/json'},
                    timeout=5,
                )
            elif self.provider == 'serverchan':
                resp = requests.post(
                    f'https://sctapi.ftqq.com/{self.webhook}.send',
                    json={'title': '可转债信号', 'content': msg},
                    timeout=5,
                )
            else:
                logger.warning(f"不支持的推送方式: {self.provider}")
                return

            if resp.status_code != 200:
                logger.warning(f"推送失败 ({resp.status_code}): {resp.text[:200]}")
            else:
                logger.info(f"推送成功 ({self.provider})")

        except ImportError:
            logger.warning("推送需要 requests 库")
        except Exception as e:
            logger.error(f"推送异常: {e}")
