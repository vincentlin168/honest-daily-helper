"""修复前后覆盖率对比的测试（SDD 验证）。

核心主张：**修复真的有用**，而且这个主张能被数据自动验证，
而不是靠文档里的一句话。

对比的自动分界：``skip_events`` 里的第一条记录 —— 旧版画面不变时
根本不写 skip_events，所以第一条 skip_event 的时间就是新逻辑上线时间。
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import RecordStore


class TestFixComparison(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "fixcmp.db"))
        # 基准时间：今天 00:00（落在统计窗口内）
        self.base = int(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())

    def tearDown(self):
        self.store.close()

    def _add_call(self, at):
        self.store.insert_llm_call({"at": at, "ok": True, "model": "m",
                                     "input_tokens": 10, "output_tokens": 5})

    def _add_entry(self, started, ended):
        self.store.insert_entry({
            "started_at": started, "ended_at": ended,
            "activity": "工作", "content": "", "category": "代码开发",
        })

    def test_no_skip_events_means_no_split(self):
        """没有跳过记录 -> 无法分界 -> before/after 都是 None。"""
        self._add_call(self.base + 10)
        self._add_entry(self.base, self.base + 60)
        eff = self.store.interval_effectiveness(days=1)
        self.assertIsNone(eff["fix_split_ts"])
        self.assertIsNone(eff["before_fix"])
        self.assertIsNone(eff["after_fix"])

    def test_split_detected_from_first_skip(self):
        """分界点 = 第一条 skip_event 的时间。"""
        self.store.insert_skip_event("unchanged", at=self.base + 2000)
        eff = self.store.interval_effectiveness(days=1)
        self.assertEqual(eff["fix_split_ts"], self.base + 2000)

    def test_before_and_after_coverage_computed(self):
        """修复前后分别算覆盖率，修复后应更高。"""
        t = self.base
        # 修复前：2 条记录，跨度 600s，总时长 120s -> 20%
        self._add_entry(t, t + 60)
        self._add_entry(t + 600, t + 660)
        for x in range(t, t + 700, 100):
            self._add_call(x)
        # 修复上线
        self.store.insert_skip_event("unchanged", at=t + 1000)
        # 修复后：2 条记录，跨度 600s，总时长 600s -> 100%
        self._add_entry(t + 1200, t + 1800)
        self._add_entry(t + 1800, t + 2400)
        for x in range(t + 1200, t + 2400, 100):
            self._add_call(x)

        eff = self.store.interval_effectiveness(days=1)
        b = eff["before_fix"]
        a = eff["after_fix"]
        self.assertIsNotNone(b)
        self.assertIsNotNone(a)
        self.assertEqual(b["segments"], 2)
        self.assertEqual(a["segments"], 2)
        # 跨度 = 首条开始到末条结束 = 660s，总时长 120s
        self.assertAlmostEqual(b["coverage"], 120 / 660, places=2)
        self.assertAlmostEqual(a["coverage"], 1.0, places=2)
        self.assertGreater(a["coverage"], b["coverage"],
                           "修复后覆盖率应该更高")

    def test_zero_duration_counted(self):
        """零秒条目被单独计数。"""
        t = self.base
        self._add_entry(t, t)              # 零秒
        self._add_entry(t + 10, t + 70)
        self.store.insert_skip_event("unchanged", at=t)
        eff = self.store.interval_effectiveness(days=1)
        self.assertEqual(eff["after_fix"]["zero_duration"], 1)

    def test_coverage_of_empty_is_zero(self):
        self.assertEqual(RecordStore._coverage_of([])["coverage"], 0.0)
        self.assertEqual(RecordStore._coverage_of([])["segments"], 0)

    def test_single_entry_coverage_is_full(self):
        """只有一条记录时，跨度就是它自身时长 -> 覆盖率 100%。

        这不是除零 bug：一条记录被完整记录下来，覆盖率就该是 100%。
        """
        one = [{"started_at": 100, "ended_at": 160}]
        res = RecordStore._coverage_of(one)
        self.assertEqual(res["coverage"], 1.0)
        self.assertEqual(res["recorded_seconds"], 60)

    def test_comparison_is_empty_safe(self):
        """空库调用不应抛异常。"""
        eff = self.store.interval_effectiveness(days=1)
        self.assertEqual(eff["segments"], 0)
        self.assertEqual(eff["coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
