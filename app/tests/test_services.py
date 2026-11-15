"""upload 与 skill_server 模块的测试（SDD 验证）。

覆盖点：上报器配置/头构造/未配置保护；Skill 服务鉴权与路由。
"""

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report import build_report
from skill_server import SkillHandler, SkillServer
from store import RecordStore
from upload import ReportUploader


class TestReportUploader(unittest.TestCase):
    def test_disabled_by_default(self):
        u = ReportUploader()
        self.assertFalse(u.enabled)
        r = u.upload("x", {})
        self.assertFalse(r["ok"])

    def test_headers_with_and_without_token(self):
        u1 = ReportUploader(url="http://x", token="")
        self.assertNotIn("Authorization", u1._headers())
        u2 = ReportUploader(url="http://x", token="tok123")
        self.assertEqual(u2._headers()["Authorization"], "Bearer tok123")

    def test_enabled_when_url_set(self):
        u = ReportUploader(url="http://localhost:9/report", token="t")
        self.assertTrue(u.enabled)

    # ---------- 回归：首次请求就失败时不能抛 UnboundLocalError ----------

    def test_timeout_on_first_attempt_returns_error_dict(self):
        """回归：第一次尝试就超时，response 从未赋值。

        旧实现里 `getattr(response, ...)` 会因 response 未定义抛 UnboundLocalError。
        """
        u = ReportUploader(url="http://10.255.255.1:81/report", token="t")
        # 缩短超时，避免测试长时间等待
        u.REQUEST_TIMEOUT = 0.05
        r = u.upload("x", {"a": 1})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        self.assertEqual(r["http_status"], 0)
        self.assertGreaterEqual(r["duration_ms"], 0)

    def test_connection_error_returns_error_dict(self):
        u = ReportUploader(url="http://127.0.0.1:9/report", token="t")
        u.REQUEST_TIMEOUT = 1.0
        r = u.upload("x", {"a": 1})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        self.assertEqual(r["http_status"], 0)


class TestSkillServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = RecordStore(str(Path(cls.tmp) / "srv.db"))
        cls.store.insert_entry({
            "started_at": int(datetime.now().timestamp()),
            "ended_at": int(datetime.now().timestamp()),
            "content": "写代码", "category": "代码开发",
        })
        SkillHandler.store = cls.store
        SkillHandler.token = "test-token"
        SkillHandler.report_builder = build_report
        cls.server = HTTPServer(("127.0.0.1", 0), SkillHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()

    def _get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # trust_env=False：不读取系统代理环境变量。否则开启系统代理的机器上，
        # 本地 127.0.0.1 请求会被代理转发，导致 502 / 非 JSON 响应。
        with httpx.Client(timeout=5, trust_env=False) as client:
            r = client.get(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        return r.status_code, r.json()

    def test_health_no_token(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_unauthorized_without_token(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 401)

    def test_authorized_status(self):
        status, body = self._get("/api/status", token="test-token")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["entries_count"], 1)

    def test_entries_by_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        status, body = self._get(f"/api/entries?date={today}", token="test-token")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["entries"]), 1)

    def test_report(self):
        today = datetime.now().strftime("%Y-%m-%d")
        status, body = self._get(f"/api/report?date={today}", token="test-token")
        self.assertEqual(status, 200)
        self.assertIn("工作日报", body["report"])


if __name__ == "__main__":
    unittest.main()
