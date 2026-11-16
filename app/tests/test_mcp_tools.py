"""MCP 工具层测试（SDD 验证）。

覆盖点：
1. **工具注册表完整性** —— 每个工具有 name / description / inputSchema
2. **只读保证** —— ReadOnlyStore 上不存在任何写方法
3. **参数校验** —— 非法日期 / 非法 limit 返回结构化错误而非抛异常
4. **截断诚实性** —— truncated / total 语义正确
5. **无副作用** —— export_data 不写文件；日报不含 AI 分析

设计要点
--------
工具层是纯逻辑，**不依赖 stdio** —— 协议层另测。
所以这些测试可以直接调 ``call_tool()``，无需起进程。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archive import ReportArchive
from mcp_tools import (ReadOnlyStore, TOOL_MAP, ToolContext, ToolError,
                       call_tool, list_tools, tool_names)
from store import RecordStore


def _ts_at(hour: int, minute: int = 0, day_offset: int = 0) -> int:
    """构造某天某时刻的时间戳。"""
    base = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int((base - timedelta(days=day_offset)).timestamp())


class ToolTestBase(unittest.TestCase):
    """共用一套含真实数据的只读上下文。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = str(Path(cls.tmp) / "mcp.db")
        cls.store = RecordStore(cls.db_path)
        cls.archive_dir = str(Path(cls.tmp) / "reports")
        cls.archive = ReportArchive(cls.archive_dir)

        # 造今天的记录：两个分类、三段时间
        today = datetime.now().strftime("%Y-%m-%d")
        cls.today = today
        cls.store.insert_entry({
            "started_at": _ts_at(9, 0), "ended_at": _ts_at(10, 30),
            "activity": "写代码", "content": "实现 MCP 工具层",
            "category": "开发", "model": "deepseek-flash",
        })
        cls.store.insert_entry({
            "started_at": _ts_at(11, 0), "ended_at": _ts_at(12, 0),
            "activity": "写文档", "content": "编写 Spec 文档",
            "category": "写作", "model": "deepseek-flash",
        })
        cls.store.insert_entry({
            "started_at": _ts_at(14, 0), "ended_at": _ts_at(15, 30),
            "activity": "开会", "content": "同步进度",
            "category": "会议", "model": "deepseek-flash",
        })

        # 造 token 用量与失败调用
        cls.store.insert_llm_call({
            "at": _ts_at(9, 5), "feature": "recognition", "model": "deepseek-flash",
            "ok": 1, "input_tokens": 1000, "output_tokens": 200,
            "estimated": 0.01, "latency_ms": 1200, "images": 1,
        })
        cls.store.insert_llm_call({
            "at": _ts_at(9, 10), "feature": "recognition", "model": "deepseek-flash",
            "ok": 0, "http_status": 401, "error": "[401] Unauthorized",
            "input_tokens": 50, "output_tokens": 0,
            "estimated": 0.0, "latency_ms": 300, "images": 1,
        })

        cls.ctx = ToolContext(cls.store, cls.archive, db_path=cls.db_path,
                              price_input=1.0, price_output=2.0)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def call(self, name, **args):
        return call_tool(self.ctx, name, args)


class TestToolRegistry(unittest.TestCase):
    """工具注册表结构。"""

    def test_all_tools_have_required_fields(self):
        for tool in list_tools():
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)
            self.assertTrue(tool["description"].strip(),
                            f"工具 {tool['name']} 的描述为空")
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tool_count(self):
        self.assertEqual(len(tool_names()), 18)

    def test_tool_names_unique(self):
        names = tool_names()
        self.assertEqual(len(names), len(set(names)))

    def test_required_params_declared_in_schema(self):
        """必填参数必须在 schema 的 required 里列出。"""
        for name in ("get_entry", "get_entries_range", "get_report_body"):
            schema = TOOL_MAP[name].schema
            for param in schema.get("required", []):
                self.assertIn(param, schema["properties"],
                              f"{name} 的必填参数 {param} 未在 properties 中声明")

    def test_descriptions_mention_limits(self):
        """描述要讲清限制 —— 这是 Agent 正确用工具的前提。"""
        desc = TOOL_MAP["get_entries"].description
        self.assertIn("truncated", desc)
        self.assertIn("200", desc)

    def test_unknown_tool_raises_keyerror(self):
        with self.assertRaises(KeyError):
            call_tool(None, "no_such_tool", {})


