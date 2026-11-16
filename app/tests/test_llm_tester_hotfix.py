"""「测试连接」假阴性回归测试（SDD 验证，标准库 unittest）。

Bug 背景
--------
``main.py`` 启动时把 ``llm_tester=self.llm.test_connection`` 直接绑给
``SkillServer``。这个绑定在**服务启动那一刻**就固定了当时的 LLMClient 实例。

用户在设置页改掉 api_key / api_base 后：

* 识别循环走 ``self.llm``（被 ``_reload_settings()`` 重建过）-> 用新凭据，正常
* 「测试连接」走 ``llm_tester``（仍是启动时的旧实例）-> 用旧凭据，报 401

表现为**假阴性**：Key 明明改对了，测试连接却仍说连不上。用户只能靠重启解决。

修复：改为 ``self._make_llm_tester()``，返回一个每次调用都取当前实例的可调用对象。

本测试锁定该行为：凭据变更后，测试连接必须立即反映新凭据，**无需重启**。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module
from main import DailyHelper


class TestLLMTesterNotStale(unittest.TestCase):
    """llm_tester 不得绑定到启动时的旧 LLMClient 实例。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = Path(self.tmp) / "config.json"
        self.helper = None

    def tearDown(self):
        if self.helper is not None:
            self.helper.store.close()

    def _build_helper(self):
        helper = DailyHelper(str(self.cfg_path))
        helper.store.close()  # 换掉默认创建的 store，避免污染工作目录
        self.helper = helper
        return helper

    def test_tester_reflects_updated_credentials_without_restart(self):
        """改凭据后测试连接必须立即反映新值，不需重启。"""
        helper = self._build_helper()
        tester = helper._make_llm_tester()

        # 模拟用户在设置页改 Key —— 走真实的热更新路径
        helper.config.set("llm", "api_key", "new-key-abc")
        helper.config.save()
        helper._reload_settings()

        self.assertEqual(helper.llm.api_key, "new-key-abc")

        # 关键断言：测试连接回调必须用**当前**实例的凭据
        seen = {}

        def fake_test(self):
            seen["key"] = self.api_key
            return {"ok": True, "reply": "ok"}

        with patch.object(type(helper.llm), "test_connection", fake_test):
            result = tester()

        self.assertTrue(result["ok"])
        self.assertEqual(seen.get("key"), "new-key-abc",
                         "测试连接必须使用当前实例的凭据，而不是启动时的旧凭据")

    def test_tester_before_update_uses_old_key(self):
        """对照组：未改凭据时，测试连接用启动时的凭据。"""
        helper = self._build_helper()
        helper.config.set("llm", "api_key", "original-key")
        helper.config.save()
        helper._reload_settings()

        tester = helper._make_llm_tester()
        seen = {}

        def fake_test(self):
            seen["key"] = self.api_key
            return {"ok": True, "reply": "ok"}

        with patch.object(type(helper.llm), "test_connection", fake_test):
            tester()

        self.assertEqual(seen.get("key"), "original-key")

    def test_llm_tester_is_not_bound_method(self):
        """启动时应传入可调用对象而非绑定方法。

        绑定方法会写死实例；lambda 每次求值取当前 self.llm。
        本测试直接验证 main.py 的源码写法，避免回归。
        """
        src = Path(main_module.__file__).read_text(encoding="utf-8")
        self.assertIn("llm_tester=self._make_llm_tester()", src,
                      "main.py 必须用 _make_llm_tester() 绑定 llm_tester，"
                      "否则凭据热更新后测试连接会使用旧实例（假阴性）")
        self.assertNotIn("llm_tester=self.llm.test_connection,", src,
                         "禁止直接绑定绑定方法：它会固定住启动时的 LLMClient 实例")

    def test_make_llm_tester_returns_callable(self):
        helper = self._build_helper()
        tester = helper._make_llm_tester()
        self.assertTrue(callable(tester))
        # 不是绑定方法：绑定方法带 __self__ 属性
        self.assertFalse(hasattr(tester, "__self__"),
                         "llm_tester 不得是绑定方法（会固定住旧实例）")


if __name__ == "__main__":
    unittest.main()
