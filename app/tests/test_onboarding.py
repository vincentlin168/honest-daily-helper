"""首次运行引导向导测试（SDD 验证）。

设计要点
--------
引导的价值在于「不让新用户面对一堆空白输入框」，
以及「每一步都能立刻发现问题，而不是走到最后才知道 Key 填错了」。

覆盖点
------
1. **是否需要引导**：判据是「API Key 实质配好」，而非「标记字段存在」。
2. **逐步校验**：每一步都能独立给出可操作的问题描述。
3. **不写半成品配置**：任一步校验不过，就不写配置。
4. **安全**：api_key 永不通过公共接口回显。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from onboarding import Onboarding


def _config(tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    return Config(str(Path(tmp) / "config.json"))


class TestNeedOnboarding(unittest.TestCase):
    """何时该引导用户。"""

    def setUp(self):
        self.cfg = _config()
        self.ob = Onboarding(self.cfg)

    def test_new_user_needs_onboarding(self):
        self.assertFalse(self.ob.is_completed(), "Key 为空时应需要引导")

    def test_configured_key_means_completed(self):
        self.cfg.set("llm", "api_key", "sk-abcdefghijklmnop")
        self.assertTrue(self.ob.is_completed(), "Key 配好后不该再打扰用户")

    def test_placeholder_key_still_needs_onboarding(self):
        self.cfg.set("llm", "api_key", "your-api-key-here")
        self.assertFalse(self.ob.is_completed(), "占位符不算配好")

    def test_example_key_still_needs_onboarding(self):
        for bad in ("your-key", "placeholder-key", "TODO", "changeme"):
            self.cfg.set("llm", "api_key", bad)
            self.assertFalse(self.ob.is_completed(), f"{bad} 不算配好")

    def test_whitespace_only_key(self):
        self.cfg.set("llm", "api_key", "   ")
        self.assertFalse(self.ob.is_completed())

    def test_mark_completed_persists(self):
        """走完引导后：Key 已配置 + 标记已写入，重新加载仍是完成状态。"""
        self.cfg.set("llm", "api_key", "sk-abcdefghijklmnop")
        self.ob.mark_completed()
        self.assertTrue(self.ob.is_completed())
        self.assertTrue(self.cfg.get("onboarding", "completed", default=False))
        # 重新加载后仍是完成状态
        self.cfg2 = Config(str(self.cfg.config_path))
        self.assertTrue(Onboarding(self.cfg2).is_completed())

    def test_mark_completed_without_key_still_needs_setup(self):
        """标记完成但 Key 没填 -> 仍判定为需要配置。

        这是「诚实」的选择：标记说完成了，但实质条件没满足时，
        重新提示比假装配好了更安全 —— 否则用户会卡在「没反应」的状态。
        """
        self.ob.mark_completed()
        self.assertFalse(self.ob.is_completed())
        self.assertTrue(self.cfg.get("onboarding", "completed", default=False))

    def test_mark_skipped_keeps_needing_setup(self):
        """跳过 ≠ 配好了。下次仍会提示，但不强拦。"""
        self.ob.mark_skipped()
        self.assertFalse(self.ob.is_completed())
        self.assertTrue(self.cfg.get("onboarding", "skipped", default=False))


class TestValidateModel(unittest.TestCase):
    def setUp(self):
        self.ob = Onboarding(_config())

    def test_valid_config_passes(self):
        res = self.ob.validate_model("https://api.deepseek.com/v1", "deepseek-flash", "sk-abc123")
        self.assertTrue(res["ok"])
        self.assertEqual(res["problems"], [])

    def test_empty_base_rejected(self):
        res = self.ob.validate_model("", "model", "sk-key")
        self.assertFalse(res["ok"])
        self.assertTrue(any("API 地址" in p for p in res["problems"]))

    def test_bad_scheme_rejected(self):
        res = self.ob.validate_model("api.deepseek.com/v1", "model", "sk-key")
        self.assertFalse(res["ok"])

    def test_empty_model_rejected(self):
        res = self.ob.validate_model("https://x.com/v1", "", "sk-key")
        self.assertFalse(res["ok"])
        self.assertTrue(any("模型名" in p for p in res["problems"]))

    def test_empty_key_rejected(self):
        res = self.ob.validate_model("https://x.com/v1", "m", "")
        self.assertFalse(res["ok"])
        self.assertTrue(any("Key" in p for p in res["problems"]))


class TestValidateCapture(unittest.TestCase):
    def setUp(self):
        self.ob = Onboarding(_config())

    def test_valid(self):
        self.assertTrue(self.ob.validate_capture("full", 15)["ok"])
        self.assertTrue(self.ob.validate_capture("left_half", 5)["ok"])

    def test_unknown_region_rejected(self):
        res = self.ob.validate_capture("weird", 15)
        self.assertFalse(res["ok"])

    def test_interval_bounds(self):
        self.assertFalse(self.ob.validate_capture("full", 1)["ok"])
        self.assertFalse(self.ob.validate_capture("full", 999)["ok"])

    def test_non_numeric_interval_handled(self):
        res = self.ob.validate_capture("full", "abc")
        self.assertFalse(res["ok"])


class TestValidateCategories(unittest.TestCase):
    def setUp(self):
        self.ob = Onboarding(_config())

    def test_empty_rejected(self):
        res = self.ob.validate_categories([])
        self.assertFalse(res["ok"])

    def test_whitespace_filtered(self):
        res = self.ob.validate_categories([" 代码开发 ", "", "  ", "资料研究"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["categories"], ["代码开发", "资料研究"])

    def test_duplicates_removed_keeping_order(self):
        res = self.ob.validate_categories(["A", "B", "A", "C"])
        self.assertEqual(res["categories"], ["A", "B", "C"])

    def test_too_many_rejected(self):
        res = self.ob.validate_categories([f"c{i}" for i in range(31)])
        self.assertFalse(res["ok"])


class TestStepsAndOptions(unittest.TestCase):
    def setUp(self):
        self.ob = Onboarding(_config())

    def test_has_four_steps(self):
        steps = self.ob.steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual([s["id"] for s in steps],
                         ["model", "capture", "categories", "done"])

    def test_providers_include_deepseek(self):
        labels = [p["label"] for p in self.ob.providers()]
        self.assertTrue(any("DeepSeek" in l for l in labels))

    def test_regions_cover_four_presets(self):
        values = {r["value"] for r in self.ob.region_options()}
        self.assertEqual(values, {"full", "left_half", "right_half", "center"})


class TestPublicConfigNoSecret(unittest.TestCase):
    """api_key 绝不能通过公共接口回显。"""

    def test_masked(self):
        cfg = _config()
        cfg.set("llm", "api_key", "sk-secret-value-1234")
        pub = cfg.to_public_dict()
        self.assertNotIn("sk-secret-value-1234", str(pub))
        self.assertIn("...", pub["llm"]["api_key"])


class TestConfigRobustness(unittest.TestCase):
    """配置文件健壮性 —— 新手最常踩的坑。"""

    def test_utf8_bom_is_accepted(self):
        """记事本另存为 UTF-8 会加 BOM，不能因此启动失败。

        回归背景：打包成 exe 后实测崩溃
        ``JSONDecodeError: Unexpected UTF-8 BOM``。
        """
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "config.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"llm": {"api_key": "sk-bom-test-1234"}}, ensure_ascii=False).encode("utf-8"))
        cfg = Config(str(p))
        self.assertEqual(cfg.get("llm", "api_key"), "sk-bom-test-1234")

    def test_invalid_json_raises_readable_error(self):
        """非法 JSON 要给出人能看懂的原因，而不是裸的 JSONDecodeError。"""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "config.json"
        p.write_text('{"llm": {,}}', encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            Config(str(p))
        msg = str(ctx.exception)
        self.assertIn("不是合法的 JSON", msg)
        self.assertIn("引号", msg)

    def test_save_writes_no_bom(self):
        """保存的文件不带 BOM，保证任何编辑器都能正常打开。"""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "config.json"
        cfg = Config(str(p))
        cfg.set("llm", "api_key", "sk-abc")
        cfg.save()
        self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_save_creates_backup(self):
        """覆盖前先备份，防止一次误写就丢掉用户配置。"""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "config.json"
        cfg = Config(str(p))
        cfg.set("llm", "api_key", "sk-first-1234")
        cfg.save()
        cfg.set("llm", "api_key", "sk-second-5678")
        cfg.save()
        bak = Path(str(p) + ".bak")
        self.assertTrue(bak.exists())
        with open(bak, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["llm"]["api_key"], "sk-first-1234")

    def test_missing_config_uses_defaults(self):
        tmp = tempfile.mkdtemp()
        cfg = Config(str(Path(tmp) / "nope.json"))
        self.assertEqual(cfg.get("llm", "model"), "deepseek-flash")
        self.assertTrue(cfg.get("tray", "enabled", default=False))
        self.assertIn("onboarding", cfg.to_dict())


if __name__ == "__main__":
    unittest.main()
