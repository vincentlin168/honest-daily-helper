"""main 编排的端到端测试（SDD 验证）。

覆盖点：完整识别流程（截屏→识别→聚合→存储）、日报生成、变更检测跳过。
用 mock 隔离真实截屏与真实 API 调用。
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import Screenshot
from main import DailyHelper
from store import RecordStore


def _make_helper(tmp):
    cfg = Path(tmp) / "config.json"
    helper = DailyHelper(str(cfg))
    helper.store.close()  # 关闭 _init_modules 里默认创建的 store
    helper.store = RecordStore(str(Path(tmp) / "e2e.db"))
    return helper


class TestGenerateReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _make_helper(self.tmp)
        # 用「今天中午」而不是「现在 - 1 小时」：
        # 后者在接近午夜时会落到昨天，导致 generate_report() 取不到记录。
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        noon_ts = int(noon.timestamp())
        self.helper.store.insert_entry({
            "started_at": noon_ts - 3600, "ended_at": noon_ts,
            "content": "写代码", "category": "代码开发", "model": "deepseek-flash",
        })

    def tearDown(self):
        self.helper.store.close()

    def test_generate_report_contains_content(self):
        report_md = self.helper.generate_report()
        self.assertIn("工作日报", report_md)
        self.assertIn("写代码", report_md)
        self.assertIn("代码开发", report_md)

    def test_generate_report_empty_day(self):
        report_md = self.helper.generate_report("2020-01-01")
        self.assertIn("今天还没有工作记录", report_md)


class TestRecognizeOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _make_helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_full_flow_creates_entry(self):
        shot = Screenshot(data=b"\xff\xd8\xff\xd9", width=10, height=10, size_kb=1)
        with patch.object(self.helper.capture, "grab", return_value=shot), \
             patch.object(self.helper.llm, "chat_with_image",
                          return_value=('{"activity": "写代码", "content": "写代码", "category": "代码开发"}',
                                        {"prompt_tokens": 100, "completion_tokens": 20})):
            result = self.helper.recognize_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["recognition"]["content"], "写代码")
        # 记录已入库
        latest = self.helper.store.latest_entry()
        self.assertEqual(latest["content"], "写代码")

    def test_skip_when_unchanged(self):
        empty = Screenshot()  # is_empty=True
        with patch.object(self.helper.capture, "grab", return_value=empty):
            result = self.helper.recognize_once()
        self.assertTrue(result["skipped"])

    def test_merge_same_content(self):
        shot = Screenshot(data=b"\xff\xd8\xff\xd9", width=10, height=10, size_kb=1)
        with patch.object(self.helper.capture, "grab", return_value=shot), \
             patch.object(self.helper.llm, "chat_with_image",
                          return_value=('{"activity": "写代码", "content": "写代码", "category": "代码开发"}', {})):
            self.helper.recognize_once()
            result = self.helper.recognize_once()  # 第二次内容相同
        self.assertEqual(result["action"], "merged")


if __name__ == "__main__":
    unittest.main()
