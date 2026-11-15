"""llm_client 模块的测试（SDD 验证，标准库 unittest）。

覆盖点：消息构造、响应解析、usage 提取、识别结果解析（JSON/markdown/纯文本容错）。
纯逻辑测试，不真实调用 API。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_client import LLMClient, build_system_prompt, DEFAULT_CATEGORIES


class TestBuildMessages(unittest.TestCase):
    def setUp(self):
        self.c = LLMClient("https://api.deepseek.com/v1", "sk-test", "deepseek-flash")

    def test_image_inlined_base64(self):
        msgs = self.c._build_messages("aGVsbG8=")
        # 结构：system + user
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], build_system_prompt())
        # user 消息含 text + image_url
        user_content = msgs[1]["content"]
        self.assertEqual(user_content[0]["type"], "text")
        self.assertEqual(user_content[1]["type"], "image_url")
        self.assertEqual(
            user_content[1]["image_url"]["url"],
            "data:image/jpeg;base64,aGVsbG8=",
        )

    def test_empty_image_raises(self):
        with self.assertRaises(ValueError):
            self.c._build_messages("")  # 空 base64 不应被接受


class TestExtractText(unittest.TestCase):
    def test_normal_string(self):
        payload = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(LLMClient._extract_text(payload), "hello")

    def test_list_content(self):
        payload = {"choices": [{"message": {"content": [{"type": "text", "text": "abc"}]}}]}
        self.assertEqual(LLMClient._extract_text(payload), "abc")

    def test_empty_content_raises(self):
        payload = {"choices": [{"message": {"content": ""}}]}
        with self.assertRaises(RuntimeError):
            LLMClient._extract_text(payload)

    def test_malformed_raises(self):
        with self.assertRaises(RuntimeError):
            LLMClient._extract_text({"choices": []})


class TestExtractUsage(unittest.TestCase):
    def test_usage(self):
        payload = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        u = LLMClient._extract_usage(payload)
        self.assertEqual(u["prompt_tokens"], 100)
        self.assertEqual(u["completion_tokens"], 50)
        self.assertEqual(u["total_tokens"], 150)

    def test_no_usage(self):
        self.assertEqual(LLMClient._extract_usage({}), {})


class TestBuildSystemPrompt(unittest.TestCase):
    def test_custom_categories_injected(self):
        p = build_system_prompt(["我的分类A", "我的分类B"])
        self.assertIn("我的分类A", p)
        self.assertIn("我的分类B", p)

    def test_default_categories(self):
        p = build_system_prompt()
        self.assertIn("代码开发", p)
        self.assertIn("产品设计", p)


class TestParseRecognition(unittest.TestCase):
    def test_plain_json(self):
        r = LLMClient.parse_recognition('{"activity": "开发日报工具", "content": "写代码", "category": "代码开发"}')
        self.assertEqual(r["activity"], "开发日报工具")
        self.assertEqual(r["content"], "写代码")
        self.assertEqual(r["category"], "代码开发")

    def test_missing_activity_falls_back_to_content_prefix(self):
        r = LLMClient.parse_recognition('{"content": "写代码", "category": "代码开发"}')
        self.assertEqual(r["content"], "写代码")
        self.assertEqual(r["category"], "代码开发")
        self.assertEqual(r["activity"], "写代码")  # fallback = content 前 15 字

    def test_markdown_fenced_json(self):
        text = '```json\n{"activity": "设计", "content": "做设计", "category": "产品设计"}\n```'
        r = LLMClient.parse_recognition(text)
        self.assertEqual(r["content"], "做设计")
        self.assertEqual(r["category"], "产品设计")

    def test_json_with_extra_text(self):
        text = '好的，结果是 {"activity": "开会", "content": "开会", "category": "项目管理"}'
        r = LLMClient.parse_recognition(text)
        self.assertEqual(r["content"], "开会")
        self.assertEqual(r["category"], "项目管理")

    def test_plain_text_fallback(self):
        text = "正在写日报"
        r = LLMClient.parse_recognition(text)
        self.assertEqual(r["content"], "正在写日报")
        self.assertEqual(r["category"], "其他")
        self.assertEqual(r["activity"], "正在写日报")  # fallback

    def test_missing_fields_default(self):
        r = LLMClient.parse_recognition('{"content": "只有内容"}')
        self.assertEqual(r["content"], "只有内容")
        self.assertEqual(r["category"], "其他")


if __name__ == "__main__":
    unittest.main()
