"""report 模块的测试（SDD 验证）。

覆盖点：时长格式化、时钟格式化、统计聚合、日报生成、AI prompt 构造。
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report import (
    format_duration,
    format_clock,
    aggregate_stats,
    build_report,
    build_ai_prompt,
)


def ts(hour, minute=0):
    """构造 2026-11-15 某时刻的 Unix 时间戳。"""
    return int(datetime(2026, 11, 15, hour, minute, 0).timestamp())


class TestFormat(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(format_duration(0), "0 分钟")
        self.assertEqual(format_duration(1800), "30 分钟")
        self.assertEqual(format_duration(5400), "1 小时 30 分钟")

    def test_clock(self):
        self.assertEqual(format_clock(ts(9, 30)), "09:30")


class TestAggregateStats(unittest.TestCase):
    def test_empty(self):
        s = aggregate_stats([])
        self.assertEqual(s["total_segments"], 0)
        self.assertEqual(s["total_duration"], 0)

    def test_normal(self):
        entries = [
            {"started_at": ts(9, 0), "ended_at": ts(10, 0), "content": "写代码", "category": "代码开发"},
            {"started_at": ts(10, 30), "ended_at": ts(11, 0), "content": "开会", "category": "项目管理"},
        ]
        s = aggregate_stats(entries)
        self.assertEqual(s["total_segments"], 2)
        self.assertEqual(s["total_duration"], 5400)  # 1h + 0.5h = 90 分钟
        self.assertEqual(s["category_durations"]["代码开发"], 3600)
        self.assertEqual(s["category_durations"]["项目管理"], 1800)
        # 最长空档 = 10:30 - 10:00 = 30 分钟
        self.assertEqual(s["max_gap"], 1800)
        # 集中时段 top_hour = 9 点（3600 秒）与 10 点（1800+... 实际 10 点有 30min 开会）
        self.assertIn(s["top_hour"], (9, 10))


class TestBuildReport(unittest.TestCase):
    def test_empty(self):
        r = build_report([], "2026-11-15")
        self.assertIn("今天还没有工作记录", r)

    def test_normal_contains_sections(self):
        entries = [
            {"started_at": ts(9, 0), "ended_at": ts(10, 0), "content": "写代码", "category": "代码开发"},
        ]
        r = build_report(entries, "2026-11-15")
        self.assertIn("# 工作日报", r)
        self.assertIn("概览", r)
        self.assertIn("分类占比", r)
        self.assertIn("活动时间线", r)
        self.assertIn("写代码", r)

    def test_with_ai_analysis(self):
        entries = [
            {"started_at": ts(9, 0), "ended_at": ts(10, 0), "content": "写代码", "category": "代码开发"},
        ]
        r = build_report(entries, "2026-11-15", ai_analysis="### 观察\n今天很专注。")
        self.assertIn("AI 分析", r)
        self.assertIn("今天很专注", r)


class TestBuildAiPrompt(unittest.TestCase):
    def test_contains_stats_and_timeline(self):
        entries = [
            {"started_at": ts(9, 0), "ended_at": ts(10, 0), "content": "写代码", "category": "代码开发"},
        ]
        s = aggregate_stats(entries)
        p = build_ai_prompt(s, entries)
        self.assertIn("观察", p)
        self.assertIn("证据", p)
        self.assertIn("建议", p)
        self.assertIn("小结", p)
        self.assertIn("写代码", p)


if __name__ == "__main__":
    unittest.main()
