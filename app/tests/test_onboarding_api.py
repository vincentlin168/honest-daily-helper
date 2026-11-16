"""首次运行引导的 API 层测试（SDD 验证）。

覆盖点：/api/onboarding（读）、/api/onboarding/validate（校验）、
/api/onboarding/apply（应用）、/api/onboarding/skip（跳过）。

安全要点
--------
- api_key 绝不能在 GET 响应里回显
- 校验不通过时**不得写入任何配置**（不写半成品）
- apply 时 finish=True 才标记完成
"""

import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from onboarding import Onboarding
from skill_server import SkillHandler
from store import RecordStore


class TestOnboardingAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = RecordStore(str(Path(cls.tmp) / "ob.db"))
        cls.cfg = Config(str(Path(cls.tmp) / "config.json"))
        cls.ob = Onboarding(cls.cfg)

        SkillHandler.store = cls.store
        SkillHandler.token = "tok"
        SkillHandler.config = cls.cfg
        SkillHandler.onboarding = cls.ob
        SkillHandler.archive = None
        SkillHandler.export_dir = str(Path(cls.tmp) / "exports")

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SkillHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()

    def setUp(self):
        # 每个测试从干净状态开始
        self.cfg.set("llm", "api_key", "")
        self.cfg.set("llm", "api_base", "https://api.deepseek.com/v1")
        self.cfg.set("llm", "model", "deepseek-flash")
        self.cfg.set("capture", "region_preset", "full")
        self.cfg.set("capture", "snap_interval", 15)
        self.cfg.set("categories", ["代码开发", "其他"])
        # 重置引导标记（上一个测试可能已标记完成并落盘）
        self.cfg.set("onboarding", "completed", False)
        self.cfg.set("onboarding", "skipped", False)

    def _get(self, path, token="tok"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(timeout=10, trust_env=False) as client:
            r = client.get(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        return r.status_code, r.json()

    def _post(self, path, body=None, token="tok"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(timeout=10, trust_env=False) as client:
            r = client.post(f"http://127.0.0.1:{self.port}{path}",
                            headers=headers, json=body or {})
        return r.status_code, r.json()

    # ---------- GET /api/onboarding ----------
    def test_onboarding_info(self):
        status, body = self._get("/api/onboarding")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["completed"])
        self.assertEqual(len(body["steps"]), 4)
        self.assertIn("providers", body)
        self.assertIn("regions", body)

    def test_onboarding_requires_auth(self):
        status, _ = self._get("/api/onboarding", token="wrong")
        self.assertEqual(status, 401)

    def test_onboarding_never_echoes_api_key(self):
        """安全：GET 响应里不能出现真实 Key。"""
        self.cfg.set("llm", "api_key", "sk-super-secret-123456")
        status, body = self._get("/api/onboarding")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-super-secret-123456", str(body))

    def test_completed_when_key_configured(self):
        self.cfg.set("llm", "api_key", "sk-abcdefghijklmnop")
        status, body = self._get("/api/onboarding")
        self.assertTrue(body["completed"])

    # ---------- GET /api/onboarding/validate ----------
    def test_validate_model_ok(self):
        status, body = self._get(
            "/api/onboarding/validate?kind=model"
            "&api_base=https://api.deepseek.com/v1&model=deepseek-flash&api_key=sk-abc")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_validate_model_bad(self):
        status, body = self._get("/api/onboarding/validate?kind=model&api_base=&model=&api_key=")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(len(body["problems"]) >= 3)

    def test_validate_capture(self):
        status, body = self._get(
            "/api/onboarding/validate?kind=capture&region_preset=full&snap_interval=15")
        self.assertTrue(body["ok"])

    def test_validate_capture_bad_interval(self):
        status, body = self._get(
            "/api/onboarding/validate?kind=capture&region_preset=full&snap_interval=1")
        self.assertFalse(body["ok"])

    def test_validate_categories(self):
        status, body = self._get(
            "/api/onboarding/validate?kind=categories&categories=代码开发,资料研究")
        self.assertTrue(body["ok"])
        self.assertEqual(body["categories"], ["代码开发", "资料研究"])

    def test_validate_unknown_kind(self):
        status, body = self._get("/api/onboarding/validate?kind=bogus")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    # ---------- POST /api/onboarding/apply ----------
    def test_apply_writes_config(self):
        status, body = self._post("/api/onboarding/apply", {
            "api_base": "https://api.deepseek.com/v1",
            "model": "deepseek-flash",
            "api_key": "sk-real-key-value-0001",
            "region_preset": "left_half",
            "snap_interval": 20,
            "categories": ["代码开发", "资料研究"],
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.cfg.get("llm", "model"), "deepseek-flash")
        self.assertEqual(self.cfg.get("capture", "region_preset"), "left_half")
        self.assertEqual(self.cfg.get("capture", "snap_interval"), 20)
        self.assertEqual(self.cfg.get("categories"), ["代码开发", "资料研究"])

    def test_apply_never_writes_real_config_file(self):
        """安全护栏：apply 绝不能写到真实 config.json。

        回归背景：曾在真实环境跑 UI 流程测试，把用户的真实 API Key
        覆盖成了测试值。这个断言锁住「测试用的 config 路径是临时的」。
        """
        import config as config_mod
        self.assertNotEqual(
            str(self.cfg.config_path).lower(),
            str(config_mod.Path("config.json").resolve()).lower(),
            "测试用的 Config 必须指向临时文件，不能是真实 config.json")
        self.assertIn(tempfile.gettempdir().lower(),
                      str(self.cfg.config_path).lower(),
                      "测试用的 Config 必须落在临时目录里")

    def test_apply_with_finish_marks_completed(self):
        self._post("/api/onboarding/apply", {
            "api_base": "https://api.deepseek.com/v1",
            "model": "deepseek-flash",
            "api_key": "sk-real-key-value-0002",
            "categories": ["代码开发"],
            "finish": True,
        })
        self.assertTrue(self.ob.is_completed())
        self.assertTrue(self.cfg.get("onboarding", "completed", default=False))

    def test_apply_without_finish_not_completed(self):
        """只保存不完成 -> 不应标记为已完成。"""
        self._post("/api/onboarding/apply", {
            "api_base": "https://api.deepseek.com/v1",
            "model": "deepseek-flash",
            "api_key": "sk-real-key-value-0003",
        })
        self.assertFalse(self.cfg.get("onboarding", "completed", default=False))

    def test_apply_rejects_bad_input_and_writes_nothing(self):
        """校验不通过时不得写入任何配置（不写半成品）。"""
        before_base = self.cfg.get("llm", "api_base")
        before_region = self.cfg.get("capture", "region_preset")
        status, body = self._post("/api/onboarding/apply", {
            "api_base": "not-a-url",
            "model": "m",
            "api_key": "sk-abc",
            "region_preset": "bogus_region",
        })
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(body["problems"])
        # 配置未被污染
        self.assertEqual(self.cfg.get("llm", "api_base"), before_base)
        self.assertEqual(self.cfg.get("capture", "region_preset"), before_region)

    def test_apply_empty_categories_rejected(self):
        status, body = self._post("/api/onboarding/apply", {
            "api_base": "https://x.com/v1",
            "model": "m",
            "api_key": "sk-abc",
            "categories": [],
        })
        self.assertFalse(body["ok"])

    # ---------- POST /api/onboarding/skip ----------
    def test_skip(self):
        status, body = self._post("/api/onboarding/skip")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(self.cfg.get("onboarding", "skipped", default=False))
        # 跳过不等于配好
        self.assertFalse(self.ob.is_completed())


class TestSkillServerWiring(unittest.TestCase):
    """SkillServer.__init__ 必须把依赖注入到 SkillHandler。

    回归背景：新增 onboarding 参数时只改了 main.py 的调用处，
    忘了加 __init__ 的形参 —— 表现为启动时
    ``TypeError: SkillServer.__init__() got an unexpected keyword argument``
    。而 API 测试直接设 SkillHandler 类属性，绕过了 __init__，
    所以没抓到这个错。
    """

    def test_accepts_onboarding_kwarg(self):
        from skill_server import SkillServer
        tmp = tempfile.mkdtemp()
        store = RecordStore(str(Path(tmp) / "w.db"))
        try:
            ob = Onboarding(Config(str(Path(tmp) / "c.json")))
            # 不应抛 TypeError
            SkillServer(store=store, onboarding=ob)
            self.assertIs(SkillHandler.onboarding, ob)
        finally:
            store.close()

    def test_all_injected_onto_handler(self):
        from skill_server import SkillServer
        tmp = tempfile.mkdtemp()
        store = RecordStore(str(Path(tmp) / "w2.db"))
        try:
            cfg = Config(str(Path(tmp) / "c2.json"))
            ob = Onboarding(cfg)
            SkillServer(store=store, token="t", port=9999, config=cfg,
                        onboarding=ob, export_dir="ex")
            self.assertEqual(SkillHandler.token, "t")
            self.assertEqual(SkillHandler.export_dir, "ex")
            self.assertIs(SkillHandler.config, cfg)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
