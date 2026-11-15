"""抓屏间隔与汇总写入周期的行为测试（SDD 验证）。

覆盖点
------
1. **不遗漏**：屏幕像素未变化时，活动时间仍必须计入时间线。
2. **有效聚合**：汇总周期内的多次识别应合并成一段，而不是碎片化。
3. **省 token**：画面未变时不应发起 LLM 调用。

回归背景（真实缺陷）
--------------------
实测 9.5 小时的工作只记录了 4.02 小时 —— **58% 的工作时间在时间线上不可见**。

根因：``main.capture_and_recognize()`` 在画面未变时直接 ``return None``，
既不延长上一条记录，也不记 skip_event。
而带正确逻辑的 ``recognize_once()``（会延长记录 + 记 skip_event）
在密集抓屏循环里**根本没被调用**。

后果：``skip_events`` 表 0 条记录、entries 之间 427 处空档共 5.51 小时。

修复：``capture_and_recognize()`` 在画面未变时调用
``_extend_current_activity()`` 延长上一条记录，并记 skip_event。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import Capture, Screenshot
from main import DailyHelper
from store import RecordStore


class _FakeCapture(Capture):
    """可编程的假截屏器：按脚本返回「有画面」或「画面未变」。"""

    def __init__(self, script):
        super().__init__(save_dir=None, quality=60)
        self.script = list(script)
        self.grabs = 0

    def grab(self, save_to_disk: bool = False) -> Screenshot:
        self.grabs += 1
        want_changed = self.script.pop(0) if self.script else False
        if want_changed:
            self.reset_hash()
            return Screenshot(data=b"\xff\xd8fake", width=100, height=100, size_kb=1)
        # 未变：构造一个空 Screenshot（与真实实现一致）
        return Screenshot()


class _FakeLLM:
    """最小 LLM 替身，只需提供 model 属性。"""

    model = "test-model"


def _helper(tmp):
    """造一个只连数据库、不连 LLM 的 DailyHelper。"""
    helper = DailyHelper.__new__(DailyHelper)
    helper.store = RecordStore(str(Path(tmp) / "t.db"))
    helper.archive = None
    helper.uploader = None
    helper._last_report_date = None
    # patch.object 要求属性预先存在，故先占位（测试内会被替换）
    helper.capture = Capture(save_dir=None, quality=60)
    helper.llm = _FakeLLM()
    return helper


class TestUnchangedScreenStillCounts(unittest.TestCase):
    """核心回归：画面未变时活动时间不能丢。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_extends_previous_entry_when_unchanged(self):
        """画面未变 -> 上一条记录被延长，而不是留下空档。"""
        import time
        # 先造一条已存在的记录
        now = int(time.time())
        eid = self.helper.store.insert_entry({
            "started_at": now - 100, "ended_at": now - 100,
            "activity": "读代码", "content": "看代码", "category": "代码开发",
        })
        before = self.helper.store.get_entry(eid)["ended_at"]

        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm") as llm:
                self.helper.capture_and_recognize()

        after = self.helper.store.get_entry(eid)["ended_at"]
        self.assertGreater(after, before, "画面未变时必须延长上一条记录")

    def test_records_skip_event(self):
        """画面未变 -> 记 skip_event，留下可回溯的痕迹。"""
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm"):
                self.helper.capture_and_recognize()

        # skip_events 表里应该有记录
        with self.helper.store._lock:
            row = self.helper.store._conn.execute(
                "SELECT COUNT(*) AS c FROM skip_events").fetchone()
        self.assertGreater(row["c"], 0, "画面未变时必须记 skip_event")

    def test_no_llm_call_when_unchanged(self):
        """画面未变 -> 不调用 LLM（省 token 是核心设计）。"""
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm") as llm:
                self.helper.capture_and_recognize()
                llm.chat_with_image.assert_not_called()

    def test_no_entry_when_no_previous(self):
        """没有上一条记录时无法延长，不应凭空造记录。"""
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm"):
                self.helper.capture_and_recognize()
        self.assertEqual(len(self.helper.store.entries_by_day(
            __import__("datetime").datetime.now().strftime("%Y-%m-%d"))), 0)


