"""store 与 aggregate 模块的测试（SDD 验证）。

覆盖点：建表、插入、延长、查询、聚合决策（新建/合并）。
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aggregate
from store import RecordStore


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "test.db"))

    def tearDown(self):
        self.store.close()

    def test_insert_and_latest(self):
        eid = self.store.insert_entry({
            "started_at": 1000, "ended_at": 1000,
            "content": "写代码", "category": "代码开发", "model": "deepseek-flash",
        })
        self.assertGreater(eid, 0)
        latest = self.store.latest_entry()
        self.assertEqual(latest["content"], "写代码")
        self.assertEqual(latest["category"], "代码开发")

    def test_extend_entry_capped(self):
        eid = self.store.insert_entry({
            "started_at": 1000, "ended_at": 1000,
            "content": "开会", "category": "项目管理",
        })
        # 延长到 5000，但 max_duration=3000，应被 cap 到 started_at+3000=4000
        self.store.extend_entry(eid, 5000, max_duration=3000)
        latest = self.store.latest_entry()
        self.assertEqual(latest["ended_at"], 4000)

    def test_entries_by_day(self):
        # 2026-11-15 当天 12:00 的 Unix 时间戳
        dt = datetime(2026, 11, 15, 12, 0, 0)
        ts = int(dt.timestamp())
        self.store.insert_entry({
            "started_at": ts, "ended_at": ts, "content": "当天记录", "category": "其他",
        })
        self.store.insert_entry({
            "started_at": ts + 90000, "ended_at": ts + 90000,
            "content": "跨天记录", "category": "其他",
        })
        rows = self.store.entries_by_day("2026-11-15")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "当天记录")

    def test_insert_llm_call_and_skip(self):
        self.store.insert_llm_call({
            "model": "deepseek-flash", "ok": True,
            "input_tokens": 100, "output_tokens": 50,
        })
        self.store.insert_skip_event("unchanged")
        # 不抛异常即通过


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "test.db"))

    def tearDown(self):
        self.store.close()

    def test_empty_store_creates(self):
        action, eid = aggregate.apply(
            self.store, "写代码", "正在写代码", "代码开发", model="deepseek-flash", now=1000
        )
        self.assertEqual(action, "created")
        self.assertGreater(eid, 0)

    def test_same_content_merges(self):
        aggregate.apply(self.store, "写代码", "正在写代码", "代码开发", now=1000)
        action, eid = aggregate.apply(self.store, "写代码", "继续写代码", "代码开发", now=1300)
        self.assertEqual(action, "merged")
        latest = self.store.latest_entry()
        self.assertEqual(latest["ended_at"], 1300)  # 延长了

    def test_diff_content_creates(self):
        aggregate.apply(self.store, "写代码", "正在写代码", "代码开发", now=1000)
        action, _ = aggregate.apply(self.store, "开会", "参加项目会", "项目管理", now=1300)
        self.assertEqual(action, "created")
        # 两条记录：latest 是「开会」，倒数第二条是「写代码」
        latest = self.store.latest_entry()
        self.assertEqual(latest["activity"], "开会")

    def test_should_merge_content(self):
        self.assertTrue(aggregate.should_merge_content("写代码", "写代码"))
        self.assertTrue(aggregate.should_merge_content(" 写代码 ", "写代码"))
        self.assertFalse(aggregate.should_merge_content("写代码", "开会"))

    def test_should_merge_similar_content(self):
        # 语义相同但措辞不同（视觉模型常见现象），应判定为可合并
        a = "开发日报助手工具"
        b = "开发日报助手"
        self.assertTrue(aggregate.should_merge_content(a, b))

    def test_should_not_merge_unrelated(self):
        self.assertFalse(aggregate.should_merge_content("开发日报助手工具", "浏览新闻资讯"))


if __name__ == "__main__":
    unittest.main()
