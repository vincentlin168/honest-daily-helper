"""聚合语义测试：合并到底合并了什么（SDD 验证）。

用户疑问
--------
「每 120 秒合并 8 次抓取，是把 8 份信息合成一份吗？」

实测结论
--------
**不是合成，是取舍。** 合并判定只看 `activity` 标签的相似度，
`content` 完全不参与判定；合并时若不额外处理，
只有**第一次**识别的描述会留下，后面 7 次的描述全部丢弃。

那 8 次识别的真实价值：
1. 确认活动没变（把时段延长成一段，而不是切碎）—— 真实价值
2. 提供更详细的描述 —— 修复前被浪费了

本测试锁定：合并时保留**更详细**的那份描述。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregate import apply, should_merge_content
from store import RecordStore


class TestMergeSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "m.db"))

    def tearDown(self):
        self.store.close()

    def test_merge_keeps_richer_description(self):
        """核心回归：后续更详细的描述必须替换掉第一次的。"""
        base = 1_700_000_000
        action, eid = apply(self.store, "写代码", "简短", "代码开发", now=base)
        self.assertEqual(action, "created")

        action, eid = apply(self.store, "写代码",
                            "在编辑器中实现聚合模块，修改了 apply 函数的合并分支逻辑",
                            "代码开发", now=base + 15)
        self.assertEqual(action, "merged")

        entry = self.store.get_entry(eid)
        self.assertIn("合并分支", entry["content"],
                      "更详细的描述必须被保留，不能丢弃")

    def test_merge_keeps_first_when_new_is_shorter(self):
        """新描述更短时，保留原有的（不倒退）。"""
        base = 1_700_000_000
        _, eid = apply(self.store, "写代码", "很详细的一段描述内容", "代码开发", now=base)
        apply(self.store, "写代码", "短", "代码开发", now=base + 15)
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["content"], "很详细的一段描述内容")

    def test_merge_extends_duration(self):
        """合并的核心价值：把时段延长成一段。"""
        base = 1_700_000_000
        _, eid = apply(self.store, "写代码", "描述", "代码开发", now=base)
        for i in range(1, 8):
            apply(self.store, "写代码", "描述", "代码开发", now=base + i * 15)
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["ended_at"] - entry["started_at"], 105)

    def test_eight_recognitions_produce_one_segment(self):
        """8 次识别 -> 1 段，而不是 8 段。"""
        base = 1_700_000_000
        for i in range(8):
            apply(self.store, "写代码", f"第{i}次", "代码开发", now=base + i * 15)
        rows = self.store.entries_around(base)
        self.assertEqual(len(rows), 1)

    def test_different_activity_creates_new_segment(self):
        """活动不同 -> 分开建段。"""
        base = 1_700_000_000
        apply(self.store, "写代码", "描述", "代码开发", now=base)
        action, _ = apply(self.store, "看邮件", "描述", "其他", now=base + 15)
        self.assertEqual(action, "created")

    def test_merge_decision_ignores_content(self):
        """合并判定只看 activity，content 不参与。"""
        self.assertTrue(should_merge_content("写代码", "写代码"))
        self.assertTrue(should_merge_content("写代码", "在编写代码逻辑"))
        self.assertFalse(should_merge_content("写代码", "在看邮件"))
        self.assertFalse(should_merge_content("", "写代码"))


if __name__ == "__main__":
    unittest.main()
