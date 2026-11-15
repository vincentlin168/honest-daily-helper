"""图片识别路径的 token 预算回归测试（SDD 验证）。

回归背景（真实缺陷）
--------------------
阶段 1 修过 ``test_connection()`` 的「假失败」：推理模型（deepseek-flash）会先输出
``reasoning_content``（思维链），预算太小会被思维链吃光 → ``content`` 为空、
``finish_reason='length'`` → 被误判成「Key 额度问题」。

**但同一个 bug 家族在图片路径上漏了**：``chat_with_image()`` 用 ``max_tokens=1024``，
且 ``_extract_text()`` 没有 ``reasoning_content`` 回退。

实测证据（真实 API）：
    max_tokens=1024 -> reasoning_tokens=1024, content 长度 0, finish_reason=length
    max_tokens=2048 -> reasoning_tokens=736,  content 长度 139, finish_reason=stop

生产影响：650 次识别调用中 23 次失败（3.5%），全部是这一类。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from llm_client import LLMClient


class _Resp:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


def _client():
    return LLMClient(api_base="https://example.invalid/v1", api_key="k", model="m")


class TestExtractText(unittest.TestCase):
    """_extract_text 必须与 _extract_reply 一样有 reasoning_content 回退。"""

    def test_prefers_content(self):
        payload = {"choices": [{"message": {"content": '{"a":1}', "reasoning_content": "思考…"}}]}
        self.assertEqual(LLMClient._extract_text(payload), '{"a":1}')

    def test_falls_back_to_reasoning_content(self):
        """核心回归：content 空但思维链有内容时，不该判失败。"""
        payload = {"choices": [{
            "message": {"content": "", "reasoning_content": '{"activity":"x"}'},
            "finish_reason": "length",
        }]}
        self.assertEqual(LLMClient._extract_text(payload), '{"activity":"x"}')

    def test_handles_list_content(self):
        payload = {"choices": [{"message": {"content": [{"text": "识别结果"}]}}]}
        self.assertEqual(LLMClient._extract_text(payload), "识别结果")

    def test_both_empty_raises(self):
        payload = {"choices": [{"message": {"content": "", "reasoning_content": ""},
                                "finish_reason": "stop"}]}
        with self.assertRaises(RuntimeError) as ctx:
            LLMClient._extract_text(payload)
        self.assertIn("空内容", str(ctx.exception))

    def test_truncated_gives_actionable_message(self):
        """finish_reason='length' 时应提示调大 max_tokens，而不是笼统报错。"""
        payload = {"choices": [{"message": {"content": "", "reasoning_content": ""},
                                "finish_reason": "length"}]}
        with self.assertRaises(RuntimeError) as ctx:
            LLMClient._extract_text(payload)
        self.assertIn("max_tokens", str(ctx.exception))


class TestChatWithImageTokenBudget(unittest.TestCase):
    def _run(self, payload):
        client = _client()
        captured = {}

        def fake_post(*args, **kwargs):
            captured.update(kwargs)
            return _Resp(200, payload)

        with patch.object(httpx.Client, "post", fake_post):
            result = client.chat_with_image("ZmFrZQ==")
        return result, captured

    def test_uses_generous_max_tokens(self):
        """图片路径的 token 预算必须够思维链用。

        实测：1024 会被思维链吃光（reasoning_tokens=1024, content 空），
        2048 稳定（reasoning_tokens=736, finish_reason='stop'）。
        """
        payload = {"choices": [{"message": {"content": '{"activity":"x"}'}}]}
        _, captured = self._run(payload)
        json_body = captured.get("json", {})
        self.assertGreaterEqual(
            json_body.get("max_tokens", 0), 2048,
            "max_tokens 太小，推理模型会返回空 content（实测 1024 会失败）")

    def test_reasoning_only_response_succeeds(self):
        """思维链吃光预算、content 为空时，图片识别仍应成功。"""
        payload = {"choices": [{
            "message": {
                "content": "",
                "reasoning_content": '{"activity":"开发","content":"写代码","category":"代码开发"}',
            },
            "finish_reason": "length",
        }]}
        (text, usage), _ = self._run(payload)
        self.assertIn("开发", text)

    def test_returns_usage(self):
        payload = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 934, "completion_tokens": 150,
                      "total_tokens": 1084},
        }
        (_, usage), _ = self._run(payload)
        self.assertEqual(usage.get("prompt_tokens"), 934)
        self.assertEqual(usage.get("completion_tokens"), 150)
        self.assertEqual(usage.get("total_tokens"), 1084)


if __name__ == "__main__":
    unittest.main()
