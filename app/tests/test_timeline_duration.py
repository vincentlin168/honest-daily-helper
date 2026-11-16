"""时间线时长语义测试（SDD 验证）。

用户疑问
--------
「每 15 秒做什么、到 120 秒做什么、合并了 8 次抓取吗、是否有效？」

实测发现的时间语义缺陷
----------------------
**缺陷 A：识别次数被误当作信息合并**
``aggregate.apply`` 的合并判定只看 ``activity`` 标签相似度，
``content`` 完全不参与。合并分支只更新 ``ended_at``，
新 content 被直接丢弃 —— 8 次识别里只有第 1 次的描述留下来。
等于花了 8 次的钱，只买了 1 次的信息。

**缺陷 B：活动时长为 0 秒**
``summarize_buffer`` 用 ``rec["_at"]``（识别时刻）作为 now，
新建记录时 ``started_at = ended_at = now`` -> 时长 0 秒。
两次识别之间的时间、以及最后识别到汇总时刻的尾巴，全部从时间线上消失。

修复
----
- 合并时保留**更详细**的那份描述
- ``aggregate.apply`` 新增 ``started_at`` 参数
- ``summarize_buffer`` 用「上一条识别时刻」作 started_at，
  并把最后一段延伸到汇总时刻
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import DailyHelper
from store import RecordStore

BASE = 1_700_000_000


class _FakeLLM:
    model = "test-model"


def _helper():
    tmp = tempfile.mkdtemp()
    h = DailyHelper.__new__(DailyHelper)
    h.store = RecordStore(str(Path(tmp) / "t.db"))
    h.archive = None
    h.uploader = None
    h._last_report_date = None
    h.llm = _FakeLLM()
    return h


def _rec(activity, content, at):
    return {"activity": activity, "content": content, "category": "代码开发", "_at": at}


class TestSummaryTimelineDuration(unittest.TestCase):
    """缺陷 B：时长不能为 0。"""

    def setUp(self):
        self.h = _helper()

    def tearDown(self):
        self.h.store.close()

    def test_eight_recognitions_cover_full_window(self):
        """120 秒内 8 次识别（每 15s）-> 时长应覆盖整个窗口。"""
        buf = [_rec("写代码", f"第{i}次", BASE + i * 15) for i in range(8)]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        rows = self.h.store.entries_by_day("2023-11-15")
        total = sum(r["ended_at"] - r["started_at"] for r in rows)
        self.assertEqual(total, 120, "8 次识别跨越 120s，记录时长也应是 120s")

    def test_single_recognition_extends_to_summary(self):
        """只识别 1 次、之后画面一直不变 -> 时长应延伸到汇总时刻。"""
        buf = [_rec("读文档", "描述", BASE)]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        entry = self.h.store.latest_entry()
        self.assertEqual(entry["ended_at"] - entry["started_at"], 120)

    def test_gap_between_recognitions_counted(self):
        """两次识别之间的间隔应计入时长。"""
        buf = [_rec("写代码", "a", BASE), _rec("写代码", "b", BASE + 90)]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        rows = self.h.store.entries_by_day("2023-11-15")
        total = sum(r["ended_at"] - r["started_at"] for r in rows)
        self.assertEqual(total, 120)

    def test_no_zero_length_entries(self):
        """不应该产生时长为 0 的记录。"""
        buf = [_rec("写代码", f"第{i}次", BASE + i * 15) for i in range(8)]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        rows = self.h.store.entries_by_day("2023-11-15")
        for r in rows:
            self.assertGreater(r["ended_at"] - r["started_at"], 0,
                               "记录时长不应为 0 秒")

    def test_summary_at_defaults_to_now(self):
        """不传 summary_at 时应退回当前时间，不报错。"""
        buf = [_rec("写代码", "a", BASE)]
        stat = self.h.summarize_buffer(buf)
        self.assertEqual(stat["created"], 1)

    def test_empty_buffer_is_safe(self):
        stat = self.h.summarize_buffer([])
        self.assertEqual(stat["created"], 0)
        self.assertEqual(stat["merged"], 0)

    def test_error_records_ignored_but_counted(self):
        """识别失败的记录不写入，但计入 recognized。"""
        buf = [{"_error": "boom"}, _rec("写代码", "a", BASE)]
        stat = self.h.summarize_buffer(buf, summary_at=BASE + 120)
        self.assertEqual(stat["recognized"], 2)
        self.assertEqual(stat["created"], 1)


class TestRichDescriptionRetention(unittest.TestCase):
    """缺陷 A：8 次识别不该只留 1 份描述。"""

    def setUp(self):
        self.h = _helper()

    def tearDown(self):
        self.h.store.close()

    def test_richer_later_description_replaces(self):
        buf = [
            _rec("写代码", "短", BASE),
            _rec("写代码", "在编辑器中实现了聚合模块的合并分支逻辑与时间语义处理", BASE + 15),
        ]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        entry = self.h.store.latest_entry()
        self.assertIn("合并分支", entry["content"])

    def test_shorter_later_description_does_not_replace(self):
        buf = [
            _rec("写代码", "很长的一段描述内容不应该被替换掉", BASE),
            _rec("写代码", "短", BASE + 15),
        ]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        entry = self.h.store.latest_entry()
        self.assertIn("不应该被替换掉", entry["content"])


    def test_no_overlapping_entries(self):
        """相邻记录不应重叠（时间线应首尾相接）。"""
        buf = [
            _rec("写代码", "a", BASE),
            _rec("写代码", "b", BASE + 15),
            _rec("看邮件", "c", BASE + 30),
            _rec("看邮件", "d", BASE + 45),
            _rec("写代码", "e", BASE + 60),
        ]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        rows = self.h.store.entries_by_day("2023-11-15")
        for i in range(len(rows) - 1):
            self.assertGreaterEqual(
                rows[i + 1]["started_at"], rows[i]["ended_at"],
                "相邻记录不应重叠")

    def test_activity_switch_keeps_total_duration(self):
        """活动切换时，总时长仍应覆盖整个窗口。"""
        buf = [
            _rec("写代码", "a", BASE),
            _rec("写代码", "b", BASE + 15),
            _rec("看邮件", "c", BASE + 30),
            _rec("看邮件", "d", BASE + 45),
            _rec("写代码", "e", BASE + 60),
        ]
        self.h.summarize_buffer(buf, summary_at=BASE + 120)
        rows = self.h.store.entries_by_day("2023-11-15")
        total = sum(r["ended_at"] - r["started_at"] for r in rows)
        self.assertEqual(total, 120, "活动切换不应丢失时间")


if __name__ == "__main__":
    unittest.main()
