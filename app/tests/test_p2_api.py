"""P2 数据可信闭环的 API 层测试（SDD 验证）。

覆盖点：/api/errors、/api/archive*、/api/export*、/api/entries/{id} 编辑与删除。
"""

import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archive import ReportArchive
from skill_server import SkillHandler
from store import RecordStore


class TestP2Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = RecordStore(str(Path(cls.tmp) / "p2.db"))
        cls.archive = ReportArchive(str(Path(cls.tmp) / "reports"))
        cls.export_dir = str(Path(cls.tmp) / "exports")

        SkillHandler.store = cls.store
        SkillHandler.token = "tok"
        SkillHandler.archive = cls.archive
        SkillHandler.export_dir = cls.export_dir
        SkillHandler.config = None

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

    # ---------- /api/errors ----------
    def test_errors_endpoint(self):
        status, body = self._get("/api/errors?days=7")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("by_kind", body)
        self.assertIn("error_rate", body)

    def test_errors_requires_auth(self):
        status, _ = self._get("/api/errors", token="wrong")
        self.assertEqual(status, 401)

    # ---------- /api/archive ----------
    def test_archive_list_and_body(self):
        self.archive.save("2026-11-16", "# 留档内容")
        status, body = self._get("/api/archive")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["date"] == "2026-11-16" for r in body["reports"]))

        status, body = self._get("/api/archive/body?date=2026-11-16")
        self.assertEqual(status, 200)
        self.assertIn("留档内容", body["report"])

    def test_archive_body_missing(self):
        status, body = self._get("/api/archive/body?date=2020-01-01")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_archive_delete(self):
        self.archive.save("2026-11-15", "x")
        status, body = self._post("/api/archive/delete", {"date": "2026-11-15"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(self.archive.load("2026-11-15"))

    # ---------- /api/export ----------
    def test_export_preview_json(self):
        now = datetime.now()
        ts = int(now.replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
        self.store.insert_entry({"started_at": ts, "ended_at": ts + 1800,
                                  "content": "导出测试", "category": "代码开发"})
        date = now.strftime("%Y-%m-%d")
        status, body = self._get(f"/api/export/preview?date={date}&format=json")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["count"], 1)
        self.assertIn("导出测试", body["content"])

    def test_export_preview_bad_format(self):
        status, body = self._get("/api/export/preview?format=exe")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_export_writes_file(self):
        status, body = self._post("/api/export", {"date": "2026-11-16", "format": "md"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(Path(body["path"]).exists())
        self.assertTrue(body["path"].endswith(".md"))

    def test_export_csv_file(self):
        status, body = self._post("/api/export", {"date": "2026-11-16", "format": "csv"})
        self.assertEqual(status, 200)
        self.assertTrue(body["path"].endswith(".csv"))

    # ---------- entries 编辑 / 删除 ----------
    def test_update_entry_endpoint(self):
        now = datetime.now()
        ts = int(now.replace(hour=10, minute=0, second=0, microsecond=0).timestamp())
        eid = self.store.insert_entry({"started_at": ts, "ended_at": ts + 600,
                                        "content": "原始", "category": "其他"})
        status, body = self._post(f"/api/entries/{eid}", {"category": "写文档"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["entry"]["category"], "写文档")

    def test_update_entry_no_fields(self):
        status, body = self._post("/api/entries/999", {})
        self.assertEqual(status, 400)

    def test_update_entry_invalid_id(self):
        status, body = self._post("/api/entries/abc", {"category": "x"})
        self.assertEqual(status, 400)

    def test_delete_entry_endpoint(self):
        now = datetime.now()
        ts = int(now.replace(hour=11, minute=0, second=0, microsecond=0).timestamp())
        eid = self.store.insert_entry({"started_at": ts, "ended_at": ts + 600,
                                        "content": "待删除", "category": "其他"})
        status, body = self._post(f"/api/entries/{eid}/delete", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(self.store.get_entry(eid))


if __name__ == "__main__":
    unittest.main()
