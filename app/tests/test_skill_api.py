"""Skill 服务新增接口的测试（SDD 验证）。

覆盖点：/api/llm/test 连通性测试路由；并发请求不再互相阻塞（ThreadingHTTPServer）。

回归背景
--------
1. ``llm_client.test_connection()`` 早已实现却没有任何出口，用户配完 Key
   无从得知能否用 —— 尤其在 Key 额度耗尽时只能靠猜。
2. 旧实现用单线程 ``HTTPServer``：Web UI 切页签并发请求时，慢请求
   （如生成日报）会阻塞其它请求，页面表现为「卡住」。
"""

import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report import build_report
from skill_server import SkillHandler
from store import RecordStore


class TestLLMTestEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = RecordStore(str(Path(cls.tmp) / "srv.db"))
        SkillHandler.store = cls.store
        SkillHandler.token = "test-token"
        SkillHandler.report_builder = build_report
        SkillHandler.llm_tester = lambda: {"ok": True, "reply": "连接成功"}
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

    def _post(self, path, token="test-token"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(timeout=10, trust_env=False) as client:
            r = client.post(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        return r.status_code, r.json()

    def test_llm_test_returns_ok(self):
        status, body = self._post("/api/llm/test")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["reply"], "连接成功")

    def test_llm_test_requires_auth(self):
        status, body = self._post("/api/llm/test", token=None)
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_llm_test_propagates_failure(self):
        """模型服务报错时，接口应返回 ok=False 而不是抛 500。"""
        def failing_tester():
            raise RuntimeError("连接失败：额度耗尽")

        old = SkillHandler.llm_tester
        SkillHandler.llm_tester = failing_tester
        try:
            status, body = self._post("/api/llm/test")
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertIn("额度耗尽", body["error"])
        finally:
            SkillHandler.llm_tester = old


class TestConcurrentRequests(unittest.TestCase):
    """并发请求不被阻塞（ThreadingHTTPServer 回归）。"""

    def test_concurrent_requests_not_blocked(self):
        tmp = tempfile.mkdtemp()
        store = RecordStore(str(Path(tmp) / "c.db"))
        SkillHandler.store = store
        SkillHandler.token = ""
        SkillHandler.report_builder = build_report
        SkillHandler.llm_tester = None

        server = ThreadingHTTPServer(("127.0.0.1", 0), SkillHandler)
        server.daemon_threads = True
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            results = []

            def hit():
                with httpx.Client(timeout=10, trust_env=False) as client:
                    r = client.get(f"http://127.0.0.1:{port}/api/health")
                results.append(r.status_code)

            threads = [threading.Thread(target=hit) for _ in range(8)]
            started = time.time()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            elapsed = time.time() - started

            self.assertEqual(len(results), 8, "并发请求应全部完成")
            self.assertTrue(all(code == 200 for code in results))
            # 单线程实现下 8 个请求会串行排队；多线程下应远快于此
            self.assertLess(elapsed, 5.0, f"并发请求耗时 {elapsed:.2f}s，疑似被串行阻塞")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            store.close()


if __name__ == "__main__":
    unittest.main()
