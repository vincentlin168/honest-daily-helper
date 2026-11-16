"""llm_client 连通性测试的回归测试（SDD 验证）。

覆盖点：推理模型思维链吃光 token 预算导致的「假失败」。

回归背景
--------
``test_connection()`` 原本用 ``max_tokens=20``。deepseek-flash 这类**推理模型**
会先输出 ``reasoning_content``（思维链），20 token 的预算被思维链吃光后
``content`` 为空、``finish_reason='length'`` —— 被误判为「连接失败 / Key 额度问题」。

现象：同一份可用 Key，测试连接**间歇性**成功、间歇性失败（取决于模型是否
在短回复里省略思维链）。

本测试用 mock 复现两种响应形态，确保两种都能判为「连接成功」。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from llm_client import LLMClient


def _client():
    return LLMClient(api_base="https://example.invalid/v1", api_key="k", model="m")


class _Resp:
    """最小化的 httpx.Response 替身。"""

    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


class TestExtractReply(unittest.TestCase):
    def test_prefers_content(self):
        payload = {"choices": [{"message": {"content": "连接成功", "reasoning_content": "思考…"}}]}
        self.assertEqual(LLMClient._extract_reply(payload), "连接成功")

    def test_falls_back_to_reasoning_content(self):
        """content 为空时回退思维链 —— 推理模型的常见形态。"""
        payload = {"choices": [{"message": {"content": "", "reasoning_content": "连接成功"}}]}
        self.assertEqual(LLMClient._extract_reply(payload), "连接成功")

    def test_handles_list_content(self):
        payload = {"choices": [{"message": {"content": [{"text": "连接"}]}}]}
        self.assertEqual(LLMClient._extract_reply(payload), "连接")

    def test_returns_empty_on_bad_shape(self):
        self.assertEqual(LLMClient._extract_reply({}), "")
        self.assertEqual(LLMClient._extract_reply({"choices": []}), "")


class TestTestConnection(unittest.TestCase):
    def _run(self, payload):
        client = _client()
        captured = {}

        def fake_post(*args, **kwargs):
            captured.update(kwargs)
            return _Resp(200, payload)

        with patch.object(httpx.Client, "post", fake_post):
            result = client.test_connection()
        return result, captured

    def test_success_with_reasoning_only_response(self):
        """核心回归：思维链吃光预算导致的假失败。"""
        payload = {"choices": [{"message": {
            "content": "",
            "reasoning_content": "The user asks to reply 连接成功",
            "finish_reason": "length",
        }}]}
        result, captured = self._run(payload)
        self.assertTrue(result["ok"], f"推理模型被误判为失败：{result}")
        self.assertTrue(result["reply"])

    def test_uses_generous_max_tokens(self):
        """token 预算必须给足，否则思维链会把 content 吃光。"""
        payload = {"choices": [{"message": {"content": "连接成功"}}]}
        _, captured = self._run(payload)
        json_body = captured.get("json", {})
        self.assertGreaterEqual(json_body.get("max_tokens", 0), 512,
                                "max_tokens 太小，推理模型会返回空 content")

    def test_http_error_reports_status(self):
        client = _client()
        with patch.object(httpx.Client, "post", lambda *a, **k: _Resp(401, {"error": "bad key"})):
            result = client.test_connection()
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])


if __name__ == "__main__":
    unittest.main()
