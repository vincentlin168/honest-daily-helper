"""Skill 本地服务 + Web UI 后端（Spec-Driven）。

Spec
----
提供本地 HTTP 服务（默认 127.0.0.1:8088），既是「Skill 服务」（供其他 Agent 查询），
也是「Web UI 后端」（浏览器可视化界面 + 设置面板）。

接口：
    GET  /                       Web UI 页面
    GET  /api/health             健康检查（无鉴权）
    GET  /api/status             服务状态
    GET  /api/entries?date=...   按天查记录
    GET  /api/report?date=...    生成日报
    GET  /api/settings           读取配置（密钥掩码）
    POST /api/settings           更新配置（截屏间隔/分类/模型等）
    POST /api/entries/{id}/category  手动改分类

设计要点：
1. 零依赖（标准库 http.server），只监听 127.0.0.1。
2. 可选 Bearer Token 鉴权。
3. 设置更新通过 settings_callback 回调，让主程序热更新模块。
4. **多线程处理**（ThreadingHTTPServer）：Web UI 切页签时会并发请求，
   单线程 HTTPServer 会让慢请求（如生成日报）阻塞其它请求，页面表现为「卡住」。
"""

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from overview import build_overview


def _resource_path(name: str) -> Path:
    """资源路径：兼容 PyInstaller 打包（资源在 sys._MEIPASS）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


class SkillHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    protocol_version = "HTTP/1.1"

    store = None              # RecordStore
    token = ""                # 鉴权 token
    report_builder = None     # build_report 函数
    config = None             # Config 实例
    settings_callback = None  # 设置更新后的回调（热更新模块）
    llm_tester = None         # 连通性测试回调（返回 {ok, reply|error}）

    def _authorized(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 页面与健康检查无需鉴权
        if path == "/":
            html_path = _resource_path("web_ui.html")
            if html_path.exists():
                self._send_html(html_path.read_text(encoding="utf-8"))
            else:
                self._send_html("<h2>web_ui.html 未找到</h2>")
            return

        if path == "/api/health":
            self._send_json({"ok": True, "service": "honest-daily-helper"})
            return

        if not self._authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        if path == "/api/status":
            today = datetime.now().strftime("%Y-%m-%d")
            entries = self.store.entries_by_day(today) if self.store else []
            self._send_json({"ok": True, "date": today, "entries_count": len(entries)})
            return

        if path == "/api/entries":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            self._send_json({"ok": True, "date": date, "entries": entries})
            return

        if path == "/api/report":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            builder = type(self).report_builder
            report_md = builder(entries, date) if builder else ""
            self._send_json({"ok": True, "date": date, "report": report_md})
            return

        if path == "/api/settings":
            cfg = type(self).config
            self._send_json(cfg.to_public_dict() if cfg else {})
            return

        # 概览：今日统计卡片 + 成本透明度
        if path == "/api/overview":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            usage = self.store.token_usage_by_day(date) if self.store else {}
            cfg = type(self).config
            price_in = float(cfg.get("llm", "price_per_1m_input", default=0) or 0) if cfg else 0.0
            price_out = float(cfg.get("llm", "price_per_1m_output", default=0) or 0) if cfg else 0.0
            data = build_overview(entries, usage, price_in, price_out)
            data["date"] = date
            self._send_json({"ok": True, **data})
            return

        # 热力图：最近 N 天 × 24 小时
        if path == "/api/heatmap":
            try:
                days = int((query.get("days") or [7])[0])
            except (TypeError, ValueError):
                days = 7
            grid = self.store.hourly_histogram(days) if self.store else []
            self._send_json({"ok": True, "days": days, "grid": grid})
            return

        self._send_json({"ok": False, "error": f"unknown path: {path}"}, status=404)

    # ---------- POST ----------
    def     do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        # 连通性测试：/api/llm/test —— 验证 API Key / 模型 / 网络是否可用
        if path == "/api/llm/test":
            tester = type(self).llm_tester
            if tester is None:
                self._send_json({"ok": False, "error": "服务未就绪"}, status=503)
                return
            try:
                self._send_json(tester())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=200)
            return

        # 手动改分类：/api/entries/{id}/category
        if path.startswith("/api/entries/") and path.endswith("/category"):
            try:
                entry_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._send_json({"ok": False, "error": "invalid id"}, status=400)
                return
            body = self._read_json_body()
            category = body.get("category", "")
            if not category:
                self._send_json({"ok": False, "error": "category required"}, status=400)
                return
            ok = self.store.update_category(entry_id, category) if self.store else False
            self._send_json({"ok": ok})
            return

        # 更新设置：/api/settings
        if path == "/api/settings":
            body = self._read_json_body()
            cfg = type(self).config
            if cfg is None:
                self._send_json({"ok": False, "error": "config not available"}, status=500)
                return
            cfg.update_from_dict(body)
            cfg.save()
            callback = type(self).settings_callback
            if callback:
                try:
                    callback()
                except Exception:
                    pass
            self._send_json({"ok": True, "message": "设置已保存并生效"})
            return

        self._send_json({"ok": False, "error": f"unknown path: {path}"}, status=404)

    def log_message(self, *args):
        pass  # 静默


class SkillServer:
    """Skill 本地服务。"""

    def __init__(self, store, token: str = "", port: int = 8088,
                 report_builder=None, config=None, settings_callback=None,
                 llm_tester=None):
        self.store = store
        self.port = port
        SkillHandler.store = store
        SkillHandler.token = token or ""
        SkillHandler.report_builder = report_builder
        SkillHandler.config = config
        SkillHandler.settings_callback = settings_callback
        SkillHandler.llm_tester = llm_tester

    def run(self):
        # daemon_threads=True：请求线程随主进程退出，避免关不掉
        server = ThreadingHTTPServer(("127.0.0.1", self.port), SkillHandler)
        server.daemon_threads = True
        print(f"Skill 服务已启动: http://127.0.0.1:{self.port}")
        server.serve_forever()
