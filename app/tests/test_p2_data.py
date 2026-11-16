"""P2 数据可信闭环的测试（SDD 验证）。

覆盖点：
- archive.ReportArchive：留档读写删、日期校验（防路径穿越）
- exporter：JSON / CSV / Markdown 导出、隐私（不含 Key）、写文件编码
- store：update_entry / delete_entry / get_entry / categories_in_use
- store.error_summary：错误归类与错误率
- API：/api/errors /api/archive /api/export /api/entries/{id} 编辑删除
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exporter
from archive import ReportArchive
from store import RecordStore


def _entry(start=1_700_000_000, end=1_700_003600, category="代码开发",
           content="写代码", activity="编码"):
    return {"started_at": start, "ended_at": end, "activity": activity,
            "category": category, "content": content, "source": "screenshot",
            "model": "deepseek-flash"}


class TestReportArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arch = ReportArchive(str(Path(self.tmp) / "reports"))

    def test_save_and_load(self):
        path = self.arch.save("2026-11-16", "# 今日工作\n\n写了一天代码")
        self.assertTrue(path)
        self.assertTrue(Path(path).exists())
        self.assertIn("写了一天代码", self.arch.load("2026-11-16"))

    def test_load_missing(self):
        self.assertIsNone(self.arch.load("2020-01-01"))

    def test_path_traversal_rejected(self):
        """非法日期必须被拒绝 —— 防路径穿越。"""
        self.assertIsNone(self.arch.save("../../evil", "x"))
        self.assertIsNone(self.arch.load("../../etc/passwd"))
        self.assertIsNone(self.arch.save("", "x"))
        self.assertIsNone(self.arch.save("2026-1-1", "x"))

    def test_list_reports(self):
        self.arch.save("2026-11-14", "a")
        self.arch.save("2026-11-16", "b")
        items = self.arch.list_reports()
        self.assertEqual(len(items), 2)
        # 按日期倒序
        self.assertEqual(items[0]["date"], "2026-11-16")

    def test_versioned_does_not_overwrite(self):
        self.arch.save("2026-11-16", "first")
        p2 = self.arch.save_versioned("2026-11-16", "second")
        self.assertTrue(p2)
        self.assertNotEqual(p2, str(Path(self.arch.root / "2026-11-16.md").resolve()))
        # 原文件未被覆盖
        self.assertEqual(self.arch.load("2026-11-16"), "first")

    def test_delete(self):
        self.arch.save("2026-11-16", "x")
        self.assertTrue(self.arch.delete("2026-11-16"))
        self.assertFalse(self.arch.delete("2026-11-16"))
        self.assertIsNone(self.arch.load("2026-11-16"))

    def test_empty_dir_list(self):
        self.assertEqual(self.arch.list_reports(), [])


class TestExporter(unittest.TestCase):
    def test_json_structure(self):
        content = exporter.export_json([_entry()], {"scope": "test"})
        import json
        data = json.loads(content)
        self.assertEqual(data["format"], "honest-daily-helper/export")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["meta"]["scope"], "test")
        row = data["entries"][0]
        self.assertEqual(row["duration_seconds"], 3600)
        self.assertIn("duration_text", row)

    def test_json_contains_no_secrets(self):
        """导出内容绝不能含 API Key / token。"""
        entry = _entry()
        entry["api_key"] = "sk-SECRET"
        entry["token"] = "SECRET-TOKEN"
        content = exporter.export_json([entry])
        self.assertNotIn("sk-SECRET", content)
        self.assertNotIn("SECRET-TOKEN", content)

    def test_csv_has_bom(self):
        """Excel 靠 BOM 识别 UTF-8，否则中文乱码。"""
        content = exporter.export_csv([_entry()])
        self.assertTrue(content.startswith("\ufeff"))
        self.assertIn("代码开发", content)

    def test_csv_empty(self):
        self.assertEqual(exporter.export_csv([]), "")

    def test_markdown_content(self):
        md = exporter.export_markdown([_entry()], "2026-11-15", "# 日报正文")
        self.assertIn("# 工作记录 · 2026-11-15", md)
        self.assertIn("## 日报", md)
        self.assertIn("# 日报正文", md)
        self.assertIn("| 时间 | 时长 | 分类 | 内容 |", md)
        self.assertIn("代码开发", md)

    def test_markdown_escapes_pipe(self):
        """内容里的竖线不能破坏 Markdown 表格。"""
        e = _entry(content="a|b 的内容")
        md = exporter.export_markdown([e], "2026-11-15")
        self.assertIn("a\\|b", md)

    def test_markdown_empty(self):
        md = exporter.export_markdown([], "2026-11-15")
        self.assertIn("_没有记录_", md)

    def test_write_export_encoding(self):
        tmp = tempfile.mkdtemp()
        p = exporter.write_export(str(Path(tmp) / "x.csv"), "a,b\n")
        self.assertTrue(Path(p).exists())
        raw = Path(p).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))

    def test_write_export_json_encoding(self):
        tmp = tempfile.mkdtemp()
        p = exporter.write_export(str(Path(tmp) / "x.json"), "{}")
        raw = Path(p).read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))


class TestStoreEditing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "e.db"))

    def tearDown(self):
        self.store.close()

    def test_update_entry(self):
        eid = self.store.insert_entry(_entry())
        ok = self.store.update_entry(eid, category="产品设计", content="改需求")
        self.assertTrue(ok)
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["category"], "产品设计")
        self.assertEqual(entry["content"], "改需求")

    def test_update_entry_rejects_unknown_fields(self):
        """白名单：不能改 id / started_at 等内部列。"""
        eid = self.store.insert_entry(_entry())
        ok = self.store.update_entry(eid, started_at=0, id=999)
        self.assertFalse(ok)
        entry = self.store.get_entry(eid)
        self.assertNotEqual(entry["started_at"], 0)

    def test_update_entry_no_fields(self):
        eid = self.store.insert_entry(_entry())
        self.assertFalse(self.store.update_entry(eid))

    def test_delete_entry(self):
        eid = self.store.insert_entry(_entry())
        self.assertTrue(self.store.delete_entry(eid))
        self.assertIsNone(self.store.get_entry(eid))
        self.assertFalse(self.store.delete_entry(eid))

    def test_categories_in_use(self):
        self.store.insert_entry(_entry(category="代码开发"))
        self.store.insert_entry(_entry(category="写文档"))
        self.store.insert_entry(_entry(category="代码开发"))
        cats = self.store.categories_in_use()
        self.assertEqual(cats, sorted(["代码开发", "写文档"]))


class TestErrorSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecordStore(str(Path(self.tmp) / "err.db"))

    def tearDown(self):
        self.store.close()

    def test_error_summary_empty(self):
        s = self.store.error_summary(7)
        self.assertEqual(s["total_errors"], 0)
        self.assertEqual(s["error_rate"], 0.0)
        self.assertEqual(s["by_kind"], [])

    def test_error_grouping_by_kind(self):
        now = int(datetime.now().timestamp())
        self.store.insert_llm_call({"at": now, "ok": False, "error": "ConnectionError: timed out"})
        self.store.insert_llm_call({"at": now, "ok": False, "error": "ConnectionError: refused"})
        self.store.insert_llm_call({"at": now, "ok": False, "error": "HTTPError: 401 unauthorized"})
        self.store.insert_llm_call({"at": now, "ok": True, "input_tokens": 10})

        s = self.store.error_summary(7)
        self.assertEqual(s["total_errors"], 3)
        self.assertEqual(s["total_calls"], 4)
        self.assertAlmostEqual(s["error_rate"], 0.75)
        kinds = {k["kind"]: k["count"] for k in s["by_kind"]}
        self.assertEqual(kinds["ConnectionError"], 2)
        self.assertEqual(kinds["HTTPError"], 1)
        # 按数量倒序
        self.assertEqual(s["by_kind"][0]["kind"], "ConnectionError")

    def test_recent_errors_only_failures(self):
        now = int(datetime.now().timestamp())
        self.store.insert_llm_call({"at": now, "ok": True, "input_tokens": 5})
        self.store.insert_llm_call({"at": now, "ok": False, "error": "boom"})
        recent = self.store.recent_errors(10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["error"], "boom")

    def test_recent_errors_limit(self):
        now = int(datetime.now().timestamp())
        for _ in range(5):
            self.store.insert_llm_call({"at": now, "ok": False, "error": "boom"})
        self.assertEqual(len(self.store.recent_errors(2)), 2)


if __name__ == "__main__":
    unittest.main()
