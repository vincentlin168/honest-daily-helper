"""概览统计 / 热力图 / 成本透明度的测试（SDD 验证）。

覆盖点：
- overview.build_overview 的统计正确性（段数、时长、最长连续、峰值时段、分类 Top）
- overview.compute_cost 的金额换算（Decimal 精度）
- store.hourly_histogram 的跨小时 / 跨天分摊
- store.token_usage_by_day 的用量汇总
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overview import build_overview, compute_cost
from store import RecordStore


def _entry(start, end, activity="写代码", category="代码开发", content="干活"):
    return {"started_at": int(start), "ended_at": int(end),
            "activity": activity, "category": category, "content": content}


class TestLongestContinuousWork(unittest.TestCase):
    def test_single_entry(self):
        from overview import _longest_continuous_work
        dur, count = _longest_continuous_work([_entry(0, 3600)])
        self.assertEqual(dur, 3600)
        self.assertEqual(count, 1)

    def test_contiguous_entries_merge(self):
        """间隔小于阈值的相邻记录视为连续工作。"""
        from overview import _longest_continuous_work
        base = 1_700_000_000
        entries = [_entry(base, base + 1800),
                   _entry(base + 1800 + 60, base + 3600 + 60)]  # 间隔 60s
        dur, count = _longest_continuous_work(entries)
        self.assertEqual(count, 2)
        self.assertEqual(dur, 3660)

    def test_large_gap_breaks_continuity(self):
        from overview import _longest_continuous_work
        base = 1_700_000_000
        entries = [_entry(base, base + 1800),
                   _entry(base + 3600 + 7200, base + 3600 + 9000)]  # 间隔 2 小时
        dur, count = _longest_continuous_work(entries)
        self.assertEqual(count, 1)
        self.assertEqual(dur, 1800)

    def test_empty(self):
        from overview import _longest_continuous_work
        self.assertEqual(_longest_continuous_work([]), (0, 0))


class TestComputeCost(unittest.TestCase):
    def test_no_price_returns_none(self):
        """没填单价就不编造金额 —— 只报 token 数。"""
        r = compute_cost({"input_tokens": 1000, "output_tokens": 500})
        self.assertFalse(r["has_price"])
        self.assertIsNone(r["cost"])
        self.assertEqual(r["input_tokens"], 1000)

    def test_cost_with_price(self):
        # 1M 输入 @ ¥2/1M + 1M 输出 @ ¥8/1M
        r = compute_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, 2.0, 8.0)
        self.assertTrue(r["has_price"])
        self.assertAlmostEqual(r["cost"], 10.0, places=6)

    def test_zero_tokens_zero_cost(self):
        r = compute_cost({"input_tokens": 0, "output_tokens": 0}, 2.0, 8.0)
        self.assertAlmostEqual(r["cost"], 0.0)

    def test_decimal_precision(self):
        """小额 token 也应精确，不因浮点误差丢掉分。"""
        r = compute_cost({"input_tokens": 1000, "output_tokens": 500}, 2.0, 8.0)
        # 1000/1e6*2 + 500/1e6*8 = 0.002 + 0.004 = 0.006
        self.assertAlmostEqual(r["cost"], 0.006, places=9)


class TestBuildOverview(unittest.TestCase):
    def test_overview_fields(self):
        base = 1_700_000_000
        entries = [_entry(base, base + 3600, category="代码开发"),
                   _entry(base + 3700, base + 5400, category="数据复盘")]
        usage = {"input_tokens": 2000, "output_tokens": 1000, "calls": 5, "failures": 1}
        d = build_overview(entries, usage, 2.0, 8.0)
        self.assertEqual(d["total_segments"], 2)
        self.assertEqual(d["total_duration"], 3600 + 1700)
        self.assertEqual(d["llm_calls"], 5)
        self.assertEqual(d["llm_failures"], 1)
        self.assertTrue(d["cost"]["has_price"])
        self.assertAlmostEqual(d["cost"]["cost"], 2000/1e6*2 + 1000/1e6*8, places=9)
        self.assertGreaterEqual(len(d["top_categories"]), 1)

    def test_empty_day(self):
        d = build_overview([], {}, 0, 0)
        self.assertEqual(d["total_segments"], 0)
        self.assertEqual(d["total_duration"], 0)
        self.assertEqual(d["top_categories"], [])

    def test_top_categories_limited(self):
        base = 1_700_000_000
        entries = [_entry(base + i * 7200, base + i * 7200 + 3600,
                          category=f"cat{i}") for i in range(6)]
        d = build_overview(entries, {}, 0, 0, top_categories=3)
        self.assertEqual(len(d["top_categories"]), 3)
        self.assertEqual(d["category_count"], 6)


class TestHourlyHistogram(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "h.db"))

    def tearDown(self):
        self.store.close()

    def test_shape(self):
        grid = self.store.hourly_histogram(7)
        self.assertEqual(len(grid), 7)
        for row in grid:
            self.assertEqual(len(row["hours"]), 24)
            self.assertIn("date", row)
            self.assertIn("weekday", row)

    def test_entry_placed_in_correct_hour(self):
        now = datetime.now()
        ts = int(now.replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
        self.store.insert_entry({"started_at": ts, "ended_at": ts + 1800,
                                  "content": "x", "category": "代码开发"})
        grid = self.store.hourly_histogram(7)
        today = now.strftime("%Y-%m-%d")
        row = next(r for r in grid if r["date"] == today)
        self.assertEqual(row["hours"][9], 1800)
        self.assertEqual(sum(row["hours"]), 1800)

    def test_cross_hour_split(self):
        """跨小时的活动应按实际秒数分摊到两个小时。"""
        now = datetime.now()
        start = int(now.replace(hour=10, minute=50, second=0, microsecond=0).timestamp())
        end = start + 20 * 60  # 10:50 -> 11:10
        self.store.insert_entry({"started_at": start, "ended_at": end,
                                 "content": "x", "category": "代码开发"})
        grid = self.store.hourly_histogram(7)
        today = now.strftime("%Y-%m-%d")
        row = next(r for r in grid if r["date"] == today)
        self.assertEqual(row["hours"][10], 600)   # 10:50-11:00 = 600s
        self.assertEqual(row["hours"][11], 600)   # 11:00-11:10 = 600s
        self.assertEqual(sum(row["hours"]), 1200)

    def test_days_parameter(self):
        self.assertEqual(len(self.store.hourly_histogram(1)), 1)
        self.assertEqual(len(self.store.hourly_histogram(30)), 30)
        # 越界钳制
        self.assertEqual(len(self.store.hourly_histogram(0)), 1)
        self.assertEqual(len(self.store.hourly_histogram(999)), 90)


class TestTokenUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "u.db"))

    def tearDown(self):
        self.store.close()

    def test_usage_by_day(self):
        now = int(datetime.now().timestamp())
        self.store.insert_llm_call({"at": now, "model": "m", "ok": True,
                                    "input_tokens": 1000, "output_tokens": 200})
        self.store.insert_llm_call({"at": now, "model": "m", "ok": False,
                                    "input_tokens": 300, "output_tokens": 0,
                                    "error": "boom"})
        today = datetime.now().strftime("%Y-%m-%d")
        u = self.store.token_usage_by_day(today)
        self.assertEqual(u["input_tokens"], 1300)
        self.assertEqual(u["output_tokens"], 200)
        self.assertEqual(u["total_tokens"], 1500)
        self.assertEqual(u["calls"], 2)
        self.assertEqual(u["failures"], 1)

    def test_usage_other_day_is_empty(self):
        self.store.insert_llm_call({"at": int(datetime.now().timestamp()), "model": "m",
                                    "ok": True, "input_tokens": 999})
        u = self.store.token_usage_by_day("2020-01-01")
        self.assertEqual(u["total_tokens"], 0)
        self.assertEqual(u["calls"], 0)


if __name__ == "__main__":
    unittest.main()