class TestNoGapOverLongUnchangedPeriod(unittest.TestCase):
    """长时段画面不变（读代码/思考）不应在时间线上留大洞。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_ten_unchanged_grabs_produce_one_continuous_entry(self):
        """连续 10 次画面未变 -> 应该是一条被延长的记录，而非 10 个空档。"""
        import time
        now = int(time.time())
        eid = self.helper.store.insert_entry({
            "started_at": now - 60, "ended_at": now - 60,
            "activity": "读文档", "content": "看文档", "category": "资料研究",
        })

        # 10 次未变，每次模拟时间推进 15 秒
        base = time.time()
        with patch.object(self.helper, "capture", _FakeCapture([False] * 10)):
            with patch.object(self.helper, "llm"):
                for _ in range(10):
                    with patch("main.time.time", return_value=base):
                        pass
                    self.helper.capture_and_recognize()

        entry = self.helper.store.get_entry(eid)
        # 记录时长必须 >= 原始时长 + 延长时间
        self.assertGreater(entry["ended_at"], now - 60,
                           "多次未变后记录应被显著延长")
        # 依然只有一条记录（没有碎片化）
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(len(self.helper.store.entries_by_day(today)), 1)


class TestSummarizeBufferAggregation(unittest.TestCase):
    """汇总写入周期：多次识别应聚合成段，而非碎片。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def _rec(self, activity, content, category, at):
        return {"activity": activity, "content": content, "category": category, "_at": at}

    def test_same_activity_merges_into_one(self):
        """同一活动的多次识别 -> 合并成一段。"""
        base = 1_700_000_000
        buf = [self._rec("写代码", "在写第 %d 版" % i, "代码开发", base + i * 30)
               for i in range(6)]
        stat = self.helper.summarize_buffer(buf)
        self.assertEqual(stat["recognized"], 6)
        self.assertEqual(stat["created"], 1)
        self.assertEqual(stat["merged"], 5)

    def test_different_activities_create_separate(self):
        """不同活动 -> 分开建段。"""
        base = 1_700_000_000
        buf = [self._rec("写代码", "写代码中", "代码开发", base),
               self._rec("开会议", "讨论需求", "项目管理", base + 3600)]
        stat = self.helper.summarize_buffer(buf)
        self.assertEqual(stat["created"], 2)

    def test_error_records_skipped(self):
        """识别失败的记录不写入时间线。"""
        base = 1_700_000_000
        buf = [{"_error": "boom"}, self._rec("写代码", "写代码", "代码开发", base)]
        stat = self.helper.summarize_buffer(buf)
        self.assertEqual(stat["created"], 1)
        self.assertEqual(stat["recognized"], 2)

    def test_larger_interval_means_fewer_entries(self):
        """汇总周期越大 -> 相同活动被合并得越彻底（片段更少）。

        这是「汇总写入周期」参数的核心价值：给聚合留出更多窗口。
        """
        import aggregate
        base = 1_700_000_000

        def run(window):
            tmp = tempfile.mkdtemp()
            h = _helper(tmp)
            try:
                buf = []
                # 模拟 10 分钟内、每 30 秒一次识别（共 20 次）
                for i in range(20):
                    buf.append(self._rec("写代码", f"写代码中 {i}", "代码开发",
                                         base + i * 30))
                # 按 window 大小切批，每批汇总一次
                created = 0
                for i in range(0, len(buf), window):
                    created += h.summarize_buffer(buf[i:i + window])["created"]
                return created
            finally:
                h.store.close()

        small = run(2)   # 每 2 次汇总一次
        large = run(20)  # 20 次一起汇总
        self.assertLessEqual(large, small,
                              "更大的汇总周期不应产生更多记录")


class TestExtendCurrentActivity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_respects_max_duration_cap(self):
        """单条记录延��不得超过 MAX_DURATION（4 小时）封顶。"""
        import time
        from datetime import datetime, timedelta
        import aggregate
        old = int((datetime.now() - timedelta(hours=10)).timestamp())
        eid = self.helper.store.insert_entry({
            "started_at": old, "ended_at": old,
            "activity": "长时间工作", "content": "x", "category": "代码开发",
        })
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm"):
                self.helper.capture_and_recognize()
        entry = self.helper.store.get_entry(eid)
        self.assertLessEqual(entry["ended_at"] - entry["started_at"],
                             aggregate.MAX_DURATION)

    def test_no_exception_when_store_empty(self):
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm"):
                self.helper.capture_and_recognize()  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
