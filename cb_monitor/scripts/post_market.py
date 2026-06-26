"""
盘后统计报告生成器 — PostMarketReporter

收盘后 (15:00+) 自动分析当日数据:
- 信号统计 (按等级/类型/代码)
- 后验胜率 (backtest CSV)
- 策略对比 (追涨 vs 回落)
- 概念板块热力图
- 输出: Markdown 报告 + 终端打印
"""

import os
import csv
import time
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


class PostMarketReporter:
    """盘后报告生成器"""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        self.today = time.strftime("%Y%m%d")

    def generate(self) -> str:
        """生成当日报告, 返回Markdown文本"""
        lines = []
        lines.append(f"# 可转债日内监控 - {time.strftime('%Y-%m-%d')} 盘后报告")
        lines.append("")

        # 1. 信号统计
        signal_stats = self._analyze_signals()
        if signal_stats:
            lines.append("## 一、信号统计")
            lines.append("")
            lines.append(f"| 等级 | 数量 | 占比 |")
            lines.append("|------|:---:|:---:|")
            total = sum(signal_stats.values())
            for lv in ['S', 'A', 'B', 'C', 'D']:
                cnt = signal_stats.get(lv, 0)
                if cnt:
                    pct = cnt / max(total, 1) * 100
                    lines.append(f"| {lv} | {cnt} | {pct:.0f}% |")
            lines.append(f"| **合计** | **{total}** | |")
            lines.append("")

        # 2. Top信号债
        top_codes = self._analyze_top_codes()
        if top_codes:
            lines.append("## 二、最活跃标的 (Top 5)")
            lines.append("")
            lines.append("| 代码 | 名称 | 信号数 | S/A | B/C/D |")
            lines.append("|------|------|:---:|:---:|:---:|")
            for item in top_codes[:5]:
                lines.append(f"| {item['code']} | {item['name']} | {item['total']} | {item['SA']} | {item['BCD']} |")
            lines.append("")

        # 3. 后验胜率 (backtest CSV)
        backtest_stats = self._analyze_backtest()
        if backtest_stats:
            lines.append("## 三、后验胜率")
            lines.append("")
            lines.append(f"| 指标 | 数值 |")
            lines.append("|------|------|")
            lines.append(f"| 追踪信号数 | {backtest_stats.get('total', 0)} |")
            lines.append(f"| 胜率 | {backtest_stats.get('win_rate', 0):.1f}% |")
            lines.append(f"| 平均盈亏 | {backtest_stats.get('avg_pnl', 0):+.2f}% |")
            lines.append("")

            # 策略对比
            by_strat = backtest_stats.get('by_strategy', {})
            if by_strat:
                lines.append("### 策略对比")
                lines.append("")
                lines.append("| 策略 | 信号数 | 胜率 | 平均盈亏 | 最佳 | 最差 |")
                lines.append("|------|:---:|:---:|:---:|:---:|:---:|")
                for strat, s in by_strat.items():
                    lines.append(f"| {strat} | {s.get('total',0)} | {s.get('win_rate',0):.1f}% | "
                                 f"{s.get('avg_pnl',0):+.2f}% | "
                                 f"{s.get('best',{}).get('current_pnl',0):+.2f}% | "
                                 f"{s.get('worst',{}).get('current_pnl',0):+.2f}% |")
                lines.append("")

            # 检查点统计
            by_cp = backtest_stats.get('by_checkpoint', {})
            if by_cp:
                lines.append("### 检查点收益")
                lines.append("")
                lines.append("| 时间 | 信号数 | 均盈 | 胜率 | 最佳 | 最差 |")
                lines.append("|------|:---:|:---:|:---:|:---:|:---:|")
                for k, v in by_cp.items():
                    lines.append(f"| {k}s | {v.get('count',0)} | {v.get('avg_pnl',0):+.2f}% | "
                                 f"{v.get('win_rate',0):.1f}% | {v.get('best',0):+.2f}% | {v.get('worst',0):+.2f}% |")
                lines.append("")

        # 4. 小结
        lines.append("## 四、小结")
        lines.append("")
        if backtest_stats:
            wr = backtest_stats.get('win_rate', 0)
            if wr >= 60:
                lines.append(f"> 今日胜率 {wr:.1f}%，信号质量较好，策略有效。")
            elif wr >= 45:
                lines.append(f"> 今日胜率 {wr:.1f}%，震荡市表现正常，注意控制仓位。")
            else:
                lines.append(f"> 今日胜率 {wr:.1f}%，信号质量偏低，建议复盘信号触发条件。")
        else:
            lines.append("> 今日无后验数据。")

        lines.append("")
        lines.append(f"---")
        lines.append(f"报告生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        return "\n".join(lines)

    def _analyze_signals(self) -> dict:
        """分析信号CSV: 按等级计数"""
        path = os.path.join(self.log_dir, f"signals_{self.today}.csv")
        if not os.path.exists(path):
            return {}
        stats = defaultdict(int)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    lv = row.get('等级', '')
                    if lv:
                        stats[lv] += 1
        except Exception as e:
            logger.error(f"读取信号CSV失败: {e}")
        return dict(stats)

    def _analyze_top_codes(self) -> list:
        """Top信号债"""
        path = os.path.join(self.log_dir, f"signals_{self.today}.csv")
        if not os.path.exists(path):
            return []
        code_stats = defaultdict(lambda: {'total': 0, 'SA': 0, 'BCD': 0, 'name': ''})
        try:
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    code = row.get('代码', '')
                    lv = row.get('等级', '')
                    name = row.get('名称', '')
                    if code:
                        code_stats[code]['total'] += 1
                        code_stats[code]['name'] = name
                        if lv in ('S', 'A'):
                            code_stats[code]['SA'] += 1
                        else:
                            code_stats[code]['BCD'] += 1
        except Exception as e:
            logger.error(f"分析Top标的失败: {e}")
        result = [{'code': k, **v} for k, v in code_stats.items()]
        result.sort(key=lambda x: -x['total'])
        return result

    def _analyze_backtest(self) -> dict:
        """分析后验CSV: 胜率/策略对比/检查点"""
        path = os.path.join(self.log_dir, f"backtest_{self.today}.csv")
        if not os.path.exists(path):
            return {}
        try:
            records = []
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    records.append(row)

            if not records:
                return {}

            total = len(records)
            positive = sum(1 for r in records if float(r.get('final_pnl', 0)) > 0)
            win_rate = round(positive / total * 100, 1)
            avg_pnl = round(sum(float(r.get('final_pnl', 0)) for r in records) / total, 2)

            by_strategy = {}
            for strat in ['chase', 'dip']:
                subset = [r for r in records if r.get('strategy') == strat]
                if subset:
                    s_total = len(subset)
                    s_positive = sum(1 for r in subset if float(r.get('final_pnl', 0)) > 0)
                    pnls = [float(r['final_pnl']) for r in subset]
                    by_strategy[strat] = {
                        'total': s_total,
                        'win_rate': round(s_positive / s_total * 100, 1),
                        'avg_pnl': round(sum(pnls) / s_total, 2),
                        'best': max(zip(pnls, subset), key=lambda x: x[0])[1] if pnls else {},
                        'worst': min(zip(pnls, subset), key=lambda x: x[0])[1] if pnls else {},
                    }

            # 检查点
            checkpoint_keys = ['10s', '30s', '60s', '180s', '300s']
            by_checkpoint = {}
            for cp in checkpoint_keys:
                cp_label = f"{cp}_pnl"
                cp_data = [float(r[cp_label]) for r in records if cp_label in r and r[cp_label]]
                if cp_data:
                    by_checkpoint[cp] = {
                        'count': len(cp_data),
                        'avg_pnl': round(sum(cp_data) / len(cp_data), 2),
                        'win_rate': round(sum(1 for p in cp_data if p > 0) / len(cp_data) * 100, 1),
                        'best': round(max(cp_data), 2),
                        'worst': round(min(cp_data), 2),
                    }

            return {
                'total': total,
                'win_rate': win_rate,
                'avg_pnl': avg_pnl,
                'by_strategy': by_strategy,
                'by_checkpoint': by_checkpoint,
            }
        except Exception as e:
            logger.error(f"分析后验CSV失败: {e}")
            return {}

    def save_and_print(self):
        """生成报告并打印+保存"""
        report = self.generate()
        print(report)

        # 保存到文件
        report_path = os.path.join(self.log_dir, f"report_{self.today}.md")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"盘后报告已保存: {report_path}")


def run_post_market(log_dir: str = "logs"):
    """入口函数"""
    reporter = PostMarketReporter(log_dir)
    reporter.save_and_print()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_post_market()