class TestReadOnlyGuarantee(unittest.TestCase):
    """只读必须是结构性的，不是靠约定。"""

    def test_no_write_methods_on_readonly_store(self):
        ro = ReadOnlyStore.__new__(ReadOnlyStore)
        forbidden = [
            "insert_entry", "extend_entry", "update_category", "update_entry",
            "delete_entry", "insert_llm_call", "insert_skip_event", "close",
            "_conn", "_lock",
        ]
        for name in forbidden:
            self.assertFalse(hasattr(ro, name),
                             f"ReadOnlyStore 不应暴露 {name} —— 只读是结构性的")

    def test_readonly_store_has_read_methods(self):
        ro = ReadOnlyStore.__new__(ReadOnlyStore)
        for name in ("entries_by_day", "get_entry", "hourly_histogram",
                     "recent_errors", "token_usage_by_day"):
            self.assertTrue(hasattr(ro, name), f"缺少只读方法 {name}")

    def test_tool_context_uses_readonly_store(self):
        tmp = tempfile.mkdtemp()
        store = RecordStore(str(Path(tmp) / "ro.db"))
        try:
            ctx = ToolContext(store)
            self.assertIsInstance(ctx.store, ReadOnlyStore)
            self.assertFalse(hasattr(ctx.store, "insert_entry"))
        finally:
            store.close()


class TestParameterValidation(ToolTestBase):
    """参数校验 —— 错误要可读，不能抛裸异常。"""

    def test_invalid_date_raises_tool_error(self):
        """非法日期必须被拒绝，且错误可读。

        注意空串是**例外**：空串被视作「没传日期」，回退到今天 ——
        Agent 传空串时直接回今天比报错更合理。
        """
        for bad in ("2026-13-99", "not-a-date", "20261116", "2026-11-16T00:00"):
            with self.assertRaises(ToolError, msg=f"{bad!r} 应被拒绝"):
                self.call("get_entries", date=bad)

    def test_empty_date_falls_back_to_today(self):
        """空串日期回退到今天，不报错。"""
        res = self.call("get_entries", date="")
        self.assertTrue(res["ok"])
        self.assertEqual(res["date"], datetime.now().strftime("%Y-%m-%d"))

    def test_compact_date_format_rejected(self):
        """紧凑格式（20261116）必须拒绝 —— 它不是「某一天」的规范写法。"""
        with self.assertRaises(ToolError):
            self.call("get_entries", date="20261116")

    def test_invalid_date_error_message_is_helpful(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("get_entries", date="bad-date")
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_missing_date_param_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.call("get_entries_range", start_date="2026-11-01")

    def test_start_after_end_raises_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("get_entries_range", start_date="2026-11-10",
                      end_date="2026-11-01")
        self.assertIn("不能晚于", str(ctx.exception))

    def test_invalid_id_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.call("get_entry", id="abc")

    def test_invalid_limit_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.call("get_entries", date=self.today, limit="many")

    def test_invalid_export_format_raises_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("export_data", format="xml")
        self.assertIn("json", str(ctx.exception))

    def test_arguments_must_be_object(self):
        with self.assertRaises(ToolError):
            call_tool(self.ctx, "get_entries", ["not", "a", "dict"])


class TestToolBehavior(ToolTestBase):
    """各工具的实际行为。"""

    def test_get_entries_returns_today_records(self):
        res = self.call("get_entries", date=self.today)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["total"], 3)
        self.assertEqual(len(res["data"]["items"]), 3)

    def test_get_entries_on_empty_day(self):
        res = self.call("get_entries", date="2020-01-01")
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["total"], 0)
        self.assertEqual(res["data"]["items"], [])

    def test_get_entry_found_and_not_found(self):
        res = self.call("get_entry", id=1)
        self.assertTrue(res["found"])
        self.assertEqual(res["data"]["id"], 1)

        missing = self.call("get_entry", id=99999)
        self.assertFalse(missing["found"])
        self.assertIsNone(missing["data"])

    def test_get_overview_has_cost(self):
        res = self.call("get_overview", date=self.today)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["total_segments"], 3)
        self.assertTrue(res["data"]["cost"]["has_price"])

    def test_get_daily_stats(self):
        res = self.call("get_daily_stats", date=self.today)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["total_segments"], 3)

    def test_get_top_categories_sorted_by_duration(self):
        res = self.call("get_top_categories", date=self.today, top=2)
        cats = res["data"]["top_categories"]
        self.assertEqual(len(cats), 2)
        self.assertGreaterEqual(cats[0]["duration"], cats[1]["duration"])

    def test_get_heatmap(self):
        res = self.call("get_heatmap", days=3)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["data"]["heatmap"]), 1)

    def test_get_interval_effectiveness(self):
        res = self.call("get_interval_effectiveness", days=7)
        self.assertTrue(res["ok"])
        self.assertIn("data", res)

    def test_get_token_usage_by_day(self):
        res = self.call("get_token_usage", date=self.today)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["calls"], 2)
        self.assertEqual(res["data"]["failures"], 1)

    def test_get_cost_uses_configured_price(self):
        """花费要算上**失败调用**的消耗 —— 失败的请求同样花了钱。

        诚实性要求：不能只统计成功调用，那样算出来的费用会偏低。
        """
        res = self.call("get_cost", date=self.today)
        self.assertTrue(res["data"]["has_price"])
        usage = res["data"]
        # 成功调用 1000 输入 / 200 输出；失败调用 50 输入 / 0 输出
        self.assertEqual(usage["input_tokens"], 1050)
        self.assertEqual(usage["output_tokens"], 200)
        expected = (1050 * 1.0 + 200 * 2.0) / 1_000_000
        self.assertAlmostEqual(usage["cost"], expected, places=9)

    def test_get_cost_without_price_does_not_fabricate(self):
        ctx = ToolContext(self.store, self.archive)
        res = call_tool(ctx, "get_cost", {"date": self.today})
        self.assertFalse(res["data"]["has_price"])
        self.assertIsNone(res["data"]["cost"])

    def test_get_errors(self):
        res = self.call("get_errors")
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["data"]["count"], 1)

    def test_get_error_summary(self):
        res = self.call("get_error_summary", days=7)
        self.assertTrue(res["ok"])

    def test_get_status(self):
        res = self.call("get_status")
        self.assertTrue(res["ok"])
        self.assertTrue(res["data"]["read_only"])
        self.assertEqual(res["data"]["today"], self.today)


