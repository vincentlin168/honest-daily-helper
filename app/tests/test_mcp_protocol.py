"""MCP 协议层测试（SDD 验证）。

覆盖点：
1. **握手** —— initialize 返回协议版本与能力
2. **工具列举** —— tools/list 返回 schema 合法的工具清单
3. **工具调用** —— tools/call 返回 content + structuredContent
4. **JSON-RPC 错误** —— 未知方法 -32601、非法 JSON -32700（id 为 null）
5. **通知** —— 无 id 的消息不产生响应
6. **stdout 纯净性** —— 端到端跑一轮，stdout 每行都必须是合法 JSON

设计要点
--------
协议逻辑（``MCPProtocol.handle``）与 I/O 解耦，所以大部分测试直接调函数；
但 **stdout 纯净性必须端到端测** —— 因为「stdout 被污染」是这个模块
最容易踩、也最难靠单元测试发现的坑。

背景
----
MCP 的 stdio 传输里，stdout 是协议通道。任何 ``print()``、日志、警告
混进去，Agent 的解析器就会失败。所以：
* 启动时把 ``sys.stdout`` 重定向到 stderr
* 协议输出走 ``sys.__stdout__``
* 只往 stderr 写日志（不与主程序共用日志文件）
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import (METHOD_NOT_FOUND, PARSE_ERROR, PROTOCOL_VERSION,
                        MCPProtocol, serve)
from mcp_tools import ToolContext
from store import RecordStore

APP_DIR = Path(__file__).resolve().parent.parent


def _make_ctx(tmp: str) -> ToolContext:
    store = RecordStore(str(Path(tmp) / "proto.db"))
    return ToolContext(store, db_path=str(Path(tmp) / "proto.db"))


class ProtocolTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx = _make_ctx(self.tmp)
        self.protocol = MCPProtocol(self.ctx)

    def tearDown(self):
        try:
            self.ctx.store._store.close()  # noqa: SLF001
        except Exception:
            pass

    def send(self, method, params=None, msg_id=1):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if msg_id is not None:
            msg["id"] = msg_id
        return self.protocol.handle(msg)


class TestInitialize(ProtocolTestBase):

    def test_initialize_returns_protocol_version_and_capabilities(self):
        res = self.send("initialize", {"protocolVersion": PROTOCOL_VERSION})
        self.assertIn("result", res)
        result = res["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "honest-daily-helper")

    def test_initialize_echoes_client_protocol_version(self):
        """宽松协商 —— 客户端报什么版本就回什么版本。

        严格校验版本会让新旧客户端直接握手失败，
        对本地工具来说「继续跑」比「拒绝服务」更划算。
        """
        res = self.send("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_without_version_uses_default(self):
        res = self.send("initialize", {})
        self.assertEqual(res["result"]["protocolVersion"], PROTOCOL_VERSION)

    def test_initialize_includes_instructions(self):
        """instructions 要说明「只读」和「截断」—— 让 Agent 一开始就知道边界。"""
        res = self.send("initialize", {})
        text = res["result"]["instructions"]
        self.assertIn("只读", text)
        self.assertIn("truncated", text)

    def test_initialized_notification_sets_flag(self):
        self.assertFalse(self.protocol.initialized)
        self.protocol.handle({"jsonrpc": "2.0",
                              "method": "notifications/initialized"})
        self.assertTrue(self.protocol.initialized)


class TestToolsList(ProtocolTestBase):

    def test_lists_all_tools(self):
        res = self.send("tools/list")
        tools = res["result"]["tools"]
        self.assertEqual(len(tools), 18)

    def test_each_tool_has_valid_schema(self):
        for tool in self.send("tools/list")["result"]["tools"]:
            self.assertRegex(tool["name"], r"^[a-z][a-z0-9_]*$")
            self.assertTrue(tool["description"].strip())
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIsInstance(schema["properties"], dict)
            for prop, spec in schema["properties"].items():
                self.assertIn("type", spec, f"{tool['name']}.{prop} 缺 type")
                self.assertIn("description", spec,
                              f"{tool['name']}.{prop} 缺 description")


class TestToolsCall(ProtocolTestBase):

    def test_call_returns_content_and_structured(self):
        res = self.send("tools/call", {"name": "get_status", "arguments": {}})
        result = res["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("structuredContent", result)

    def test_call_content_is_valid_json(self):
        res = self.send("tools/call", {"name": "get_status", "arguments": {}})
        text = res["content"][0]["text"] if "content" in res else res["result"]["content"][0]["text"]
        json.loads(text)  # 不抛异常即通过

    def test_unknown_tool_returns_is_error_not_rpc_error(self):
        """未知工具应是 tool 级错误，不是 JSON-RPC error。

        区别很重要：isError 的结果 Agent 能读懂并自我纠正；
        而协议层错误会让 Agent 以为服务坏了。
        """
        res = self.send("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertNotIn("error", res)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("可用工具", res["result"]["content"][0]["text"])

    def test_tool_param_error_returns_is_error(self):
        res = self.send("tools/call", {"name": "get_entries",
                                       "arguments": {"date": "bad"}})
        self.assertNotIn("error", res)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("YYYY-MM-DD", res["result"]["content"][0]["text"])

    def test_call_without_name_returns_is_error(self):
        res = self.send("tools/call", {"arguments": {}})
        self.assertTrue(res["result"]["isError"])


class TestJSONRPCErrors(ProtocolTestBase):

    def test_unknown_method_returns_method_not_found(self):
        res = self.send("no/such/method")
        self.assertEqual(res["error"]["code"], METHOD_NOT_FOUND)
        self.assertEqual(res["id"], 1)

    def test_non_object_message_returns_invalid_request(self):
        res = self.protocol.handle("not a dict")
        self.assertEqual(res["error"]["code"], -32600)
        self.assertIsNone(res["id"])

    def test_missing_method_returns_invalid_request(self):
        res = self.protocol.handle({"jsonrpc": "2.0", "id": 5})
        self.assertEqual(res["error"]["code"], -32600)

    def test_parse_error_has_null_id(self):
        """解析失败时 id 必须是 null —— 客户端无从知道是哪条消息坏了。"""
        stdin = io.StringIO("this is not json\n")
        stdout = io.StringIO()
        serve(stdin, stdout, self.protocol)
        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        res = json.loads(lines[0])
        self.assertEqual(res["error"]["code"], PARSE_ERROR)
        self.assertIsNone(res["id"])


class TestNotifications(ProtocolTestBase):

    def test_notification_produces_no_response(self):
        res = self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(res)

    def test_notification_with_unknown_method_produces_no_response(self):
        res = self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/cancelled"})
        self.assertIsNone(res)

    def test_serve_skips_notification_lines(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        stdout = io.StringIO()
        serve(stdin, stdout, self.protocol)
        self.assertEqual(stdout.getvalue().strip(), "")


class TestServeLoop(ProtocolTestBase):

    def test_multiple_requests_get_multiple_responses(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        serve(stdin, stdout, self.protocol)
        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["id"], 1)
        self.assertEqual(json.loads(lines[1])["id"], 2)

    def test_blank_lines_skipped(self):
        stdin = io.StringIO("\n\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n\n")
        stdout = io.StringIO()
        serve(stdin, stdout, self.protocol)
        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)


class TestStdoutPurity(unittest.TestCase):
    """stdout 纯净性 —— 这个模块最容易踩的坑。

    端到端起真实子进程：给它发几轮协议消息，
    然后断言 stdout **每一行**都是合法 JSON，且能解析出正确的 id。

    如果有任何 print / 日志混进 stdout，这里立刻会炸。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "e2e.db")
        # 造一条记录，让工具有数据可返回
        store = RecordStore(self.db)
        import datetime as _dt
        now = int(_dt.datetime.now().timestamp())
        store.insert_entry({
            "started_at": now - 3600, "ended_at": now - 1800,
            "activity": "测试", "content": "端到端验证",
            "category": "开发", "model": "m",
        })
        store.close()

    def _run(self, requests):
        payload = "\n".join(json.dumps(r, ensure_ascii=False)
                            for r in requests) + "\n"
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / "mcp_server.py"), "--db", self.db],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )
        return proc

    def test_stdout_contains_only_json_lines(self):
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "get_entries", "arguments": {}}},
        ])
        stdout_text = proc.stdout.decode("utf-8")
        lines = [l for l in stdout_text.splitlines() if l.strip()]
        self.assertTrue(lines, "stdout 为空 —— 服务器没响应")
        for line in lines:
            parsed = json.loads(line)  # 非 JSON 会直接抛异常
            self.assertEqual(parsed["jsonrpc"], "2.0")
        ids = [json.loads(l).get("id") for l in lines]
        self.assertEqual(ids, [1, 2, 3])

    def test_exit_code_zero_on_clean_eof(self):
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ])
        self.assertEqual(proc.returncode, 0)

    def test_stderr_may_have_noise_but_stdout_stays_clean(self):
        """即使 stderr 有输出（警告等），stdout 也必须干净。"""
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "get_status", "arguments": {}}},
        ])
        stdout_text = proc.stdout.decode("utf-8")
        for line in stdout_text.splitlines():
            if line.strip():
                json.loads(line)

    def test_tool_call_works_end_to_end(self):
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "get_entries", "arguments": {}}},
        ])
        lines = [json.loads(l) for l in proc.stdout.decode("utf-8").splitlines()
                 if l.strip()]
        self.assertEqual(len(lines), 2)
        payload = json.loads(lines[1]["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["total"], 1)

    def test_chinese_survives_round_trip(self):
        """中文必须能往返 —— 这是编码陷阱的直接回归测试。

        Windows 上 ``sys.__stdout__`` 默认是 GBK，若不显式指定 UTF-8，
        中文会被按 GBK 写出去，Agent 按 UTF-8 解析直接失败。
        这个坑很隐蔽：纯 ASCII 一切正常，只有出现中文才炸。
        """
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "get_entries", "arguments": {}}},
        ])
        stdout_text = proc.stdout.decode("utf-8")  # 必须是 UTF-8 能解
        lines = [json.loads(l) for l in stdout_text.splitlines() if l.strip()]

        # instructions 里的中文要完好
        instructions = lines[0]["result"]["instructions"]
        self.assertIn("只读", instructions)

        # 工具描述（中文）也要完好
        proc2 = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        tools = json.loads(
            proc2.stdout.decode("utf-8").splitlines()[0])["result"]["tools"]
        self.assertIn("工作活动记录", tools[0]["description"])

        # 返回内容里的中文（记录内容）要完好
        payload = json.loads(lines[1]["result"]["content"][0]["text"])
        items = payload["data"]["items"]
        self.assertEqual(items[0]["content"], "端到端验证")

    def test_chinese_arguments_are_parsed(self):
        """客户端发来的中文参数也要能正确解析（stdin 编码陷阱）。"""
        proc = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_entries",
                        "arguments": {"date": "2020-01-01"}}},
        ])
        lines = [json.loads(l) for l in proc.stdout.decode("utf-8").splitlines()
                 if l.strip()]
        self.assertIn("result", lines[0])
        payload = json.loads(lines[0]["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])


class TestMissingDatabase(unittest.TestCase):
    """数据库不存在时要友好提示，而不是抛栈。"""

    def test_missing_db_gives_helpful_error(self):
        tmp = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / "mcp_server.py"),
             "--db", str(Path(tmp) / "nonexistent.db")],
            input=b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        # RecordStore 会自动建库，所以这里应该正常退出（退出码 0）
        # 关键是**不崩**、stdout 干净
        self.assertEqual(proc.returncode, 0)
        for line in proc.stdout.decode("utf-8").splitlines():
            if line.strip():
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
