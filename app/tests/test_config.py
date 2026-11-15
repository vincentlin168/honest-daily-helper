"""config 模块的测试（SDD 验证，标准库 unittest，零依赖）。

覆盖点：默认值、点路径读写、密钥掩码、公钥出口不回显、加载兜底、掩码忽略。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, mask_secret


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: None)

    def test_defaults_when_no_file(self):
        cfg = Config(str(Path(self.tmp) / "nonexist.json"))
        self.assertEqual(cfg.get("llm", "model"), "deepseek-flash")
        self.assertEqual(cfg.get("capture", "interval"), 120)
        self.assertEqual(cfg.get("report", "trigger"), "both")

    def test_get_nested_and_default(self):
        cfg = Config(str(Path(self.tmp) / "c.json"))
        self.assertEqual(cfg.get("llm", "api_base"), "https://api.deepseek.com/v1")
        self.assertEqual(cfg.get("nonexistent", "key", default="fallback"), "fallback")

    def test_set_and_get(self):
        p = Path(self.tmp) / "c.json"
        cfg = Config(str(p))
        cfg.set("llm", "model", "glm-4.6v")
        self.assertEqual(cfg.get("llm", "model"), "glm-4.6v")

    def test_mask_secret(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("sk-1234567890abcdef"), "sk-1...cdef")
        self.assertEqual(mask_secret("short"), "*****")

    def test_public_dict_masks_secrets(self):
        cfg = Config(str(Path(self.tmp) / "c.json"))
        cfg.set("llm", "api_key", "sk-secret-key-123")
        cfg.set("skill_server", "token", "tok-abc123")
        pub = cfg.to_public_dict()
        self.assertNotIn("sk-secret-key-123", json.dumps(pub))
        self.assertEqual(pub["llm"]["api_key"], "sk-s...-123")
        # 内部 to_dict 保留明文
        self.assertEqual(cfg.to_dict()["llm"]["api_key"], "sk-secret-key-123")

    def test_load_merges_defaults(self):
        p = Path(self.tmp) / "c.json"
        p.write_text(json.dumps({"llm": {"model": "custom-model"}}), encoding="utf-8")
        cfg = Config(str(p))
        self.assertEqual(cfg.get("llm", "model"), "custom-model")
        self.assertEqual(cfg.get("capture", "interval"), 120)

    def test_update_from_dict_ignores_masked_secret(self):
        cfg = Config(str(Path(self.tmp) / "c.json"))
        cfg.set("llm", "api_key", "sk-real-key")
        cfg.update_from_dict({"llm": {"api_key": "sk-r...-key", "model": "new-model"}})
        self.assertEqual(cfg.get("llm", "api_key"), "sk-real-key")
        self.assertEqual(cfg.get("llm", "model"), "new-model")


if __name__ == "__main__":
    unittest.main()