class TestTruncationHonesty(ToolTestBase):
    """截断必须诚实告知 —— 这是本项目的核心原则。"""

    def test_limit_truncates_and_marks(self):
        res = self.call("get_entries", date=self.today, limit=2)
        data = res["data"]
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["total"], 3)
        self.assertTrue(data["truncated"])

    def test_no_truncation_when_all_returned(self):
        res = self.call("get_entries", date=self.today, limit=100)
        self.assertFalse(res["data"]["truncated"])

    def test_offset_paginates(self):
        page1 = self.call("get_entries", date=self.today, limit=2, offset=0)
        page2 = self.call("get_entries", date=self.today, limit=2, offset=2)
        ids1 = {e["id"] for e in page1["data"]["items"]}
        ids2 = {e["id"] for e in page2["data"]["items"]}
        self.assertEqual(len(ids2), 1)
        self.assertFalse(ids1 & ids2)
        self.assertFalse(page2["data"]["truncated"])

    def test_limit_clamped_to_max(self):
        res = self.call("get_entries", date=self.today, limit=999999)
        self.assertTrue(res["ok"])


class TestNoSideEffects(ToolTestBase):
    """无副作用：MCP 工具不改任何状态。"""

    def test_export_data_returns_content_without_writing_file(self):
        before = set(p for p in Path(self.archive_dir).glob("*")) if Path(self.archive_dir).exists() else set()
        res = self.call("export_data", format="json", date=self.today)
        self.assertTrue(res["ok"])
        self.assertIn("content", res["data"])
        self.assertEqual(res["data"]["entry_count"], 3)

        after = set(p for p in Path(self.archive_dir).glob("*")) if Path(self.archive_dir).exists() else set()
        self.assertEqual(before, after, "export_data 不应写任何文件")

    def test_export_formats(self):
        for fmt in ("json", "csv", "md"):
            res = self.call("export_data", format=fmt, date=self.today)
            self.assertTrue(res["ok"], f"{fmt} 导出失败")
            self.assertTrue(res["data"]["content"].strip(), f"{fmt} 导出内容为空")

    def test_daily_report_has_no_ai_analysis(self):
        """日报必须是模板版 —— AI 分析要调 LLM，有副作用。"""
        res = self.call("get_daily_report", date=self.today)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["data"]["ai_analysis"])
        # 模板版的标题
        self.assertIn("## 概览", res["data"]["report_markdown"])
        self.assertIn("## 活动时间线", res["data"]["report_markdown"])
        # AI 分析段落不应出现
        self.assertNotIn("## AI 分析", res["data"]["report_markdown"])

    def test_report_body_reads_archive(self):
        self.archive.save(self.today, "# 留档日报\n\n### 观察\n测试\n")
        try:
            res = self.call("get_report_body", date=self.today)
            self.assertTrue(res["found"])
            self.assertIn("### 观察", res["data"]["content"])
        finally:
            self.archive.delete(self.today)

    def test_report_body_missing_returns_found_false(self):
        res = self.call("get_report_body", date="2020-01-01")
        self.assertFalse(res["found"])

    def test_tools_do_not_mutate_store(self):
        """跑一遍所有工具后，记录数不变。"""
        before = len(self.store.entries_by_day(self.today))
        for name in tool_names():
            args = {}
            if name in ("get_entries", "get_overview", "get_daily_stats",
                        "get_top_categories", "get_daily_report", "get_cost"):
                args["date"] = self.today
            elif name == "get_entries_range":
                args = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
            elif name == "get_entry":
                args = {"id": 1}
            elif name == "get_report_body":
                args = {"date": self.today}
            try:
                call_tool(self.ctx, name, args)
            except ToolError:
                pass  # 参数类错误是预期的
        after = len(self.store.entries_by_day(self.today))
        self.assertEqual(before, after, "MCP 工具不应改变数据库内容")


if __name__ == "__main__":
    unittest.main()
